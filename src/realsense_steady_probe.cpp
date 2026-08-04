#include "rs_camera/deadline_scheduler.h"
#include "rs_camera/trace_marker.h"

#include <librealsense2/rs.hpp>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdint>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <map>
#include <memory>
#include <mutex>
#include <pthread.h>
#include <sched.h>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

namespace
{
struct options
{
    std::vector<std::string> serials;
    std::string stream_mode = "d435_all";
    std::string delivery = "wait";
    std::string summary_output;
    std::string events_output;
    std::string deadline_profile;
    std::string rate_monotonic_profile;
    std::string rate_monotonic_policy;
    std::string warmup_ready_file;
    std::string measurement_start_gate;
    int scheduler_apply_after_frames = 0;
    int rate_monotonic_highest_priority = 80;
    int camera_count = 1;
    int depth_width = 848;
    int depth_height = 480;
    int color_width = 640;
    int color_height = 480;
    int fps = 30;
    int frames = 10000;
    int warmup_frames = 30;
    int frame_timeout_ms = 1500;
    int startup_timeout_ms = 15000;
    int measurement_duration_ms = 0;
    int measurement_timeout_ms = 0;
    int measurement_gate_timeout_ms = 0;
};

struct stats
{
    size_t n = 0;
    double min = 0.0;
    double max = 0.0;
    double mean = 0.0;
    double stddev = 0.0;
    double p50 = 0.0;
    double p90 = 0.0;
    double p99 = 0.0;
    double p999 = 0.0;
};

struct frame_event
{
    uint64_t host_boottime_ns = 0;
    uint64_t delivery = 0;
    std::string stream;
    int stream_index = 0;
    uint64_t frame_number = 0;
    double sensor_timestamp_ms = 0.0;
    std::string timestamp_domain;
};

struct stream_metrics
{
    uint64_t frames = 0;
    uint64_t drops = 0;
    bool has_last = false;
    uint64_t last_frame_number = 0;
    double last_sensor_timestamp_ms = 0.0;
    uint64_t last_host_ns = 0;
    std::string timestamp_domain;
    std::vector<double> sensor_interarrival_ms;
    std::vector<double> host_interarrival_ms;
};

struct camera_metrics
{
    std::mutex mutex;
    uint64_t warmup_deliveries = 0;
    uint64_t deliveries = 0;
    uint64_t frames = 0;
    uint64_t timeouts = 0;
    uint64_t pre_measurement_timeouts = 0;
    uint64_t measurement_timeouts = 0;
    uint64_t start_begin_ns = 0;
    uint64_t start_end_ns = 0;
    uint64_t stop_begin_ns = 0;
    uint64_t stop_end_ns = 0;
    uint64_t first_warmup_ns = 0;
    uint64_t first_measured_ns = 0;
    uint64_t last_measured_ns = 0;
    uint64_t last_delivery_ns = 0;
    bool scheduler_ready = false;
    bool warmed = false;
    bool completed = false;
    std::vector<double> delivery_interarrival_ms;
    std::vector<double> wait_ms;
    std::map<std::string, stream_metrics> streams;
    std::vector<frame_event> events;
};

struct frame_freshness_metrics
{
    uint64_t observed_frames = 0;
    uint64_t unique_frames = 0;
    uint64_t duplicate_frames = 0;
    uint64_t sequence_gaps = 0;
    uint64_t nonadvancing_frames = 0;
    uint64_t out_of_order_frames = 0;
};

struct delivery_freshness_metrics
{
    uint64_t fully_fresh_framesets = 0;
    uint64_t partially_stale_framesets = 0;
    uint64_t stale_framesets = 0;
};

struct camera_freshness_metrics
{
    frame_freshness_metrics frames;
    delivery_freshness_metrics deliveries;
    std::map<std::string, frame_freshness_metrics> streams;
};

struct camera_runtime
{
    explicit camera_runtime(rs2::context &context) : pipe(context) {}

