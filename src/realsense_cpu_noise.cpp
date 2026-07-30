#include <chrono>
#include <csignal>
#include <cstdint>
#include <cstdlib>
#include <fstream>
#include <functional>
#include <iomanip>
#include <iostream>
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

void handle_signal(int)
{
    stop_requested = 1;
}

struct Options
{
    std::string ready_file;
    std::string summary_output;
    unsigned int workers = 1;
    double warmup_seconds = 10.0;
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
        else if (argument == "--warmup-seconds")
            options.warmup_seconds = std::stod(require_value());
        else if (argument == "--help" || argument == "-h")
        {
            std::cout
                << "Usage: realsense_cpu_noise [OPTIONS]\n"
                << "Continuously execute register-only integer arithmetic.\n\n"
                << "Options:\n"
                << "  --ready-file PATH        write JSON after warm-up (required)\n"
                << "  --summary-output PATH    write final JSON on shutdown (required)\n"
                << "  --workers N              busy-loop worker threads (default: online CPUs)\n"
                << "  --warmup-seconds N       seconds before ready (default: 10)\n";
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
    if (options.warmup_seconds <= 0.0)
        throw std::runtime_error("warm-up duration must be positive");
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

double normalized_utilization(double equivalents, unsigned int workers)
{
    return workers > 0 ? equivalents / static_cast<double>(workers) * 100.0 : 0.0;
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

void busy_worker(
    unsigned int index,
    std::vector<uint64_t>& iterations,
    std::vector<uint64_t>& checksums)
{
#if defined(__linux__)
    const std::string name = "rs-cpu-noise-" + std::to_string(index);
    (void)pthread_setname_np(pthread_self(), name.substr(0, 15).c_str());
#endif

    // The hot path deliberately keeps its state in registers. It performs no
    // allocation, array traversal, I/O, or shared-memory update. The signal
    // flag is checked only once per batch to minimize cache traffic.
    constexpr uint64_t operations_per_batch = 4096;
    uint64_t state = 0x9e3779b97f4a7c15ULL
        ^ (static_cast<uint64_t>(index) + 1ULL) * 0xbf58476d1ce4e5b9ULL;
    uint64_t completed = 0;
    while (!stop_requested)
    {
        for (uint64_t operation = 0; operation < operations_per_batch; ++operation)
        {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
            state *= 0xd6e8feb86659fd93ULL;
#if defined(__GNUC__) || defined(__clang__)
            __asm__ __volatile__("" : "+r"(state));
#endif
        }
        completed += operations_per_batch;
    }

    iterations[index] = completed;
    checksums[index] = state;
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

        std::vector<uint64_t> iterations(options.workers, 0);
        std::vector<uint64_t> checksums(options.workers, 0);
        const uint64_t process_start_boottime_ns = boottime_ns();
        const std::string cpu_affinity = effective_cpu_affinity();
        const auto process_begin = std::chrono::steady_clock::now();
        const double cpu_begin = process_cpu_seconds();
        threads.reserve(options.workers);
        for (unsigned int index = 0; index < options.workers; ++index)
        {
            threads.emplace_back(
                busy_worker,
                index,
                std::ref(iterations),
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
            throw std::runtime_error("terminated during CPU-noise warm-up");

        const auto ready_time = std::chrono::steady_clock::now();
        const double ready_cpu = process_cpu_seconds();
        const double warmup_wall_seconds = std::chrono::duration<double>(
            ready_time - process_begin).count();
        const double warmup_cpu_seconds = ready_cpu - cpu_begin;
        const double warmup_equivalents = cpu_equivalents(
            warmup_cpu_seconds, warmup_wall_seconds);
        const uint64_t ready_boottime_ns = boottime_ns();

        std::ostringstream ready_json;
        ready_json << std::fixed << std::setprecision(6)
                   << "{\"schema_version\":1"
                   << ",\"mode\":\"busy_loop\""
                   << ",\"ready\":true"
                   << ",\"workers\":" << options.workers
                   << ",\"working_set\":\"register_only\""
                   << ",\"effective_cpu_affinity\":\"" << cpu_affinity << "\""
                   << ",\"operations_per_stop_check\":4096"
                   << ",\"warmup_wall_seconds\":" << warmup_wall_seconds
                   << ",\"warmup_process_cpu_seconds\":" << warmup_cpu_seconds
                   << ",\"warmup_cpu_equivalents\":" << warmup_equivalents
                   << ",\"warmup_normalized_utilization_percent\":"
                   << normalized_utilization(warmup_equivalents, options.workers)
                   << ",\"process_start_boottime_ns\":" << process_start_boottime_ns
                   << ",\"ready_boottime_ns\":" << ready_boottime_ns
                   << '}';
        write_text_file(options.ready_file, ready_json.str());
        std::cout << "RS_CPU_NOISE_READY " << ready_json.str() << std::endl;

        while (!stop_requested)
            std::this_thread::sleep_for(std::chrono::milliseconds(20));

        join_all(threads);
        const auto process_end = std::chrono::steady_clock::now();
        const double cpu_end = process_cpu_seconds();
        const double measurement_wall_seconds = std::chrono::duration<double>(
            process_end - ready_time).count();
        const double measurement_cpu_seconds = cpu_end - ready_cpu;
        const double measurement_equivalents = cpu_equivalents(
            measurement_cpu_seconds, measurement_wall_seconds);
        const uint64_t end_boottime_ns = boottime_ns();
        const uint64_t total_iterations = std::accumulate(
            iterations.begin(), iterations.end(), uint64_t {0});
        const uint64_t checksum = std::accumulate(
            checksums.begin(), checksums.end(), uint64_t {0});

        std::ostringstream summary_json;
        summary_json << std::fixed << std::setprecision(6)
                     << "{\"schema_version\":1"
                     << ",\"mode\":\"busy_loop\""
                     << ",\"success\":true"
                     << ",\"workers\":" << options.workers
                     << ",\"working_set\":\"register_only\""
                     << ",\"effective_cpu_affinity\":\"" << cpu_affinity << "\""
                     << ",\"operations_per_stop_check\":4096"
                     << ",\"measurement_wall_seconds\":" << measurement_wall_seconds
                     << ",\"measurement_process_cpu_seconds\":" << measurement_cpu_seconds
                     << ",\"measurement_cpu_equivalents\":" << measurement_equivalents
                     << ",\"measurement_normalized_utilization_percent\":"
                     << normalized_utilization(measurement_equivalents, options.workers)
                     << ",\"total_iterations\":" << total_iterations
                     << ",\"checksum\":" << checksum
                     << ",\"process_start_boottime_ns\":" << process_start_boottime_ns
                     << ",\"ready_boottime_ns\":" << ready_boottime_ns
                     << ",\"end_boottime_ns\":" << end_boottime_ns
                     << '}';
        write_text_file(options.summary_output, summary_json.str());
        std::cout << "RS_CPU_NOISE_RESULT " << summary_json.str() << std::endl;
        return 0;
    }
    catch (const std::exception& error)
    {
        stop_requested = 1;
        join_all(threads);
        std::cerr << "RS_CPU_NOISE_ERROR {\"message\":\""
                  << error.what() << "\"}" << std::endl;
        return 2;
    }
}
