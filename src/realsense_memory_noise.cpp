#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <csignal>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <memory>
#include <mutex>
#include <numeric>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <time.h>
#include <vector>

#if defined(__linux__)
#include <pthread.h>
#include <sched.h>
#endif

namespace
{
volatile std::sig_atomic_t stop_requested = 0;
std::atomic<bool> measurement_started {false};

void handle_signal(int)
{
    stop_requested = 1;
}

struct Options
{
    std::string ready_file;
    std::string summary_output;
    unsigned int workers = 1;
    uint64_t buffer_size_mib = 64;
    uint64_t copy_chunk_kib = 1024;
    double warmup_seconds = 10.0;
    double target_memory_mib_per_second = 0.0;
};

Options parse_options(int argc, char** argv)
{
    Options options;
    const unsigned int hardware_threads = std::thread::hardware_concurrency();
    options.workers = hardware_threads == 0 ? 1 : hardware_threads;

    for (int index = 1; index < argc; ++index)
    {
        const std::string argument = argv[index];
        auto require_value = [&]() -> std::string {
            if (++index >= argc)
                throw std::runtime_error("missing value after " + argument);
            return argv[index];
        };

        if (argument == "--ready-file")
            options.ready_file = require_value();
        else if (argument == "--summary-output")
            options.summary_output = require_value();
        else if (argument == "--workers")
            options.workers = static_cast<unsigned int>(std::stoul(require_value()));
        else if (argument == "--buffer-size-mib")
            options.buffer_size_mib = std::stoull(require_value());
        else if (argument == "--copy-chunk-kib")
            options.copy_chunk_kib = std::stoull(require_value());
        else if (argument == "--warmup-seconds")
            options.warmup_seconds = std::stod(require_value());
        else if (argument == "--target-memory-mib-per-second")
            options.target_memory_mib_per_second = std::stod(require_value());
        else if (argument == "--help" || argument == "-h")
        {
            std::cout
                << "Usage: realsense_memory_noise [OPTIONS]\n"
                << "Continuously copy between fixed-size thread-private buffers.\n\n"
                << "Options:\n"
                << "  --ready-file PATH        write JSON after warm-up (required)\n"
                << "  --summary-output PATH    write final JSON on shutdown (required)\n"
                << "  --workers N              memory-copy worker threads (default: online CPUs)\n"
                << "  --buffer-size-mib N      bytes copied per iteration (default: 64 MiB)\n"
                << "  --copy-chunk-kib N       pacing/copy quantum within each buffer (default: 1024 KiB)\n"
                << "  --warmup-seconds N       seconds before ready (default: 10)\n"
                << "  --target-memory-mib-per-second N\n"
                << "                           aggregate estimated read+write rate; 0 is unlimited\n";
            std::exit(0);
        }
        else
            throw std::runtime_error("unknown argument: " + argument);
    }

    if (options.ready_file.empty())
        throw std::runtime_error("--ready-file is required");
    if (options.summary_output.empty())
        throw std::runtime_error("--summary-output is required");
    if (options.workers < 1)
        throw std::runtime_error("worker count must be positive");
    if (options.buffer_size_mib < 1)
        throw std::runtime_error("buffer size must be positive");
    if (options.copy_chunk_kib < 1)
        throw std::runtime_error("copy chunk size must be positive");
    if (options.warmup_seconds <= 0.0)
        throw std::runtime_error("warm-up duration must be positive");
    if (!std::isfinite(options.target_memory_mib_per_second)
        || options.target_memory_mib_per_second < 0.0)
        throw std::runtime_error("target memory rate must be finite and non-negative");
    if (options.target_memory_mib_per_second > 0.0
        && options.target_memory_mib_per_second < 1.0)
        throw std::runtime_error("target memory rate must be zero or at least 1 MiB/s");
    return options;
}

void write_text_file(const std::string& path, const std::string& contents)
{
    std::ofstream output(path, std::ios::out | std::ios::trunc);
    if (!output)
        throw std::runtime_error("cannot open output file: " + path);
    output << contents << '\n';
    output.close();
    if (!output)
        throw std::runtime_error("cannot write output file: " + path);
}

uint64_t boottime_ns()
{
    timespec value {};
    if (clock_gettime(CLOCK_BOOTTIME, &value) != 0)
        throw std::runtime_error("clock_gettime(CLOCK_BOOTTIME) failed");
    return static_cast<uint64_t>(value.tv_sec) * 1000000000ULL
        + static_cast<uint64_t>(value.tv_nsec);
}

double process_cpu_seconds()
{
    timespec value {};
    if (clock_gettime(CLOCK_PROCESS_CPUTIME_ID, &value) != 0)
        throw std::runtime_error("clock_gettime(CLOCK_PROCESS_CPUTIME_ID) failed");
    return static_cast<double>(value.tv_sec)
        + static_cast<double>(value.tv_nsec) / 1000000000.0;
}

double cpu_equivalents(double cpu_seconds, double wall_seconds)
{
    return wall_seconds > 0.0 ? cpu_seconds / wall_seconds : 0.0;
}

double mib_per_second(uint64_t bytes, double wall_seconds)
{
    return wall_seconds > 0.0
        ? static_cast<double>(bytes) / (1024.0 * 1024.0) / wall_seconds
        : 0.0;
}

std::string effective_cpu_affinity()
{
#if defined(__linux__)
    cpu_set_t cpus;
    CPU_ZERO(&cpus);
    if (sched_getaffinity(0, sizeof(cpus), &cpus) != 0)
        throw std::runtime_error("sched_getaffinity failed");
    std::ostringstream output;
    bool first = true;
    for (int cpu = 0; cpu < CPU_SETSIZE; ++cpu)
    {
        if (!CPU_ISSET(cpu, &cpus))
            continue;
        if (!first)
            output << ',';
        output << cpu;
        first = false;
    }
    return output.str();
#else
    return "unknown";
#endif
}

using ByteBuffer = std::unique_ptr<unsigned char, decltype(&std::free)>;

ByteBuffer allocate_aligned(uint64_t size_bytes)
{
    void* memory = nullptr;
    if (size_bytes > std::numeric_limits<std::size_t>::max())
        throw std::runtime_error("buffer size exceeds addressable memory");
    if (posix_memalign(&memory, 64, static_cast<std::size_t>(size_bytes)) != 0)
        throw std::runtime_error("cannot allocate aligned memory-copy buffer");
    return ByteBuffer(static_cast<unsigned char*>(memory), &std::free);
}

struct WorkerBuffers
{
    explicit WorkerBuffers(uint64_t size_bytes, unsigned int index)
        : first(allocate_aligned(size_bytes))
        , second(allocate_aligned(size_bytes))
    {
        std::memset(first.get(), static_cast<int>(0x31U + index % 127U), size_bytes);
        std::memset(second.get(), static_cast<int>(0xa7U - index % 127U), size_bytes);
    }