    std::string serial;
    std::string name;
    std::string firmware;
    std::string physical_port;
    std::string usb_type;
    rs2::pipeline pipe;
    rs2::config config;
    rs2::pipeline_profile profile;
    camera_metrics metrics;
    std::thread wait_thread;
};

struct shared_control
{
    std::mutex mutex;
    std::condition_variable cv;
    std::atomic<bool> measurement_enabled{false};
    std::atomic<uint64_t> measurement_deadline_ns{0};
    std::atomic<bool> noise_transition_active{false};
    std::atomic<bool> stop{false};
    std::atomic<uint64_t> origin_ns{0};
    size_t scheduler_ready_cameras = 0;
    size_t warmed_cameras = 0;
    size_t completed_cameras = 0;
    std::atomic<bool> failed{false};
    std::string error;
};

options parse_args(int argc, char **argv)
{
    options opts;
    for (int i = 1; i < argc; ++i)
    {
        const std::string arg = argv[i];
        auto value = [&](const std::string &name) {
            if (i + 1 >= argc)
                throw std::runtime_error("Missing value for " + name);
            return std::string(argv[++i]);
        };

        if (arg == "--serial")
            opts.serials.push_back(value(arg));
        else if (arg == "--camera-count")
            opts.camera_count = std::stoi(value(arg));
        else if (arg == "--stream-mode")
            opts.stream_mode = value(arg);
        else if (arg == "--delivery")
            opts.delivery = value(arg);
        else if (arg == "--frames")
            opts.frames = std::stoi(value(arg));
        else if (arg == "--warmup-frames")
            opts.warmup_frames = std::stoi(value(arg));
        else if (arg == "--frame-timeout-ms")
            opts.frame_timeout_ms = std::stoi(value(arg));
        else if (arg == "--startup-timeout-ms")
            opts.startup_timeout_ms = std::stoi(value(arg));
        else if (arg == "--measurement-duration-ms")
            opts.measurement_duration_ms = std::stoi(value(arg));
        else if (arg == "--measurement-timeout-ms")
            opts.measurement_timeout_ms = std::stoi(value(arg));
        else if (arg == "--fps")
            opts.fps = std::stoi(value(arg));
        else if (arg == "--depth-width")
            opts.depth_width = std::stoi(value(arg));
        else if (arg == "--depth-height")
            opts.depth_height = std::stoi(value(arg));
        else if (arg == "--color-width")
            opts.color_width = std::stoi(value(arg));
        else if (arg == "--color-height")
            opts.color_height = std::stoi(value(arg));
        else if (arg == "--summary-output")
            opts.summary_output = value(arg);
        else if (arg == "--events-output")
            opts.events_output = value(arg);
        else if (arg == "--deadline-profile")
            opts.deadline_profile = value(arg);
        else if (arg == "--deadline-apply-after-frames")
            opts.scheduler_apply_after_frames = std::stoi(value(arg));
        else if (arg == "--scheduler-apply-after-frames")
            opts.scheduler_apply_after_frames = std::stoi(value(arg));
        else if (arg == "--rate-monotonic-profile")
            opts.rate_monotonic_profile = value(arg);
        else if (arg == "--rate-monotonic-policy")
            opts.rate_monotonic_policy = value(arg);
        else if (arg == "--rate-monotonic-highest-priority")
            opts.rate_monotonic_highest_priority = std::stoi(value(arg));
        else if (arg == "--warmup-ready-file")
            opts.warmup_ready_file = value(arg);
        else if (arg == "--measurement-start-gate")
            opts.measurement_start_gate = value(arg);
        else if (arg == "--measurement-gate-timeout-ms")
            opts.measurement_gate_timeout_ms = std::stoi(value(arg));
        else if (arg == "--help" || arg == "-h")
        {
            std::cout
                << "Usage: " << argv[0] << " [options]\n"
                << "  --serial SERIAL              Repeat for each selected camera\n"
                << "  --camera-count N             Select the first N cameras when serials are omitted\n"
                << "  --stream-mode depth|depth_color|d435_all\n"
                << "  --delivery wait|callback\n"
                << "  --frames N                   Measured deliveries per camera\n"
                << "  --measurement-duration-ms N Measure for a fixed wall-clock duration; zero uses --frames\n"
                << "  --warmup-frames N\n"
                << "  --frame-timeout-ms N --startup-timeout-ms N\n"
                << "  --measurement-timeout-ms N   Zero selects an automatic deadline\n"
                << "  --fps N\n"
                << "  --depth-width N --depth-height N\n"
                << "  --color-width N --color-height N\n"
                << "  --summary-output PATH --events-output PATH\n"
                << "  --deadline-profile PATH       Apply per-thread SCHED_DEADLINE during warm-up\n"
                << "  --rate-monotonic-profile PATH Apply per-thread fixed-priority scheduling during warm-up\n"
                << "  --rate-monotonic-policy rr|fifo\n"
                << "  --rate-monotonic-highest-priority N\n"
                << "  --scheduler-apply-after-frames N  Per-camera pre-scheduler warm-up; zero is automatic\n"
                << "  --deadline-apply-after-frames N  Compatibility alias for the previous option\n"
                << "  --warmup-ready-file PATH      Signal that every camera is warm\n"
                << "  --measurement-start-gate PATH Wait for this file before measuring\n"
                << "  --measurement-gate-timeout-ms N  Maximum gate wait\n";
            std::exit(0);
        }
        else
            throw std::runtime_error("Unknown argument: " + arg);
    }

    if (!opts.serials.empty())
        opts.camera_count = static_cast<int>(opts.serials.size());
    if (opts.camera_count <= 0 || opts.frames <= 0 || opts.fps <= 0)
        throw std::runtime_error("camera-count, frames, and fps must be positive");
    if (opts.measurement_duration_ms < 0)
        throw std::runtime_error("measurement-duration-ms must be non-negative");
    if (opts.measurement_duration_ms > 0 && opts.measurement_timeout_ms > 0)
        throw std::runtime_error(
            "measurement-duration-ms and measurement-timeout-ms are mutually exclusive");
    if (opts.warmup_frames < 0 || opts.frame_timeout_ms <= 0 || opts.startup_timeout_ms <= 0)
        throw std::runtime_error("Invalid warm-up or timeout value");
    if (opts.delivery != "wait" && opts.delivery != "callback")
        throw std::runtime_error("Unsupported delivery mode: " + opts.delivery);
    if (opts.scheduler_apply_after_frames < 0)
        throw std::runtime_error("scheduler-apply-after-frames must be non-negative");
    const bool has_warmup_file = !opts.warmup_ready_file.empty();
    const bool has_measurement_gate = !opts.measurement_start_gate.empty();
    if (has_warmup_file != has_measurement_gate)
        throw std::runtime_error(
            "warmup-ready-file and measurement-start-gate must be used together");
    if (has_measurement_gate && opts.measurement_gate_timeout_ms <= 0)
        throw std::runtime_error(
            "measurement-gate-timeout-ms must be positive when a gate is used");
    const bool deadline_requested = !opts.deadline_profile.empty();
    const bool rate_monotonic_profile_requested =
        !opts.rate_monotonic_profile.empty();
    const bool rate_monotonic_policy_requested =
        !opts.rate_monotonic_policy.empty();
    if (deadline_requested &&
        (rate_monotonic_profile_requested || rate_monotonic_policy_requested))
        throw std::runtime_error(
            "SCHED_DEADLINE and rate-monotonic scheduling are mutually exclusive");
    if (rate_monotonic_profile_requested != rate_monotonic_policy_requested)
        throw std::runtime_error(
            "rate-monotonic-profile and rate-monotonic-policy must be used together");
    if (rate_monotonic_policy_requested && opts.rate_monotonic_policy != "rr" &&
        opts.rate_monotonic_policy != "fifo")
        throw std::runtime_error("rate-monotonic-policy must be rr or fifo");
    if (rate_monotonic_policy_requested &&
        opts.rate_monotonic_highest_priority <= 0)
        throw std::runtime_error(
            "rate-monotonic-highest-priority must be positive");
    if (deadline_requested || rate_monotonic_profile_requested)
    {
        if (opts.warmup_frames < 2)
            throw std::runtime_error(
                "Modeled scheduling requires at least two warm-up deliveries");
        if (opts.scheduler_apply_after_frames >= opts.warmup_frames)
            throw std::runtime_error(
                "scheduler-apply-after-frames must be less than warmup-frames");
    }
    return opts;
}

std::string json_escape(const std::string &value)
{
    std::ostringstream out;
    for (unsigned char c : value)
    {
        switch (c)
        {
        case '\\': out << "\\\\"; break;
        case '"': out << "\\\""; break;
        case '\n': out << "\\n"; break;
        case '\r': out << "\\r"; break;
        case '\t': out << "\\t"; break;
        default:
            if (c < 0x20)
                out << "\\u" << std::hex << std::setw(4) << std::setfill('0') << static_cast<int>(c);
            else
                out << static_cast<char>(c);
        }
    }
    return out.str();
}

std::string quoted(const std::string &value)
{
    return "\"" + json_escape(value) + "\"";
}

void set_failure(shared_control &control, const std::string &message);

void write_atomic_timestamp(const std::string &path, uint64_t timestamp_ns)
{
    const std::filesystem::path destination(path);
    const std::filesystem::path temporary = destination.string() + ".tmp";
    {
        std::ofstream out(temporary);
        if (!out)
            throw std::runtime_error("Cannot create transition file: " + path);
        out << timestamp_ns << "\n";
    }
    std::error_code error;
    std::filesystem::rename(temporary, destination, error);
    if (error)
    {
        std::filesystem::remove(temporary);
        throw std::runtime_error(
            "Cannot publish transition file " + path + ": " + error.message());
    }
}

std::string read_text(const std::filesystem::path &path)
{
    std::ifstream in(path);
    std::ostringstream contents;
    contents << in.rdbuf();
    return contents.str();
}

uint64_t wait_for_measurement_gate(const options &opts, shared_control &control)
{
    const std::filesystem::path gate(opts.measurement_start_gate);
    const std::filesystem::path error_path(opts.measurement_start_gate + ".error");
    const auto deadline = std::chrono::steady_clock::now() +
                          std::chrono::milliseconds(opts.measurement_gate_timeout_ms);
    while (!control.failed.load())
    {
        if (std::filesystem::exists(error_path))
        {
            std::string detail = read_text(error_path);
            while (!detail.empty() && (detail.back() == '\n' || detail.back() == '\r'))
                detail.pop_back();
            set_failure(
                control,
                "Noise setup failed: " +
                    (detail.empty() ? std::string("unknown error") : detail));
            return 0;
        }
        if (std::filesystem::exists(gate))
        {
            std::ifstream in(gate);
            uint64_t timestamp_ns = 0;
            if (in >> timestamp_ns && timestamp_ns > 0)
                return timestamp_ns;
            set_failure(control, "Noise setup failed: invalid measurement gate");
            return 0;
        }
        if (std::chrono::steady_clock::now() >= deadline)
        {
            set_failure(control, "Noise setup failed: measurement gate timed out");
            return 0;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    return 0;
}

std::string csv_field(const std::string &value)
{
    if (value.find_first_of(",\"\n\r") == std::string::npos)
        return value;
    std::string escaped = "\"";
    for (char c : value)
    {
        if (c == '"')
            escaped += '"';
        escaped += c;
    }
    return escaped + '"';
}

stats summarize(std::vector<double> values)
{
    stats result;
    result.n = values.size();
    if (values.empty())
        return result;

    std::sort(values.begin(), values.end());
    result.min = values.front();
    result.max = values.back();
    double sum = 0.0;
    for (double value : values)
        sum += value;
    result.mean = sum / static_cast<double>(values.size());
    double squared = 0.0;
    for (double value : values)
        squared += (value - result.mean) * (value - result.mean);
    result.stddev = std::sqrt(squared / static_cast<double>(values.size()));

    auto percentile = [&](double p) {
        if (values.size() == 1)
            return values.front();
        const double position = p * static_cast<double>(values.size() - 1);
        const auto low = static_cast<size_t>(std::floor(position));
        const auto high = static_cast<size_t>(std::ceil(position));
        return values[low] + (values[high] - values[low]) * (position - static_cast<double>(low));
    };
    result.p50 = percentile(0.5);
    result.p90 = percentile(0.9);
    result.p99 = percentile(0.99);
    result.p999 = percentile(0.999);
    return result;
}

void write_stats(std::ostream &out, const stats &value)
{
    out << "{\"n\":" << value.n
        << ",\"min\":" << value.min
        << ",\"max\":" << value.max
        << ",\"mean\":" << value.mean
        << ",\"stddev\":" << value.stddev
        << ",\"p50\":" << value.p50
        << ",\"p90\":" << value.p90
        << ",\"p99\":" << value.p99
        << ",\"p999\":" << value.p999 << "}";
}

double ns_to_ms(uint64_t ns)
{
    return static_cast<double>(ns) / 1000000.0;
}

std::string info(const rs2::device &device, rs2_camera_info key)
{
    return device.supports(key) ? device.get_info(key) : "unknown";
}

std::string stream_key(const rs2::frame &frame)
{
    const auto profile = frame.get_profile();
    return std::string(rs2_stream_to_string(profile.stream_type())) + "#" +
           std::to_string(profile.stream_index());
}

void configure_streams(rs2::config &config, const options &opts, const std::string &serial)
{
    config.enable_device(serial);
    config.enable_stream(
        RS2_STREAM_DEPTH, opts.depth_width, opts.depth_height, RS2_FORMAT_Z16, opts.fps);
    if (opts.stream_mode == "depth")
        return;
    if (opts.stream_mode == "depth_color" || opts.stream_mode == "d435_all")
    {
        config.enable_stream(
            RS2_STREAM_COLOR, opts.color_width, opts.color_height, RS2_FORMAT_RGB8, opts.fps);
    }
    else
        throw std::runtime_error("Unsupported stream mode: " + opts.stream_mode);
    if (opts.stream_mode == "d435_all")
    {
        config.enable_stream(RS2_STREAM_INFRARED,
                             1,
                             opts.depth_width,
                             opts.depth_height,
                             RS2_FORMAT_Y8,
                             opts.fps);
        config.enable_stream(RS2_STREAM_INFRARED,
                             2,
                             opts.depth_width,
                             opts.depth_height,
                             RS2_FORMAT_Y8,
                             opts.fps);
    }
}

void set_failure(shared_control &control, const std::string &message)
{
    {
        std::lock_guard<std::mutex> lock(control.mutex);
        if (!control.failed.load())
        {
            control.failed.store(true);
            control.error = message;
        }
    }
    control.stop.store(true);
    control.cv.notify_all();
}

void record_frame(camera_metrics &metrics,
                  const rs2::frame &frame,
                  uint64_t host_ns,
                  uint64_t delivery)
{
    if (!frame)
        return;

    const std::string key = stream_key(frame);
    auto &stream = metrics.streams[key];
    const uint64_t number = frame.get_frame_number();
    const double sensor_ms = frame.get_timestamp();
    if (stream.has_last)
    {
        if (number > stream.last_frame_number + 1)
            stream.drops += number - stream.last_frame_number - 1;
        stream.sensor_interarrival_ms.push_back(sensor_ms - stream.last_sensor_timestamp_ms);
        stream.host_interarrival_ms.push_back(ns_to_ms(host_ns - stream.last_host_ns));
    }
    stream.has_last = true;
    stream.last_frame_number = number;
    stream.last_sensor_timestamp_ms = sensor_ms;
    stream.last_host_ns = host_ns;
    stream.timestamp_domain = rs2_timestamp_domain_to_string(frame.get_frame_timestamp_domain());
    ++stream.frames;
    ++metrics.frames;
    metrics.events.push_back({host_ns,
                              delivery,
                              rs2_stream_to_string(frame.get_profile().stream_type()),
                              frame.get_profile().stream_index(),
                              number,
                              sensor_ms,
                              stream.timestamp_domain});
}

bool modeled_scheduler_requested(const options &opts)
{
    return !opts.deadline_profile.empty() ||
           !opts.rate_monotonic_profile.empty();
}

bool fixed_duration_measurement(const options &opts)
{
    return opts.measurement_duration_ms > 0;
}

int scheduler_apply_threshold(const options &opts)
{
    if (opts.scheduler_apply_after_frames > 0)
        return opts.scheduler_apply_after_frames;
    return std::min(opts.fps, std::max(1, opts.warmup_frames / 10));
}

void record_delivery(camera_runtime &camera,
                     const rs2::frame &frame,
                     uint64_t host_ns,
                     double wait_ms,
                     const options &opts,
                     shared_control &control)
{
    bool became_scheduler_ready = false;
    bool became_warm = false;
    bool became_complete = false;
    {
        std::lock_guard<std::mutex> lock(camera.metrics.mutex);
        auto &metrics = camera.metrics;
        if (metrics.completed)
            return;

        if (!metrics.warmed)
        {
            if (!metrics.first_warmup_ns)
                metrics.first_warmup_ns = host_ns;
            ++metrics.warmup_deliveries;
            if (modeled_scheduler_requested(opts) && !metrics.scheduler_ready &&
                metrics.warmup_deliveries >=
                    static_cast<uint64_t>(scheduler_apply_threshold(opts)))
            {
                metrics.scheduler_ready = true;
                became_scheduler_ready = true;
            }
            if (metrics.warmup_deliveries >= static_cast<uint64_t>(opts.warmup_frames))
            {
                metrics.warmed = true;
                became_warm = true;
            }
        }
        else if (control.measurement_enabled.load())
        {
            const uint64_t deadline_ns = control.measurement_deadline_ns.load();
            if (deadline_ns && host_ns > deadline_ns)
                return;
            ++metrics.deliveries;
            if (!metrics.first_measured_ns)
                metrics.first_measured_ns = host_ns;
            if (metrics.last_delivery_ns)
                metrics.delivery_interarrival_ms.push_back(
                    ns_to_ms(host_ns - metrics.last_delivery_ns));
            metrics.last_delivery_ns = host_ns;
            metrics.last_measured_ns = host_ns;
            if (wait_ms >= 0.0)
                metrics.wait_ms.push_back(wait_ms);

            if (frame.is<rs2::frameset>())
            {
                for (auto &&child : frame.as<rs2::frameset>())
                    record_frame(metrics, child, host_ns, metrics.deliveries);
            }
            else
                record_frame(metrics, frame, host_ns, metrics.deliveries);

            if (!fixed_duration_measurement(opts) &&
                metrics.deliveries >= static_cast<uint64_t>(opts.frames))
            {
                metrics.completed = true;
                became_complete = true;
            }
        }
    }

    if (became_scheduler_ready)
    {
        std::lock_guard<std::mutex> lock(control.mutex);
        ++control.scheduler_ready_cameras;
        control.cv.notify_all();
    }
    if (became_warm)
    {
        std::lock_guard<std::mutex> lock(control.mutex);
        ++control.warmed_cameras;
        control.cv.notify_all();
    }
    if (became_complete)
    {
        std::lock_guard<std::mutex> lock(control.mutex);
        ++control.completed_cameras;
        control.cv.notify_all();
    }
}

void wait_loop(camera_runtime &camera,
               size_t camera_index,
               const options &opts,
               shared_control &control)
{
    const std::string name = "rs-wait-" + std::to_string(camera_index);
    pthread_setname_np(pthread_self(), name.substr(0, 15).c_str());
    while (!control.stop.load())
    {
        const bool measurement_was_enabled = control.measurement_enabled.load();
        const uint64_t begin_ns = rs_trace_boottime_ns();
        try
        {
            const auto frames = camera.pipe.wait_for_frames(opts.frame_timeout_ms);
            const uint64_t end_ns = rs_trace_boottime_ns();
            record_delivery(
                camera, frames, end_ns, ns_to_ms(end_ns - begin_ns), opts, control);
            std::lock_guard<std::mutex> lock(camera.metrics.mutex);
            if (camera.metrics.completed)
                break;
        }
        catch (const rs2::error &error)
        {
            {
                std::lock_guard<std::mutex> lock(camera.metrics.mutex);
                ++camera.metrics.timeouts;
                if (measurement_was_enabled)
                    ++camera.metrics.measurement_timeouts;
                else
                    ++camera.metrics.pre_measurement_timeouts;
            }
            if (measurement_was_enabled)
            {
                if (!control.measurement_enabled.load() || control.stop.load())
                    break;
                continue;
            }
            const std::string prefix = control.noise_transition_active.load()
                                           ? "Noise transition frame failure: "
                                           : "";
            set_failure(control, prefix + camera.serial + ": " + error.what());
            break;
        }
    }
}

std::vector<std::string> select_serials(rs2::context &context, const options &opts)
{
    std::vector<std::string> available;
    for (auto &&device : context.query_devices())
    {
        if (device.supports(RS2_CAMERA_INFO_SERIAL_NUMBER))
            available.emplace_back(device.get_info(RS2_CAMERA_INFO_SERIAL_NUMBER));
    }
    std::sort(available.begin(), available.end());

    if (!opts.serials.empty())
    {
        for (const auto &serial : opts.serials)
        {
            if (std::find(available.begin(), available.end(), serial) == available.end())
                throw std::runtime_error("Requested camera is unavailable: " + serial);
        }
        return opts.serials;
    }
    if (available.size() < static_cast<size_t>(opts.camera_count))
        throw std::runtime_error("Requested " + std::to_string(opts.camera_count) +
                                 " cameras, but only " + std::to_string(available.size()) +
                                 " were found");
    available.resize(static_cast<size_t>(opts.camera_count));
    return available;
}

std::string scheduler_policy()
{
    switch (sched_getscheduler(0))
    {
    case SCHED_OTHER: return "SCHED_OTHER";
    case SCHED_RR: return "SCHED_RR";
    case SCHED_FIFO: return "SCHED_FIFO";
#ifdef SCHED_DEADLINE
    case SCHED_DEADLINE: return "SCHED_DEADLINE";
#endif
    default: return "UNKNOWN";
    }
}

uint64_t total_drops(const camera_metrics &metrics)
{
    uint64_t result = 0;
    for (const auto &entry : metrics.streams)
        result += entry.second.drops;
    return result;
}

camera_freshness_metrics analyze_freshness(const camera_metrics &metrics)
{
    struct working_stream
    {
        bool has_highest = false;
        uint64_t highest = 0;
        uint64_t nonadvancing_frames = 0;
        uint64_t out_of_order_frames = 0;
        std::vector<uint64_t> frame_numbers;
    };

    camera_freshness_metrics result;
    std::map<std::string, working_stream> streams;
    bool have_delivery = false;
    uint64_t current_delivery = 0;
    bool delivery_has_frame = false;
    bool delivery_has_advance = false;
    bool delivery_all_advance = true;

    auto finish_delivery = [&]() {
        if (!delivery_has_frame)
            return;
        if (!delivery_has_advance)
            ++result.deliveries.stale_framesets;
        else if (delivery_all_advance)
            ++result.deliveries.fully_fresh_framesets;
        else
            ++result.deliveries.partially_stale_framesets;
    };

    for (const auto &event : metrics.events)
    {
        if (!have_delivery || event.delivery != current_delivery)
        {
            if (have_delivery)
                finish_delivery();
            have_delivery = true;
            current_delivery = event.delivery;
            delivery_has_frame = false;
            delivery_has_advance = false;
            delivery_all_advance = true;
        }

        const std::string key = event.stream + "#" + std::to_string(event.stream_index);
        auto &stream = streams[key];
        const bool advanced = !stream.has_highest || event.frame_number > stream.highest;
        if (advanced)
        {
            stream.has_highest = true;
            stream.highest = event.frame_number;
        }
        else
        {
            ++stream.nonadvancing_frames;
            if (event.frame_number < stream.highest)
                ++stream.out_of_order_frames;
        }
        stream.frame_numbers.push_back(event.frame_number);
        delivery_has_frame = true;
        delivery_has_advance = delivery_has_advance || advanced;
        delivery_all_advance = delivery_all_advance && advanced;
    }
    if (have_delivery)
        finish_delivery();

    for (auto &entry : streams)
    {
        auto &working = entry.second;
        auto &numbers = working.frame_numbers;
        std::sort(numbers.begin(), numbers.end());
        const auto unique_end = std::unique(numbers.begin(), numbers.end());
        const uint64_t observed = numbers.size();
        const uint64_t unique = static_cast<uint64_t>(
            std::distance(numbers.begin(), unique_end));

        frame_freshness_metrics stream_result;
        stream_result.observed_frames = observed;
        stream_result.unique_frames = unique;
        stream_result.duplicate_frames = observed - unique;
        stream_result.nonadvancing_frames = working.nonadvancing_frames;
        stream_result.out_of_order_frames = working.out_of_order_frames;
        if (unique > 0)
        {
            const uint64_t minimum = numbers.front();
            const uint64_t maximum = *(unique_end - 1);
            stream_result.sequence_gaps = maximum - minimum + 1 - unique;
        }
        result.streams.emplace(entry.first, stream_result);

        result.frames.observed_frames += stream_result.observed_frames;
        result.frames.unique_frames += stream_result.unique_frames;
        result.frames.duplicate_frames += stream_result.duplicate_frames;
        result.frames.sequence_gaps += stream_result.sequence_gaps;
        result.frames.nonadvancing_frames += stream_result.nonadvancing_frames;
        result.frames.out_of_order_frames += stream_result.out_of_order_frames;
    }
    return result;
}

void add_freshness(camera_freshness_metrics &destination,
                   const camera_freshness_metrics &source)
{
    destination.frames.observed_frames += source.frames.observed_frames;
    destination.frames.unique_frames += source.frames.unique_frames;
    destination.frames.duplicate_frames += source.frames.duplicate_frames;
    destination.frames.sequence_gaps += source.frames.sequence_gaps;
    destination.frames.nonadvancing_frames += source.frames.nonadvancing_frames;
    destination.frames.out_of_order_frames += source.frames.out_of_order_frames;
    destination.deliveries.fully_fresh_framesets +=
        source.deliveries.fully_fresh_framesets;
    destination.deliveries.partially_stale_framesets +=
        source.deliveries.partially_stale_framesets;
    destination.deliveries.stale_framesets += source.deliveries.stale_framesets;
}

void write_frame_freshness(std::ostream &out, const frame_freshness_metrics &metrics)
{
    out << "\"observed_frames\":" << metrics.observed_frames
        << ",\"unique_frames\":" << metrics.unique_frames
        << ",\"duplicate_frames\":" << metrics.duplicate_frames
        << ",\"sequence_gaps\":" << metrics.sequence_gaps
        << ",\"nonadvancing_frames\":" << metrics.nonadvancing_frames
        << ",\"out_of_order_frames\":" << metrics.out_of_order_frames;
}

void write_delivery_freshness(std::ostream &out,
                              const delivery_freshness_metrics &metrics)
{
    out << "\"fully_fresh_framesets\":" << metrics.fully_fresh_framesets
        << ",\"partially_stale_framesets\":"
        << metrics.partially_stale_framesets
        << ",\"stale_framesets\":" << metrics.stale_framesets;
}

void write_active_streams(std::ostream &out, const rs2::pipeline_profile &profile)
{
    out << "[";
    bool first = true;
    for (auto &&stream : profile.get_streams())
    {
        if (!first)
            out << ",";
        first = false;
        out << "{\"stream\":" << quoted(rs2_stream_to_string(stream.stream_type()))
            << ",\"index\":" << stream.stream_index()
            << ",\"format\":" << quoted(rs2_format_to_string(stream.format()))
            << ",\"fps\":" << stream.fps();
        if (stream.is<rs2::video_stream_profile>())
        {
            const auto video = stream.as<rs2::video_stream_profile>();
            out << ",\"width\":" << video.width() << ",\"height\":" << video.height();
        }
        out << "}";
    }
    out << "]";
}

void write_summary(const std::string &path,
                   const options &opts,
                   const std::vector<std::unique_ptr<camera_runtime>> &cameras,
                   uint64_t warmup_ready_ns,
                   uint64_t measurement_gate_open_ns,
                   uint64_t measurement_start_ns,
                   uint64_t measurement_end_ns,
                   bool success,
                   const std::string &error,
                   const rs_camera::deadline_application *deadline_result,
                   const rs_camera::rate_monotonic_application *rate_monotonic_result)
{
    rs_trace_phase_marker("freshness_analysis_begin");
    const auto freshness_begin = std::chrono::steady_clock::now();
    std::vector<camera_freshness_metrics> camera_freshness;
    camera_freshness.reserve(cameras.size());
    camera_freshness_metrics aggregate_freshness;
    for (const auto &camera : cameras)
    {
        std::lock_guard<std::mutex> lock(camera->metrics.mutex);
        camera_freshness.push_back(analyze_freshness(camera->metrics));
        add_freshness(aggregate_freshness, camera_freshness.back());
    }
    const double freshness_analysis_ms = std::chrono::duration<double, std::milli>(
        std::chrono::steady_clock::now() - freshness_begin).count();
    rs_trace_phase_marker("freshness_analysis_end");

    std::ofstream out(path);
    if (!out)
        throw std::runtime_error("Cannot open summary output: " + path);
    out << std::fixed << std::setprecision(6);

    uint64_t deliveries = 0;
    uint64_t frames = 0;
    uint64_t drops = 0;
    uint64_t timeouts = 0;
    uint64_t pre_measurement_timeouts = 0;
    uint64_t measurement_timeouts = 0;
    uint64_t raw_events = 0;
    std::vector<double> delivery_gaps;
    std::vector<double> wait_times;
    for (const auto &camera : cameras)
    {
        std::lock_guard<std::mutex> lock(camera->metrics.mutex);
        deliveries += camera->metrics.deliveries;
        frames += camera->metrics.frames;
        drops += total_drops(camera->metrics);
        timeouts += camera->metrics.timeouts;
        pre_measurement_timeouts += camera->metrics.pre_measurement_timeouts;
        measurement_timeouts += camera->metrics.measurement_timeouts;
        raw_events += camera->metrics.events.size();
        delivery_gaps.insert(delivery_gaps.end(),
                             camera->metrics.delivery_interarrival_ms.begin(),
                             camera->metrics.delivery_interarrival_ms.end());
        wait_times.insert(
            wait_times.end(), camera->metrics.wait_ms.begin(), camera->metrics.wait_ms.end());
    }

    sched_param parameter{};
    sched_getparam(0, &parameter);
    out << "{\n"
        << "  \"schema_version\": 7,\n"
        << "  \"success\": " << (success ? "true" : "false") << ",\n"
        << "  \"error\": " << quoted(error) << ",\n"
        << "  \"scheduler\": {\"policy\":" << quoted(scheduler_policy())
        << ",\"priority\":" << parameter.sched_priority
        << ",\"main_thread_policy\":" << quoted(scheduler_policy())
        << ",\"steady_worker_policy\":"
        << quoted(deadline_result
                      ? "SCHED_DEADLINE"
                      : (rate_monotonic_result
                             ? rate_monotonic_result->policy
                             : scheduler_policy()))
        << "},\n"
        << "  \"deadline\": "
        << (deadline_result
                ? rs_camera::deadline_application_json(*deadline_result)
                : "null")
        << ",\n"
        << "  \"rate_monotonic\": "
        << (rate_monotonic_result
                ? rs_camera::rate_monotonic_application_json(
                      *rate_monotonic_result)
                : "null")
        << ",\n"
        << "  \"run\": {\"camera_count\":" << cameras.size()
        << ",\"stream_mode\":" << quoted(opts.stream_mode)
        << ",\"delivery\":" << quoted(opts.delivery)
        << ",\"measurement_mode\":"
        << quoted(fixed_duration_measurement(opts) ? "duration" : "deliveries")
        << ",\"frames_per_camera\":" << opts.frames
        << ",\"measurement_duration_ms\":" << opts.measurement_duration_ms
        << ",\"warmup_frames\":" << opts.warmup_frames
        << ",\"scheduler_apply_after_frames\":"
        << (modeled_scheduler_requested(opts) ? scheduler_apply_threshold(opts) : 0)
        << ",\"deadline_apply_after_frames\":"
        << (opts.deadline_profile.empty() ? 0 : scheduler_apply_threshold(opts))
        << ",\"fps\":" << opts.fps
        << ",\"frame_timeout_ms\":" << opts.frame_timeout_ms
        << ",\"startup_timeout_ms\":" << opts.startup_timeout_ms << "},\n"
        << "  \"transition\": {\"noise_gate_enabled\":"
        << (!opts.measurement_start_gate.empty() ? "true" : "false")
        << ",\"warmup_ready_boottime_ns\":" << warmup_ready_ns
        << ",\"measurement_gate_open_boottime_ns\":" << measurement_gate_open_ns
        << ",\"warmup_to_gate_ms\":"
        << (measurement_gate_open_ns >= warmup_ready_ns
                ? ns_to_ms(measurement_gate_open_ns - warmup_ready_ns)
                : 0.0)
        << "},\n"
        << "  \"measurement\": {\"start_boottime_ns\":" << measurement_start_ns
        << ",\"end_boottime_ns\":" << measurement_end_ns
        << ",\"mode\":"
        << quoted(fixed_duration_measurement(opts) ? "duration" : "deliveries")
        << ",\"requested_duration_ms\":" << opts.measurement_duration_ms
        << ",\"duration_ms\":"
        << (measurement_end_ns >= measurement_start_ns
                ? ns_to_ms(measurement_end_ns - measurement_start_ns)
                : 0.0)
        << "},\n"
        << "  \"postprocess\": {\"freshness_analysis_ms\":"
        << freshness_analysis_ms << "},\n"
        << "  \"aggregate\": {\"deliveries\":" << deliveries << ",\"frames\":" << frames
        << ",\"drops\":" << drops << ",\"timeouts\":" << timeouts
        << ",\"pre_measurement_timeouts\":" << pre_measurement_timeouts
        << ",\"measurement_timeouts\":" << measurement_timeouts
        << ",\"raw_events\":" << raw_events << ",";
    write_frame_freshness(out, aggregate_freshness.frames);
    out << ",";
    write_delivery_freshness(out, aggregate_freshness.deliveries);
    out << ",\"delivery_interarrival_ms\":";
    write_stats(out, summarize(delivery_gaps));
    out << ",\"wait_ms\":";
    write_stats(out, summarize(wait_times));
    out << "},\n  \"cameras\": [";

    for (size_t i = 0; i < cameras.size(); ++i)
    {
        const auto &camera = cameras[i];
        std::lock_guard<std::mutex> lock(camera->metrics.mutex);
        const auto &metrics = camera->metrics;
        const auto &freshness = camera_freshness[i];
        if (i)
            out << ",";
        out << "\n    {\"index\":" << i
            << ",\"serial\":" << quoted(camera->serial)
            << ",\"name\":" << quoted(camera->name)
            << ",\"firmware\":" << quoted(camera->firmware)
            << ",\"physical_port\":" << quoted(camera->physical_port)
            << ",\"usb_type\":" << quoted(camera->usb_type)
            << ",\"start_call_ms\":" << ns_to_ms(metrics.start_end_ns - metrics.start_begin_ns)
            << ",\"stop_call_ms\":"
            << (metrics.stop_end_ns >= metrics.stop_begin_ns
                    ? ns_to_ms(metrics.stop_end_ns - metrics.stop_begin_ns)
                    : 0.0)
            << ",\"warmup_deliveries\":" << metrics.warmup_deliveries
            << ",\"deliveries\":" << metrics.deliveries
            << ",\"frames\":" << metrics.frames
            << ",\"drops\":" << total_drops(metrics)
            << ",\"timeouts\":" << metrics.timeouts
            << ",\"pre_measurement_timeouts\":" << metrics.pre_measurement_timeouts
            << ",\"measurement_timeouts\":" << metrics.measurement_timeouts
            << ",";
        write_frame_freshness(out, freshness.frames);
        out << ",";
        write_delivery_freshness(out, freshness.deliveries);
        out
            << ",\"first_warmup_boottime_ns\":" << metrics.first_warmup_ns
            << ",\"first_measured_boottime_ns\":" << metrics.first_measured_ns
            << ",\"last_measured_boottime_ns\":" << metrics.last_measured_ns
            << ",\"delivery_interarrival_ms\":";
        write_stats(out, summarize(metrics.delivery_interarrival_ms));
        out << ",\"wait_ms\":";
        write_stats(out, summarize(metrics.wait_ms));
        out << ",\"active_streams\":";
        write_active_streams(out, camera->profile);
        out << ",\"streams\":{";
        bool first_stream = true;
        for (const auto &entry : metrics.streams)
        {
            if (!first_stream)
                out << ",";
            first_stream = false;
            const auto &stream = entry.second;
            const auto freshness_it = freshness.streams.find(entry.first);
            const frame_freshness_metrics empty_freshness;
            const auto &stream_freshness = freshness_it != freshness.streams.end()
                                               ? freshness_it->second
                                               : empty_freshness;
            out << quoted(entry.first) << ":{\"frames\":" << stream.frames
                << ",\"drops\":" << stream.drops
                << ",";
            write_frame_freshness(out, stream_freshness);
            out << ",\"timestamp_domain\":" << quoted(stream.timestamp_domain)
                << ",\"sensor_interarrival_ms\":";
            write_stats(out, summarize(stream.sensor_interarrival_ms));
            out << ",\"host_interarrival_ms\":";
            write_stats(out, summarize(stream.host_interarrival_ms));
            out << "}";
        }
        out << "}}";
    }
    out << "\n  ]\n}\n";
}

void write_events(const std::string &path,
                  const std::vector<std::unique_ptr<camera_runtime>> &cameras,
                  uint64_t origin_ns)
{
    std::ofstream out(path);
    if (!out)
        throw std::runtime_error("Cannot open events output: " + path);
    out << "camera_index,serial,delivery,stream,stream_index,frame_number,"
           "sensor_timestamp_ms,timestamp_domain,host_boottime_ns,relative_ms\n";
    out << std::fixed << std::setprecision(6);
    for (size_t i = 0; i < cameras.size(); ++i)
    {
        const auto &camera = cameras[i];
        std::lock_guard<std::mutex> lock(camera->metrics.mutex);
        for (const auto &event : camera->metrics.events)
        {
            out << i << "," << csv_field(camera->serial) << "," << event.delivery << ","
                << csv_field(event.stream) << "," << event.stream_index << ","
                << event.frame_number << "," << event.sensor_timestamp_ms << ","
                << csv_field(event.timestamp_domain) << "," << event.host_boottime_ns << ","
                << (event.host_boottime_ns >= origin_ns
                        ? ns_to_ms(event.host_boottime_ns - origin_ns)
                        : 0.0)
                << "\n";
        }
    }
}

int automatic_measurement_timeout_ms(const options &opts)
{
    const double expected_ms =
        static_cast<double>(opts.frames) * 1000.0 / static_cast<double>(opts.fps);
    return static_cast<int>(expected_ms * 2.0) + std::max(5000, opts.frame_timeout_ms * 3);
}
} // namespace

int main(int argc, char **argv)
try
{
    const options opts = parse_args(argc, argv);
    rs_trace_phase_marker("process_start");
    rs_trace_phase_marker("before_context");
    rs2::context context;
    rs_trace_phase_marker("after_context");
    const auto serials = select_serials(context, opts);
    rs_trace_phase_marker("after_device_selection");

    std::vector<std::unique_ptr<camera_runtime>> cameras;
    shared_control control;
    cameras.reserve(serials.size());
    for (const auto &serial : serials)
    {
        auto camera = std::make_unique<camera_runtime>(context);
        camera->serial = serial;
        configure_streams(camera->config, opts, serial);
        cameras.emplace_back(std::move(camera));
    }

    rs_trace_phase_marker("before_pipeline_start");
    for (size_t i = 0; i < cameras.size(); ++i)
    {
        auto &camera = *cameras[i];
        camera.metrics.start_begin_ns = rs_trace_boottime_ns();
        if (opts.delivery == "callback")
        {
            camera.profile = camera.pipe.start(camera.config, [&, selected = &camera](rs2::frame frame) {
                record_delivery(*selected, frame, rs_trace_boottime_ns(), -1.0, opts, control);
            });
        }
        else
            camera.profile = camera.pipe.start(camera.config);
        camera.metrics.start_end_ns = rs_trace_boottime_ns();
        const auto device = camera.profile.get_device();
        camera.name = info(device, RS2_CAMERA_INFO_NAME);
        camera.firmware = info(device, RS2_CAMERA_INFO_FIRMWARE_VERSION);
        camera.physical_port = info(device, RS2_CAMERA_INFO_PHYSICAL_PORT);
        camera.usb_type = info(device, RS2_CAMERA_INFO_USB_TYPE_DESCRIPTOR);
    }
    rs_trace_phase_marker("after_pipeline_start");

    if (opts.delivery == "wait")
    {
        for (size_t i = 0; i < cameras.size(); ++i)
        {
            auto *camera = cameras[i].get();
            camera->wait_thread =
                std::thread([&, camera, i]() { wait_loop(*camera, i, opts, control); });
        }
    }

    std::unique_ptr<rs_camera::deadline_application> deadline_result;
    std::unique_ptr<rs_camera::rate_monotonic_application>
        rate_monotonic_result;
    if (modeled_scheduler_requested(opts))
    {
        {
            std::unique_lock<std::mutex> lock(control.mutex);
            const bool ready = control.cv.wait_for(
                lock,
                std::chrono::milliseconds(opts.startup_timeout_ms),
                [&]() {
                    return control.failed.load() ||
                           control.scheduler_ready_cameras == cameras.size();
                });
            if (!ready && !control.failed.load())
            {
                lock.unlock();
                set_failure(
                    control,
                    "Timed out before the modeled-scheduler warm-up transition");
            }
        }
        if (!control.failed.load())
        {
            try
            {
                if (!opts.deadline_profile.empty())
                {
                    rs_trace_phase_marker("deadline_apply_begin");
                    deadline_result =
                        std::make_unique<rs_camera::deadline_application>(
                            rs_camera::apply_deadline_profile(
                                opts.deadline_profile));
                    std::cout << "RS_DEADLINE {\"profile_entries\":"
                              << deadline_result->profile_entries
                              << ",\"live_threads\":"
                              << deadline_result->live_threads
                              << ",\"applied\":true}\n";
                    rs_trace_phase_marker("deadline_apply_end");
                }
                else
                {
                    rs_trace_phase_marker("rate_monotonic_apply_begin");
                    const int policy = opts.rate_monotonic_policy == "rr"
                                           ? SCHED_RR
                                           : SCHED_FIFO;
                    rate_monotonic_result = std::make_unique<
                        rs_camera::rate_monotonic_application>(
                        rs_camera::apply_rate_monotonic_profile(
                            opts.rate_monotonic_profile,
                            policy,
                            opts.rate_monotonic_highest_priority));
                    std::cout << "RS_RATE_MONOTONIC {\"policy\":\""
                              << rate_monotonic_result->policy
                              << "\",\"profile_entries\":"
                              << rate_monotonic_result->profile_entries
                              << ",\"live_threads\":"
                              << rate_monotonic_result->live_threads
                              << ",\"priority_levels\":"
                              << rate_monotonic_result->priority_levels
                              << ",\"highest_priority\":"
                              << rate_monotonic_result->highest_priority
                              << ",\"lowest_priority\":"
                              << rate_monotonic_result->lowest_priority
                              << ",\"applied\":true}\n";
                    rs_trace_phase_marker("rate_monotonic_apply_end");
                }
            }
            catch (const std::exception &error)
            {
                rs_trace_phase_marker(
                    opts.deadline_profile.empty()
                        ? "rate_monotonic_apply_error"
                        : "deadline_apply_error");
                set_failure(
                    control,
                    std::string(opts.deadline_profile.empty()
                                    ? "Rate-monotonic setup failed: "
                                    : "SCHED_DEADLINE setup failed: ") +
                        error.what());
            }
        }
    }

    if (!control.failed.load())
    {
        std::unique_lock<std::mutex> lock(control.mutex);
        const bool ready = control.cv.wait_for(
            lock,
            std::chrono::milliseconds(opts.startup_timeout_ms),
            [&]() {
                return control.failed.load() ||
                       control.warmed_cameras == cameras.size();
            });
        if (!ready && !control.failed.load())
        {
            lock.unlock();
            set_failure(control, "Timed out while waiting for all cameras to warm up");
        }
    }

    uint64_t warmup_ready_ns = 0;
    uint64_t measurement_gate_open_ns = 0;
    uint64_t measurement_start_ns = 0;
    uint64_t measurement_end_ns = 0;
    if (!control.failed.load())
    {
        warmup_ready_ns = rs_trace_boottime_ns();
        rs_trace_phase_marker("camera_warmup_complete");
        if (!opts.measurement_start_gate.empty())
        {
            write_atomic_timestamp(opts.warmup_ready_file, warmup_ready_ns);
            rs_trace_phase_marker("noise_start_wait_begin");
            control.noise_transition_active.store(true);
            measurement_gate_open_ns = wait_for_measurement_gate(opts, control);
            control.noise_transition_active.store(false);
            if (!control.failed.load())
                rs_trace_phase_marker("noise_ready");
        }
        else
            measurement_gate_open_ns = warmup_ready_ns;
    }
    if (!control.failed.load())
    {
        measurement_start_ns = rs_trace_boottime_ns();
        control.origin_ns.store(measurement_start_ns);
        if (fixed_duration_measurement(opts))
        {
            control.measurement_deadline_ns.store(
                measurement_start_ns +
                static_cast<uint64_t>(opts.measurement_duration_ms) * 1000000ULL);
        }
        rs_trace_phase_marker("steady_state_begin");
        control.measurement_enabled.store(true);
        std::unique_lock<std::mutex> lock(control.mutex);
        if (fixed_duration_measurement(opts))
        {
            control.cv.wait_for(
                lock,
                std::chrono::milliseconds(opts.measurement_duration_ms),
                [&]() { return control.failed.load(); });
        }
        else
        {
            const int timeout_ms = opts.measurement_timeout_ms > 0
                                       ? opts.measurement_timeout_ms
                                       : automatic_measurement_timeout_ms(opts);
            const bool complete = control.cv.wait_for(
                lock,
                std::chrono::milliseconds(timeout_ms),
                [&]() {
                    return control.failed.load() ||
                           control.completed_cameras == cameras.size();
                });
            if (!complete && !control.failed.load())
            {
                lock.unlock();
                set_failure(control, "Timed out during steady-state measurement");
            }
        }
        control.measurement_enabled.store(false);
        measurement_end_ns = rs_trace_boottime_ns();
        rs_trace_phase_marker("steady_state_end");
    }

    control.stop.store(true);
    rs_trace_phase_marker("before_pipeline_stop");
    for (auto &camera : cameras)
    {
        camera->metrics.stop_begin_ns = rs_trace_boottime_ns();
        try
        {
            camera->pipe.stop();
        }
        catch (const rs2::error &error)
        {
            if (!control.failed.load())
                set_failure(control, camera->serial + " stop: " + error.what());
        }
        camera->metrics.stop_end_ns = rs_trace_boottime_ns();
    }
    for (auto &camera : cameras)
    {
        if (camera->wait_thread.joinable())
            camera->wait_thread.join();
    }
    rs_trace_phase_marker("after_pipeline_stop");

    if (!opts.events_output.empty())
        write_events(opts.events_output, cameras, measurement_start_ns);
    if (!opts.summary_output.empty())
        write_summary(opts.summary_output,
                      opts,
                      cameras,
                      warmup_ready_ns,
                      measurement_gate_open_ns,
                      measurement_start_ns,
                      measurement_end_ns,
                      !control.failed.load(),
                      control.error,
                      deadline_result.get(),
                      rate_monotonic_result.get());

    std::cout << "RS_STEADY_RESULT {\"success\":" << (!control.failed.load() ? "true" : "false")
              << ",\"camera_count\":" << cameras.size()
              << ",\"measurement_mode\":"
              << quoted(fixed_duration_measurement(opts) ? "duration" : "deliveries")
              << ",\"frames_per_camera\":" << opts.frames
              << ",\"measurement_ms\":"
              << (measurement_end_ns >= measurement_start_ns
                      ? ns_to_ms(measurement_end_ns - measurement_start_ns)
                      : 0.0)
              << ",\"error\":" << quoted(control.error) << "}\n";
    rs_trace_phase_marker(control.failed.load() ? "process_error" : "process_exit");
    return control.failed.load() ? 2 : 0;
}
catch (const rs2::error &error)
{
    rs_trace_phase_marker("process_error");
    std::cerr << "RealSense error: " << error.what() << "\n";
    return 2;
}
catch (const std::exception &error)
{
    rs_trace_phase_marker("process_error");
    std::cerr << "Error: " << error.what() << "\n";
    return 1;
}
