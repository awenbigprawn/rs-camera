#include <algorithm>
#include <cerrno>
#include <chrono>
#include <csignal>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <linux/fs.h>
#include <sstream>
#include <stdexcept>
#include <string>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <sys/types.h>
#include <unistd.h>
#include <grp.h>
#include <vector>

namespace
{
volatile std::sig_atomic_t stop_requested = 0;

void handle_signal(int)
{
    stop_requested = 1;
}

struct Options
{
    std::string device;
    std::string ready_file;
    std::string summary_output;
    size_t block_size = 1024 * 1024;
    double warmup_seconds = 10.0;
    uid_t drop_uid = static_cast<uid_t>(-1);
    gid_t drop_gid = static_cast<gid_t>(-1);
};

struct PhaseStats
{
    uint64_t bytes = 0;
    uint64_t read_operations = 0;
    uint64_t wraps = 0;
    std::vector<double> read_latencies_ms;
};

class FileDescriptor
{
public:
    explicit FileDescriptor(int descriptor) : descriptor_(descriptor) {}
    ~FileDescriptor()
    {
        if (descriptor_ >= 0)
            close(descriptor_);
    }
    FileDescriptor(const FileDescriptor&) = delete;
    FileDescriptor& operator=(const FileDescriptor&) = delete;
    int get() const { return descriptor_; }

private:
    int descriptor_;
};

class AlignedBuffer
{
public:
    AlignedBuffer(size_t alignment, size_t size)
    {
        const int result = posix_memalign(&data_, alignment, size);
        if (result != 0)
            throw std::runtime_error("posix_memalign failed: " + std::string(strerror(result)));
    }
    ~AlignedBuffer() { free(data_); }
    AlignedBuffer(const AlignedBuffer&) = delete;
    AlignedBuffer& operator=(const AlignedBuffer&) = delete;
    void* data() { return data_; }

private:
    void* data_ = nullptr;
};

std::string json_escape(const std::string& value)
{
    std::ostringstream output;
    for (const unsigned char character : value)
    {
        switch (character)
        {
        case '"': output << "\\\""; break;
        case '\\': output << "\\\\"; break;
        case '\b': output << "\\b"; break;
        case '\f': output << "\\f"; break;
        case '\n': output << "\\n"; break;
        case '\r': output << "\\r"; break;
        case '\t': output << "\\t"; break;
        default:
            if (character < 0x20)
            {
                output << "\\u" << std::hex << std::setw(4) << std::setfill('0')
                       << static_cast<int>(character) << std::dec;
            }
            else
            {
                output << character;
            }
        }
    }
    return output.str();
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

double percentile(std::vector<double> values, double fraction)
{
    if (values.empty())
        return 0.0;
    std::sort(values.begin(), values.end());
    const double position = fraction * static_cast<double>(values.size() - 1);
    const size_t lower = static_cast<size_t>(position);
    const size_t upper = std::min(lower + 1, values.size() - 1);
    const double weight = position - static_cast<double>(lower);
    return values[lower] * (1.0 - weight) + values[upper] * weight;
}

double mean(const std::vector<double>& values)
{
    if (values.empty())
        return 0.0;
    double total = 0.0;
    for (const double value : values)
        total += value;
    return total / static_cast<double>(values.size());
}

Options parse_options(int argc, char** argv)
{
    Options options;
    for (int index = 1; index < argc; ++index)
    {
        const std::string argument = argv[index];
        auto require_value = [&]() -> std::string {
            if (++index >= argc)
                throw std::runtime_error("missing value after " + argument);
            return argv[index];
        };

        if (argument == "--device")
            options.device = require_value();
        else if (argument == "--ready-file")
            options.ready_file = require_value();
        else if (argument == "--summary-output")
            options.summary_output = require_value();
        else if (argument == "--block-size-kib")
            options.block_size = std::stoull(require_value()) * 1024;
        else if (argument == "--warmup-seconds")
            options.warmup_seconds = std::stod(require_value());
        else if (argument == "--drop-uid")
            options.drop_uid = static_cast<uid_t>(std::stoul(require_value()));
        else if (argument == "--drop-gid")
            options.drop_gid = static_cast<gid_t>(std::stoul(require_value()));
        else if (argument == "--help" || argument == "-h")
        {
            std::cout
                << "Usage: realsense_usb_storage_noise [OPTIONS]\n"
                << "Continuously read an unmounted USB block device sequentially with O_DIRECT.\n\n"
                << "Options:\n"
                << "  --device PATH            whole USB block device (required)\n"
                << "  --ready-file PATH        write JSON after read warm-up (required)\n"
                << "  --summary-output PATH    write final JSON on shutdown (required)\n"
                << "  --block-size-kib N       direct-read block size (default: 1024)\n"
                << "  --warmup-seconds N       seconds before ready (default: 10)\n"
                << "  --drop-uid UID           drop root privileges after opening device\n"
                << "  --drop-gid GID           drop root privileges after opening device\n";
            std::exit(0);
        }
        else
            throw std::runtime_error("unknown argument: " + argument);
    }

    if (options.device.empty())
        throw std::runtime_error("--device is required");
    if (options.ready_file.empty())
        throw std::runtime_error("--ready-file is required");
    if (options.summary_output.empty())
        throw std::runtime_error("--summary-output is required");
    if (options.block_size < 4096 || options.block_size > 64 * 1024 * 1024)
        throw std::runtime_error("block size must be between 4 KiB and 64 MiB");
    if (options.warmup_seconds <= 0.0)
        throw std::runtime_error("warm-up duration must be positive");
    if ((options.drop_uid == static_cast<uid_t>(-1))
        != (options.drop_gid == static_cast<gid_t>(-1)))
    {
        throw std::runtime_error("--drop-uid and --drop-gid must be specified together");
    }
    return options;
}

void drop_privileges(uid_t uid, gid_t gid)
{
    if (uid == static_cast<uid_t>(-1))
        return;
    if (geteuid() != 0)
    {
        if (geteuid() != uid || getegid() != gid)
            throw std::runtime_error("cannot change UID/GID without root privileges");
        return;
    }
    if (setgroups(0, nullptr) != 0)
        throw std::runtime_error("setgroups failed: " + std::string(strerror(errno)));
    if (setgid(gid) != 0)
        throw std::runtime_error("setgid failed: " + std::string(strerror(errno)));
    if (setuid(uid) != 0)
        throw std::runtime_error("setuid failed: " + std::string(strerror(errno)));
}

bool read_one_block(
    int descriptor,
    void* buffer,
    size_t block_size,
    PhaseStats& stats)
{
    const auto begin = std::chrono::steady_clock::now();
    const ssize_t bytes_read = read(descriptor, buffer, block_size);
    const int read_errno = errno;
    const auto end = std::chrono::steady_clock::now();
    if (bytes_read < 0)
    {
        if (read_errno == EINTR && stop_requested)
            return false;
        throw std::runtime_error("direct read failed: " + std::string(strerror(read_errno)));
    }
    if (bytes_read == 0)
    {
        if (lseek(descriptor, 0, SEEK_SET) < 0)
            throw std::runtime_error("cannot rewind block device: " + std::string(strerror(errno)));
        ++stats.wraps;
        return true;
    }

    stats.bytes += static_cast<uint64_t>(bytes_read);
    ++stats.read_operations;
    stats.read_latencies_ms.push_back(
        std::chrono::duration<double, std::milli>(end - begin).count());
    return true;
}

std::string canonical_path(const std::string& path)
{
    char* resolved = realpath(path.c_str(), nullptr);
    if (resolved == nullptr)
        throw std::runtime_error("cannot resolve device path: " + std::string(strerror(errno)));
    const std::string result(resolved);
    free(resolved);
    return result;
}
} // namespace

int main(int argc, char** argv)
{
    try
    {
        const Options options = parse_options(argc, argv);
        std::signal(SIGINT, handle_signal);
        std::signal(SIGTERM, handle_signal);

        const std::string resolved_device = canonical_path(options.device);
        FileDescriptor descriptor(open(resolved_device.c_str(), O_RDONLY | O_DIRECT | O_CLOEXEC));
        if (descriptor.get() < 0)
            throw std::runtime_error("cannot open block device read-only: " + std::string(strerror(errno)));

        struct stat device_stat {};
        if (fstat(descriptor.get(), &device_stat) != 0)
            throw std::runtime_error("fstat failed: " + std::string(strerror(errno)));
        if (!S_ISBLK(device_stat.st_mode))
            throw std::runtime_error("device is not a block device: " + resolved_device);

        uint64_t device_bytes = 0;
        if (ioctl(descriptor.get(), BLKGETSIZE64, &device_bytes) != 0)
            throw std::runtime_error("BLKGETSIZE64 failed: " + std::string(strerror(errno)));
        int logical_block_size = 0;
        if (ioctl(descriptor.get(), BLKSSZGET, &logical_block_size) != 0)
            throw std::runtime_error("BLKSSZGET failed: " + std::string(strerror(errno)));
        if (logical_block_size <= 0 || options.block_size % logical_block_size != 0)
            throw std::runtime_error("direct-read size is not aligned to the logical block size");

        const long page_size = sysconf(_SC_PAGESIZE);
        const size_t alignment = std::max(
            static_cast<size_t>(page_size > 0 ? page_size : 4096),
            static_cast<size_t>(logical_block_size));
        AlignedBuffer buffer(alignment, options.block_size);
        (void)posix_fadvise(descriptor.get(), 0, 0, POSIX_FADV_SEQUENTIAL);
        drop_privileges(options.drop_uid, options.drop_gid);

        PhaseStats warmup;
        const auto process_begin = std::chrono::steady_clock::now();
        while (!stop_requested)
        {
            const double elapsed = std::chrono::duration<double>(
                std::chrono::steady_clock::now() - process_begin).count();
            if (elapsed >= options.warmup_seconds)
                break;
            if (!read_one_block(
                    descriptor.get(), buffer.data(), options.block_size, warmup))
                break;
        }
        if (stop_requested)
            throw std::runtime_error("terminated during USB read warm-up");

        const auto ready_time = std::chrono::steady_clock::now();
        const double startup_seconds = std::chrono::duration<double>(
            ready_time - process_begin).count();
        const double warmup_mib_per_second =
            static_cast<double>(warmup.bytes) / (1024.0 * 1024.0) / startup_seconds;

        std::ostringstream ready_json;
        ready_json << std::fixed << std::setprecision(6)
                   << "{\"schema_version\":1"
                   << ",\"mode\":\"sequential_read\""
                   << ",\"ready\":true"
                   << ",\"access\":\"read_only\""
                   << ",\"direct_io\":true"
                   << ",\"device\":\"" << json_escape(options.device) << "\""
                   << ",\"resolved_device\":\"" << json_escape(resolved_device) << "\""
                   << ",\"device_major\":" << major(device_stat.st_rdev)
                   << ",\"device_minor\":" << minor(device_stat.st_rdev)
                   << ",\"device_bytes\":" << device_bytes
                   << ",\"logical_block_size\":" << logical_block_size
                   << ",\"read_block_size\":" << options.block_size
                   << ",\"warmup_seconds\":" << startup_seconds
                   << ",\"warmup_bytes\":" << warmup.bytes
                   << ",\"warmup_read_operations\":" << warmup.read_operations
                   << ",\"warmup_mib_per_second\":" << warmup_mib_per_second
                   << '}';
        write_text_file(options.ready_file, ready_json.str());
        std::cout << "RS_USB_STORAGE_NOISE_READY " << ready_json.str() << std::endl;

        PhaseStats measurement;
        while (!stop_requested)
        {
            if (!read_one_block(
                    descriptor.get(), buffer.data(), options.block_size, measurement))
                break;
        }
        const auto process_end = std::chrono::steady_clock::now();
        const double duration_seconds = std::chrono::duration<double>(
            process_end - ready_time).count();
        const double mib_per_second = duration_seconds > 0.0
            ? static_cast<double>(measurement.bytes) / (1024.0 * 1024.0) / duration_seconds
            : 0.0;
        const double minimum = measurement.read_latencies_ms.empty()
            ? 0.0
            : *std::min_element(
                measurement.read_latencies_ms.begin(), measurement.read_latencies_ms.end());
        const double maximum = measurement.read_latencies_ms.empty()
            ? 0.0
            : *std::max_element(
                measurement.read_latencies_ms.begin(), measurement.read_latencies_ms.end());

        std::ostringstream summary_json;
        summary_json << std::fixed << std::setprecision(6)
                     << "{\"schema_version\":1"
                     << ",\"mode\":\"sequential_read\""
                     << ",\"success\":true"
                     << ",\"access\":\"read_only\""
                     << ",\"direct_io\":true"
                     << ",\"device\":\"" << json_escape(options.device) << "\""
                     << ",\"resolved_device\":\"" << json_escape(resolved_device) << "\""
                     << ",\"device_major\":" << major(device_stat.st_rdev)
                     << ",\"device_minor\":" << minor(device_stat.st_rdev)
                     << ",\"device_bytes\":" << device_bytes
                     << ",\"logical_block_size\":" << logical_block_size
                     << ",\"read_block_size\":" << options.block_size
                     << ",\"warmup_seconds\":" << startup_seconds
                     << ",\"warmup_bytes\":" << warmup.bytes
                     << ",\"measurement_seconds\":" << duration_seconds
                     << ",\"bytes_read\":" << measurement.bytes
                     << ",\"read_operations\":" << measurement.read_operations
                     << ",\"device_wraps\":" << measurement.wraps
                     << ",\"mib_per_second\":" << mib_per_second
                     << ",\"read_ms_min\":" << minimum
                     << ",\"read_ms_mean\":" << mean(measurement.read_latencies_ms)
                     << ",\"read_ms_p99\":" << percentile(measurement.read_latencies_ms, 0.99)
                     << ",\"read_ms_p999\":" << percentile(measurement.read_latencies_ms, 0.999)
                     << ",\"read_ms_max\":" << maximum
                     << '}';
        write_text_file(options.summary_output, summary_json.str());
        std::cout << "RS_USB_STORAGE_NOISE_RESULT " << summary_json.str() << std::endl;
        return 0;
    }
    catch (const std::exception& error)
    {
        std::cerr << "RS_USB_STORAGE_NOISE_ERROR {\"message\":\""
                  << json_escape(error.what()) << "\"}" << std::endl;
        return 2;
    }
}