    ByteBuffer first {nullptr, &std::free};
    ByteBuffer second {nullptr, &std::free};
};

class AggregateRateLimiter
{
public:
    AggregateRateLimiter(uint64_t estimated_bytes_per_quantum, double target_mib_per_second)
        : enabled_(target_mib_per_second > 0.0)
    {
        if (!enabled_)
            return;
        constexpr long double bytes_per_mib = 1024.0L * 1024.0L;
        const long double seconds = static_cast<long double>(estimated_bytes_per_quantum)
            / (static_cast<long double>(target_mib_per_second) * bytes_per_mib);
        interval_ = std::chrono::duration_cast<Clock::duration>(
            std::chrono::duration<long double>(seconds));
        if (interval_ < std::chrono::nanoseconds(1))
            interval_ = std::chrono::nanoseconds(1);
    }

    bool enabled() const
    {
        return enabled_;
    }

    uint64_t interval_ns() const
    {
        return static_cast<uint64_t>(
            std::chrono::duration_cast<std::chrono::nanoseconds>(interval_).count());
    }

    bool wait_for_slot()
    {
        if (!enabled_)
            return !stop_requested;

        std::unique_lock<std::mutex> lock(mutex_);
        const Clock::time_point now = Clock::now();
        if (!initialized_ || next_slot_ < now)
        {
            next_slot_ = now;
            initialized_ = true;
        }
        const Clock::time_point slot = next_slot_;

        while (!stop_requested)
        {
            const Clock::time_point current = Clock::now();
            if (current >= slot)
            {
                // Schedule from the actual release time so delayed workers do not
                // accumulate credits and later produce an unbounded catch-up burst.
                next_slot_ = current + interval_;
                return true;
            }
            const auto remaining = slot - current;
            std::this_thread::sleep_for(
                std::min(remaining, std::chrono::duration_cast<Clock::duration>(
                                        std::chrono::milliseconds(10))));
        }
        return false;
    }

private:
    using Clock = std::chrono::steady_clock;

    bool enabled_ = false;
    bool initialized_ = false;
    Clock::duration interval_ {};
    Clock::time_point next_slot_ {};
    std::mutex mutex_;
};

void copy_worker(
    unsigned int index,
    uint64_t buffer_size_bytes,
    uint64_t copy_chunk_bytes,
    WorkerBuffers& buffers,
    AggregateRateLimiter& rate_limiter,
    std::atomic<uint64_t>& progress_chunks,
    std::vector<uint64_t>& measured_chunks,
    std::vector<uint64_t>& completed_buffer_passes,
    std::vector<uint64_t>& checksums)
{
#if defined(__linux__)
    const std::string name = "rs-mem-noise-" + std::to_string(index);
    (void)pthread_setname_np(pthread_self(), name.substr(0, 15).c_str());
#endif

    unsigned char* source = buffers.first.get();
    unsigned char* destination = buffers.second.get();
    uint64_t offset = 0;
    uint64_t chunks = 0;
    uint64_t passes = 0;
    while (!stop_requested)
    {
        if (!rate_limiter.wait_for_slot())
            break;
        std::memcpy(
            destination + offset,
            source + offset,
            static_cast<std::size_t>(copy_chunk_bytes));
        offset += copy_chunk_bytes;
        if (offset == buffer_size_bytes)
        {
            offset = 0;
            std::swap(source, destination);
            if (measurement_started.load(std::memory_order_relaxed))
                ++passes;
        }
        progress_chunks.fetch_add(1, std::memory_order_relaxed);
        if (measurement_started.load(std::memory_order_relaxed))
            ++chunks;
    }

    measured_chunks[index] = chunks;
    completed_buffer_passes[index] = passes;
    checksums[index] = static_cast<uint64_t>(source[0])
        + static_cast<uint64_t>(source[buffer_size_bytes / 2])
        + static_cast<uint64_t>(source[buffer_size_bytes - 1]);
}

void join_all(std::vector<std::thread>& threads)
{
    for (std::thread& worker : threads)
    {
        if (worker.joinable())
            worker.join();
    }
}
} // namespace

int main(int argc, char** argv)
{
    std::vector<std::thread> threads;
    try
    {
        const Options options = parse_options(argc, argv);
        std::signal(SIGINT, handle_signal);
        std::signal(SIGTERM, handle_signal);

        constexpr uint64_t bytes_per_mib = 1024ULL * 1024ULL;
        constexpr uint64_t bytes_per_kib = 1024ULL;
        if (options.buffer_size_mib > std::numeric_limits<uint64_t>::max() / bytes_per_mib)
            throw std::runtime_error("buffer size overflows byte count");
        const uint64_t buffer_size_bytes = options.buffer_size_mib * bytes_per_mib;
        if (options.copy_chunk_kib > std::numeric_limits<uint64_t>::max() / bytes_per_kib)
            throw std::runtime_error("copy chunk size overflows byte count");
        const uint64_t copy_chunk_bytes = options.copy_chunk_kib * bytes_per_kib;
        if (copy_chunk_bytes > buffer_size_bytes)
            throw std::runtime_error("copy chunk size must not exceed buffer size");
        if (buffer_size_bytes % copy_chunk_bytes != 0)
            throw std::runtime_error("copy chunk size must divide buffer size exactly");
        if (options.workers > std::numeric_limits<uint64_t>::max() / 2ULL
            || static_cast<uint64_t>(options.workers) * 2ULL
                > std::numeric_limits<uint64_t>::max() / buffer_size_bytes)
            throw std::runtime_error("total allocation size overflows byte count");
        const uint64_t total_allocated_bytes =
            static_cast<uint64_t>(options.workers) * 2ULL * buffer_size_bytes;
        const uint64_t estimated_memory_bytes_per_quantum = 2ULL * copy_chunk_bytes;
        AggregateRateLimiter rate_limiter(
            estimated_memory_bytes_per_quantum,
            options.target_memory_mib_per_second);

        std::vector<WorkerBuffers> buffers;
        buffers.reserve(options.workers);
        for (unsigned int index = 0; index < options.workers; ++index)
            buffers.emplace_back(buffer_size_bytes, index);

        std::vector<uint64_t> measured_chunks(options.workers, 0);
        std::vector<uint64_t> completed_buffer_passes(options.workers, 0);
        std::vector<uint64_t> checksums(options.workers, 0);
        std::unique_ptr<std::atomic<uint64_t>[]> progress_chunks(
            new std::atomic<uint64_t>[options.workers]);
        for (unsigned int index = 0; index < options.workers; ++index)
            progress_chunks[index].store(0, std::memory_order_relaxed);

        const uint64_t process_start_boottime_ns = boottime_ns();
        const std::string cpu_affinity = effective_cpu_affinity();
        const auto process_begin = std::chrono::steady_clock::now();
        const double cpu_begin = process_cpu_seconds();
        threads.reserve(options.workers);
        for (unsigned int index = 0; index < options.workers; ++index)
        {
            threads.emplace_back(
                copy_worker,
                index,
                buffer_size_bytes,
                copy_chunk_bytes,
                std::ref(buffers[index]),
                std::ref(rate_limiter),
                std::ref(progress_chunks[index]),
                std::ref(measured_chunks),
                std::ref(completed_buffer_passes),
                std::ref(checksums));
        }

        while (!stop_requested)
        {
            const double elapsed = std::chrono::duration<double>(
                std::chrono::steady_clock::now() - process_begin).count();
            if (elapsed >= options.warmup_seconds)
                break;
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        if (stop_requested)
            throw std::runtime_error("terminated during memory-noise warm-up");

        uint64_t warmup_chunks = 0;
        for (unsigned int index = 0; index < options.workers; ++index)
            warmup_chunks += progress_chunks[index].load(std::memory_order_relaxed);
        if (warmup_chunks == 0)
            throw std::runtime_error("memory-copy workers made no warm-up progress");

        const auto ready_time = std::chrono::steady_clock::now();
        const double ready_cpu = process_cpu_seconds();
        const double warmup_wall_seconds = std::chrono::duration<double>(
            ready_time - process_begin).count();
        const double warmup_cpu_seconds = ready_cpu - cpu_begin;
        const uint64_t warmup_payload_bytes = warmup_chunks * copy_chunk_bytes;
        const uint64_t ready_boottime_ns = boottime_ns();
        measurement_started.store(true, std::memory_order_release);
        const double warmup_estimated_memory_mib_per_second =
            mib_per_second(warmup_payload_bytes * 2ULL, warmup_wall_seconds);

        std::ostringstream ready_json;
        ready_json << std::fixed << std::setprecision(6)
                   << "{\"schema_version\":3"
                   << ",\"mode\":\"fixed_copy\""
                   << ",\"ready\":true"
                   << ",\"workers\":" << options.workers
                   << ",\"memory_access\":\"thread_private_memcpy_read_write\""
                   << ",\"buffer_size_mib\":" << options.buffer_size_mib
                   << ",\"buffer_size_bytes\":" << buffer_size_bytes
                   << ",\"copy_chunk_kib\":" << options.copy_chunk_kib
                   << ",\"copy_chunk_bytes\":" << copy_chunk_bytes
                   << ",\"chunks_per_buffer_pass\":"
                   << buffer_size_bytes / copy_chunk_bytes
                   << ",\"buffers_per_worker\":2"
                   << ",\"total_allocated_bytes\":" << total_allocated_bytes
                   << ",\"rate_limited\":"
                   << (rate_limiter.enabled() ? "true" : "false")
                   << ",\"target_memory_mib_per_second\":"
                   << options.target_memory_mib_per_second
                   << ",\"target_payload_mib_per_second\":"
                   << options.target_memory_mib_per_second / 2.0
                   << ",\"estimated_memory_mib_per_quantum\":"
                   << static_cast<double>(estimated_memory_bytes_per_quantum) / bytes_per_mib
                   << ",\"throttle_interval_ns_per_quantum\":"
                   << rate_limiter.interval_ns()
                   << ",\"effective_cpu_affinity\":\"" << cpu_affinity << "\""
                   << ",\"warmup_wall_seconds\":" << warmup_wall_seconds
                   << ",\"warmup_process_cpu_seconds\":" << warmup_cpu_seconds
                   << ",\"warmup_cpu_equivalents\":"
                   << cpu_equivalents(warmup_cpu_seconds, warmup_wall_seconds)
                   << ",\"warmup_payload_mib_per_second\":"
                   << mib_per_second(warmup_payload_bytes, warmup_wall_seconds)
                   << ",\"warmup_estimated_memory_mib_per_second\":"
                   << warmup_estimated_memory_mib_per_second
                   << ",\"warmup_target_ratio\":"
                   << (rate_limiter.enabled()
                           ? warmup_estimated_memory_mib_per_second
                               / options.target_memory_mib_per_second
                           : 0.0)
                   << ",\"process_start_boottime_ns\":" << process_start_boottime_ns
                   << ",\"ready_boottime_ns\":" << ready_boottime_ns
                   << '}';
        write_text_file(options.ready_file, ready_json.str());
        std::cout << "RS_MEMORY_NOISE_READY " << ready_json.str() << std::endl;

        while (!stop_requested)
            std::this_thread::sleep_for(std::chrono::milliseconds(20));

        join_all(threads);
        const auto process_end = std::chrono::steady_clock::now();
        const double cpu_end = process_cpu_seconds();
        const double measurement_wall_seconds = std::chrono::duration<double>(
            process_end - ready_time).count();
        const double measurement_cpu_seconds = cpu_end - ready_cpu;
        const uint64_t total_chunks = std::accumulate(
            measured_chunks.begin(), measured_chunks.end(), uint64_t {0});
        const uint64_t total_buffer_passes = std::accumulate(
            completed_buffer_passes.begin(), completed_buffer_passes.end(), uint64_t {0});
        const uint64_t payload_bytes = total_chunks * copy_chunk_bytes;
        const uint64_t estimated_memory_traffic_bytes = payload_bytes * 2ULL;
        const uint64_t checksum = std::accumulate(
            checksums.begin(), checksums.end(), uint64_t {0});
        const uint64_t end_boottime_ns = boottime_ns();
        const double measured_memory_mib_per_second = mib_per_second(
            estimated_memory_traffic_bytes, measurement_wall_seconds);

        std::ostringstream summary_json;
        summary_json << std::fixed << std::setprecision(6)
                     << "{\"schema_version\":3"
                     << ",\"mode\":\"fixed_copy\""
                     << ",\"success\":true"
                     << ",\"workers\":" << options.workers
                     << ",\"memory_access\":\"thread_private_memcpy_read_write\""
                     << ",\"buffer_size_mib\":" << options.buffer_size_mib
                     << ",\"buffer_size_bytes\":" << buffer_size_bytes
                     << ",\"copy_chunk_kib\":" << options.copy_chunk_kib
                     << ",\"copy_chunk_bytes\":" << copy_chunk_bytes
                     << ",\"chunks_per_buffer_pass\":"
                     << buffer_size_bytes / copy_chunk_bytes
                     << ",\"buffers_per_worker\":2"
                     << ",\"total_allocated_bytes\":" << total_allocated_bytes
                     << ",\"rate_limited\":"
                     << (rate_limiter.enabled() ? "true" : "false")
                     << ",\"target_memory_mib_per_second\":"
                     << options.target_memory_mib_per_second
                     << ",\"target_payload_mib_per_second\":"
                     << options.target_memory_mib_per_second / 2.0
                     << ",\"estimated_memory_mib_per_quantum\":"
                     << static_cast<double>(estimated_memory_bytes_per_quantum) / bytes_per_mib
                     << ",\"throttle_interval_ns_per_quantum\":"
                     << rate_limiter.interval_ns()
                     << ",\"effective_cpu_affinity\":\"" << cpu_affinity << "\""
                     << ",\"measurement_wall_seconds\":" << measurement_wall_seconds
                     << ",\"measurement_process_cpu_seconds\":" << measurement_cpu_seconds
                     << ",\"measurement_cpu_equivalents\":"
                     << cpu_equivalents(measurement_cpu_seconds, measurement_wall_seconds)
                     << ",\"total_chunks\":" << total_chunks
                     << ",\"completed_buffer_passes\":" << total_buffer_passes
                     << ",\"payload_bytes_copied\":" << payload_bytes
                     << ",\"estimated_memory_traffic_bytes\":"
                     << estimated_memory_traffic_bytes
                     << ",\"payload_mib_per_second\":"
                     << mib_per_second(payload_bytes, measurement_wall_seconds)
                     << ",\"estimated_memory_mib_per_second\":"
                     << measured_memory_mib_per_second
                     << ",\"target_ratio\":"
                     << (rate_limiter.enabled()
                             ? measured_memory_mib_per_second
                                 / options.target_memory_mib_per_second
                             : 0.0)
                     << ",\"checksum\":" << checksum
                     << ",\"process_start_boottime_ns\":" << process_start_boottime_ns
                     << ",\"ready_boottime_ns\":" << ready_boottime_ns
                     << ",\"end_boottime_ns\":" << end_boottime_ns
                     << '}';
        write_text_file(options.summary_output, summary_json.str());
        std::cout << "RS_MEMORY_NOISE_RESULT " << summary_json.str() << std::endl;
        return 0;
    }
    catch (const std::exception& error)
    {
        stop_requested = 1;
        join_all(threads);
        std::cerr << "RS_MEMORY_NOISE_ERROR {\"message\":\""
                  << error.what() << "\"}" << std::endl;
        return 2;
    }
}
