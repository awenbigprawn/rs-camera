#include "rs_camera/benchmark_utils.h"
#include "rs_camera/deadline_scheduler.h"
#include "rs_camera/hardware_sync.h"
#include "rs_camera/realsense_utils.h"
#include "rs_camera/steady_metrics.h"
#include "rs_camera/trace_marker.h"

#include <librealsense2/rs.hpp>

#include <algorithm>
#include <atomic>
#include <chrono>
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
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

namespace
{
using rs_camera::csv_field;
using rs_camera::device_info;
using rs_camera::ns_to_ms;
using rs_camera::quoted;
using rs_camera::summarize;
using rs_camera::wait_for_pipeline_frame;
using rs_camera::write_stats_json;
using rs_camera::steady::add_freshness;
using rs_camera::steady::analyze_freshness;
using rs_camera::steady::camera_freshness_metrics;
using rs_camera::steady::camera_metrics;
using rs_camera::steady::delivery_freshness_metrics;
using rs_camera::steady::expected_stream_count;
using rs_camera::steady::frame_freshness_metrics;
using rs_camera::steady::make_storage_plan;
using rs_camera::steady::prepare_camera_metrics;
using rs_camera::steady::record_measured_frame;
using rs_camera::steady::record_warmup_frame;
using rs_camera::steady::total_drops;
using rs_camera::steady::warmup_health_error;

struct options
{
    std::vector<std::string> serials;
    std::string stream_mode = "stereo_all";
    std::string delivery = "wait";
    std::string summary_output;
    std::string events_output;
    std::string deadline_profile;
    std::string rate_monotonic_profile;
    std::string rate_monotonic_policy;
    std::string warmup_ready_file;
    std::string measurement_start_gate;
    std::string hardware_sync_master;
    std::vector<std::string> hardware_sync_slaves;
    bool deadline_allow_partial_profile = false;
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
    int warmup_health_window_frames = 0;
    int frame_timeout_ms = 1500;
    int startup_timeout_ms = 15000;
    int measurement_duration_ms = 0;
    int measurement_timeout_ms = 0;
    int measurement_gate_timeout_ms = 0;
};

struct camera_runtime
{
    explicit camera_runtime(rs2::context &context)
        : pipe(context), pipeline_handle(pipe)
    {
    }

    std::string serial;
    std::string name;
    std::string firmware;
    std::string physical_port;
    std::string usb_type;
    rs2::pipeline pipe;
    std::shared_ptr<rs2_pipeline> pipeline_handle;
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
        else if (arg == "--warmup-health-window-frames")
            opts.warmup_health_window_frames = std::stoi(value(arg));
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
        else if (arg == "--deadline-allow-partial-profile")
            opts.deadline_allow_partial_profile = true;
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
        else if (arg == "--hardware-sync-master")
            opts.hardware_sync_master = value(arg);
        else if (arg == "--hardware-sync-slave")
            opts.hardware_sync_slaves.push_back(value(arg));
        else if (arg == "--help" || arg == "-h")
        {
            std::cout
                << "Usage: " << argv[0] << " [options]\n"
                << "  --serial SERIAL              Repeat for each selected camera\n"
                << "  --camera-count N             Select the first N cameras when serials are omitted\n"
                << "  --stream-mode depth|depth_color|stereo_all\n"
                << "      d435_all remains a compatibility alias for stereo_all\n"
                << "  --delivery wait|callback\n"
                << "  --frames N                   Measured deliveries per camera\n"
                << "  --measurement-duration-ms N Measure for a fixed wall-clock duration; zero uses --frames\n"
                << "  --warmup-frames N\n"
                << "  --warmup-health-window-frames N  Validate only the final N warm-up deliveries\n"
                << "  --frame-timeout-ms N --startup-timeout-ms N\n"
                << "  --measurement-timeout-ms N   Zero selects an automatic deadline\n"
                << "  --fps N\n"
                << "  --depth-width N --depth-height N\n"
                << "  --color-width N --color-height N\n"
                << "  --summary-output PATH --events-output PATH\n"
                << "  --deadline-profile PATH       Apply per-thread SCHED_DEADLINE during warm-up\n"
                << "  --deadline-allow-partial-profile  Leave live workers absent from the profile as SCHED_OTHER\n"
                << "  --rate-monotonic-profile PATH Apply per-thread fixed-priority scheduling during warm-up\n"
                << "  --rate-monotonic-policy rr|fifo\n"
                << "  --rate-monotonic-highest-priority N\n"
                << "  --scheduler-apply-after-frames N  Per-camera pre-scheduler warm-up; zero is automatic\n"
                << "  --deadline-apply-after-frames N  Compatibility alias for the previous option\n"
                << "  --warmup-ready-file PATH      Signal that every camera is warm\n"
                << "  --measurement-start-gate PATH Wait for this file before measuring\n"
                << "  --measurement-gate-timeout-ms N  Maximum gate wait\n"
                << "  --hardware-sync-master SERIAL   Set one selected depth sensor to mode 1\n"
                << "  --hardware-sync-slave SERIAL    Repeat for selected depth sensors in mode 2\n";
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
    if (opts.warmup_health_window_frames < 0 ||
        opts.warmup_health_window_frames > opts.warmup_frames)
        throw std::runtime_error(
            "warmup-health-window-frames must be zero or no larger than warmup-frames");
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
    const bool hardware_sync_requested =
        !opts.hardware_sync_master.empty() || !opts.hardware_sync_slaves.empty();
    if (hardware_sync_requested)
    {
        if (opts.hardware_sync_master.empty() || opts.hardware_sync_slaves.empty())
            throw std::runtime_error(
                "Hardware sync requires one master and at least one slave");
        if (opts.serials.empty())
            throw std::runtime_error(
                "Hardware sync requires explicit --serial arguments");
        std::set<std::string> roles(opts.hardware_sync_slaves.begin(),
                                    opts.hardware_sync_slaves.end());
        if (roles.size() != opts.hardware_sync_slaves.size())
            throw std::runtime_error("Hardware-sync slave serials must be unique");
        if (!roles.insert(opts.hardware_sync_master).second)
            throw std::runtime_error(
                "The hardware-sync master cannot also be a slave");
        const std::set<std::string> selected(opts.serials.begin(), opts.serials.end());
        if (selected.size() != opts.serials.size())
            throw std::runtime_error("Selected camera serials must be unique");
        if (roles != selected)
            throw std::runtime_error(
                "Every selected camera must have exactly one hardware-sync role");
    }
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

void configure_streams(rs2::config &config, const options &opts, const std::string &serial)
{
    const bool stereo_all =
        opts.stream_mode == "stereo_all" || opts.stream_mode == "d435_all";
    config.enable_device(serial);
    config.enable_stream(
        RS2_STREAM_DEPTH, opts.depth_width, opts.depth_height, RS2_FORMAT_Z16, opts.fps);
    if (opts.stream_mode == "depth")
        return;
    if (opts.stream_mode == "depth_color" || stereo_all)
    {
        config.enable_stream(
            RS2_STREAM_COLOR, opts.color_width, opts.color_height, RS2_FORMAT_RGB8, opts.fps);
    }
    else
        throw std::runtime_error("Unsupported stream mode: " + opts.stream_mode);
    if (stereo_all)
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
                     uint64_t host_realtime_ns,
                     double wait_ms,
                     const options &opts,
                     shared_control &control)
{
    bool became_scheduler_ready = false;
    bool became_warm = false;
    bool became_complete = false;
    std::string health_error;
    std::string storage_error;
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
            const uint64_t health_window = std::min(
                static_cast<uint64_t>(opts.warmup_frames),
                static_cast<uint64_t>(opts.warmup_health_window_frames > 0
                    ? opts.warmup_health_window_frames
                    : std::max(2, opts.fps)));
            const uint64_t health_start =
                static_cast<uint64_t>(opts.warmup_frames) - health_window + 1;
            if (metrics.warmup_deliveries >= health_start)
            {
                ++metrics.warmup_health_deliveries;
                if (frame.is<rs2::frameset>())
                {
                    for (auto &&child : frame.as<rs2::frameset>())
                    {
                        const std::string error = record_warmup_frame(metrics, child);
                        if (health_error.empty() && !error.empty())
                            health_error = camera.serial + ": " + error;
                    }
                }
                else
                {
                    const std::string error = record_warmup_frame(metrics, frame);
                    if (!error.empty())
                        health_error = camera.serial + ": " + error;
                }
            }
            if (modeled_scheduler_requested(opts) && !metrics.scheduler_ready &&
                metrics.warmup_deliveries >=
                    static_cast<uint64_t>(scheduler_apply_threshold(opts)))
            {
                metrics.scheduler_ready = true;
                became_scheduler_ready = true;
            }
            if (metrics.warmup_deliveries >= static_cast<uint64_t>(opts.warmup_frames))
            {
                if (health_error.empty())
                {
                    health_error = warmup_health_error(
                        metrics,
                        camera.serial,
                        expected_stream_count(opts.stream_mode),
                        opts.delivery == "wait",
                        !opts.hardware_sync_master.empty());
                }
                if (health_error.empty())
                {
                    metrics.warmed = true;
                    became_warm = true;
                }
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
            {
                if (metrics.delivery_interarrival_ms.size() >=
                    metrics.delivery_interarrival_ms.capacity())
                    storage_error = "Preallocated delivery interval capacity exhausted";
                else
                    metrics.delivery_interarrival_ms.push_back(
                        ns_to_ms(host_ns - metrics.last_delivery_ns));
            }
            metrics.last_delivery_ns = host_ns;
            metrics.last_measured_ns = host_ns;
            if (wait_ms >= 0.0)
            {
                if (metrics.wait_ms.size() >= metrics.wait_ms.capacity())
                    storage_error = "Preallocated wait sample capacity exhausted";
                else
                    metrics.wait_ms.push_back(wait_ms);
            }

            if (storage_error.empty() && frame.is<rs2::frameset>())
            {
                for (auto &&child : frame.as<rs2::frameset>())
                {
                    storage_error = record_measured_frame(
                        metrics, child, host_ns, host_realtime_ns, metrics.deliveries);
                    if (!storage_error.empty())
                        break;
                }
            }
            else if (storage_error.empty())
                storage_error = record_measured_frame(
                    metrics, frame, host_ns, host_realtime_ns, metrics.deliveries);

            if (!fixed_duration_measurement(opts) &&
                metrics.deliveries >= static_cast<uint64_t>(opts.frames))
            {
                metrics.completed = true;
                became_complete = true;
            }
        }
    }

    if (!health_error.empty())
    {
        set_failure(control, "Warm-up freshness check failed: " + health_error);
        return;
    }
    if (!storage_error.empty())
    {
        set_failure(
            control,
            "Measurement storage failure for " + camera.serial + ": " + storage_error);
        return;
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
        auto wait_result = wait_for_pipeline_frame(
            camera.pipeline_handle.get(),
            static_cast<unsigned int>(opts.frame_timeout_ms));
        const uint64_t end_ns = rs_trace_boottime_ns();
        const uint64_t end_realtime_ns = rs_trace_realtime_ns();
        if (wait_result)
        {
            record_delivery(
                camera,
                wait_result.frame,
                end_ns,
                end_realtime_ns,
                ns_to_ms(end_ns - begin_ns),
                opts,
                control);
            std::lock_guard<std::mutex> lock(camera.metrics.mutex);
            if (camera.metrics.completed)
                break;
            continue;
        }

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
        if (control.stop.load() && control.failed.load())
            break;
        const std::string prefix = control.noise_transition_active.load()
                                       ? "Noise transition frame failure: "
                                       : "";
        set_failure(control, prefix + camera.serial + ": " + wait_result.error);
        break;
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

void write_hardware_sync(std::ostream &out,
                         const rs_camera::hardware_sync_session &session)
{
    out << "{\"enabled\":" << (session.enabled() ? "true" : "false")
        << ",\"master_serial\":" << quoted(session.master_serial())
        << ",\"slave_serials\":[";
    bool first = true;
    for (const auto &serial : session.slave_serials())
    {
        if (!first)
            out << ",";
        first = false;
        out << quoted(serial);
    }
    out << "],\"all_applied\":"
        << (session.all_applied() ? "true" : "false")
        << ",\"all_restored\":"
        << (session.all_restored() ? "true" : "false")
        << ",\"assignments\":[";
    first = true;
    for (const auto &assignment : session.assignments())
    {
        if (!first)
            out << ",";
        first = false;
        out << "{\"serial\":" << quoted(assignment.serial)
            << ",\"role\":" << quoted(assignment.role)
            << ",\"requested_mode\":" << assignment.requested_mode
            << ",\"previous_mode\":" << assignment.previous_mode
            << ",\"effective_mode\":" << assignment.effective_mode
            << ",\"restored_mode\":" << assignment.restored_mode
            << ",\"applied\":" << (assignment.applied ? "true" : "false")
            << ",\"restored\":" << (assignment.restored ? "true" : "false")
            << ",\"restore_error\":" << quoted(assignment.restore_error)
            << "}";
    }
    out << "]}";
}

size_t camera_index(const std::vector<std::unique_ptr<camera_runtime>> &cameras,
                    const std::string &serial)
{
    for (size_t index = 0; index < cameras.size(); ++index)
    {
        if (cameras[index]->serial == serial)
            return index;
    }
    throw std::runtime_error("Selected camera is unavailable: " + serial);
}

std::vector<size_t> pipeline_start_order(
    const options &opts,
    const std::vector<std::unique_ptr<camera_runtime>> &cameras)
{
    std::vector<size_t> order;
    order.reserve(cameras.size());
    if (opts.hardware_sync_master.empty())
    {
        for (size_t index = 0; index < cameras.size(); ++index)
            order.push_back(index);
        return order;
    }
    for (const auto &serial : opts.hardware_sync_slaves)
        order.push_back(camera_index(cameras, serial));
    order.push_back(camera_index(cameras, opts.hardware_sync_master));
    return order;
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
                   const rs_camera::hardware_sync_session &hardware_sync,
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
        << "  \"schema_version\": 9,\n"
        << "  \"success\": " << (success ? "true" : "false") << ",\n"
        << "  \"error\": " << quoted(error) << ",\n"
        << "  \"scheduler\": {\"policy\":" << quoted(scheduler_policy())
        << ",\"priority\":" << parameter.sched_priority
        << ",\"main_thread_policy\":" << quoted(scheduler_policy())
        << ",\"steady_worker_policy\":"
        << quoted(deadline_result
                      ? (deadline_result->partial_profile
                             ? "SCHED_DEADLINE+SCHED_OTHER"
                             : "SCHED_DEADLINE")
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
        << ",\n  \"hardware_sync\": ";
    write_hardware_sync(out, hardware_sync);
    out << ",\n"
        << "  \"run\": {\"camera_count\":" << cameras.size()
        << ",\"stream_mode\":" << quoted(opts.stream_mode)
        << ",\"delivery\":" << quoted(opts.delivery)
        << ",\"measurement_mode\":"
        << quoted(fixed_duration_measurement(opts) ? "duration" : "deliveries")
        << ",\"frames_per_camera\":" << opts.frames
        << ",\"measurement_duration_ms\":" << opts.measurement_duration_ms
        << ",\"warmup_frames\":" << opts.warmup_frames
        << ",\"warmup_health_window_frames\":"
        << (opts.warmup_health_window_frames > 0
                ? opts.warmup_health_window_frames
                : std::min(opts.warmup_frames, std::max(2, opts.fps)))
        << ",\"scheduler_apply_after_frames\":"
        << (modeled_scheduler_requested(opts) ? scheduler_apply_threshold(opts) : 0)
        << ",\"deadline_apply_after_frames\":"
        << (opts.deadline_profile.empty() ? 0 : scheduler_apply_threshold(opts))
        << ",\"fps\":" << opts.fps
        << ",\"frame_timeout_ms\":" << opts.frame_timeout_ms
        << ",\"startup_timeout_ms\":" << opts.startup_timeout_ms
        << ",\"fixed_event_storage\":true},\n"
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
    write_stats_json(out, summarize(delivery_gaps));
    out << ",\"wait_ms\":";
    write_stats_json(out, summarize(wait_times));
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
            << ",\"warmup_health_deliveries\":"
            << metrics.warmup_health_deliveries;
        uint64_t warmup_observed = 0;
        uint64_t warmup_duplicates = 0;
        uint64_t warmup_gaps = 0;
        uint64_t warmup_out_of_order = 0;
        for (const auto &[key, stream] : metrics.warmup_streams)
        {
            (void)key;
            warmup_observed += stream.observed_frames;
            warmup_duplicates += stream.duplicate_frames;
            warmup_gaps += stream.sequence_gaps;
            warmup_out_of_order += stream.out_of_order_frames;
        }
        out << ",\"warmup_observed_frames\":" << warmup_observed
            << ",\"warmup_duplicate_frames\":" << warmup_duplicates
            << ",\"warmup_sequence_gaps\":" << warmup_gaps
            << ",\"warmup_out_of_order_frames\":" << warmup_out_of_order
            << ",\"deliveries\":" << metrics.deliveries
            << ",\"frames\":" << metrics.frames
            << ",\"drops\":" << total_drops(metrics)
            << ",\"timeouts\":" << metrics.timeouts
            << ",\"pre_measurement_timeouts\":" << metrics.pre_measurement_timeouts
            << ",\"measurement_timeouts\":" << metrics.measurement_timeouts
            << ",\"storage\":{\"delivery_capacity\":"
            << metrics.storage.delivery_capacity
            << ",\"event_capacity\":" << metrics.storage.event_capacity
            << ",\"stream_sample_capacity\":"
            << metrics.storage.stream_sample_capacity
            << ",\"allocated_event_capacity\":" << metrics.events.capacity()
            << "}"
            << ",";
        write_frame_freshness(out, freshness.frames);
        out << ",";
        write_delivery_freshness(out, freshness.deliveries);
        out
            << ",\"first_warmup_boottime_ns\":" << metrics.first_warmup_ns
            << ",\"first_measured_boottime_ns\":" << metrics.first_measured_ns
            << ",\"last_measured_boottime_ns\":" << metrics.last_measured_ns
            << ",\"delivery_interarrival_ms\":";
        write_stats_json(out, summarize(metrics.delivery_interarrival_ms));
        out << ",\"wait_ms\":";
        write_stats_json(out, summarize(metrics.wait_ms));
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
            out << ",\"timestamp_domain\":"
                << quoted(rs2_timestamp_domain_to_string(stream.timestamp_domain))
                << ",\"sensor_interarrival_ms\":";
            write_stats_json(out, summarize(stream.sensor_interarrival_ms));
            out << ",\"host_interarrival_ms\":";
            write_stats_json(out, summarize(stream.host_interarrival_ms));
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
           "sensor_timestamp_ms,timestamp_domain,frame_timestamp_ms,"
           "backend_timestamp_ms,time_of_arrival_ms,host_boottime_ns,"
           "host_realtime_ns,backend_to_return_ms,arrival_to_return_ms,relative_ms\n";
    out << std::fixed << std::setprecision(6);
    for (size_t i = 0; i < cameras.size(); ++i)
    {
        const auto &camera = cameras[i];
        std::lock_guard<std::mutex> lock(camera->metrics.mutex);
        for (const auto &event : camera->metrics.events)
        {
            out << i << "," << csv_field(camera->serial) << "," << event.delivery << ","
                << csv_field(rs2_stream_to_string(event.stream)) << ","
                << event.stream_index << ","
                << event.frame_number << "," << event.sensor_timestamp_ms << ","
                << csv_field(rs2_timestamp_domain_to_string(event.timestamp_domain))
                << "," << (event.has_frame_timestamp ? event.frame_timestamp_ms : 0.0)
                << "," << (event.has_backend_timestamp ? event.backend_timestamp_ms : 0.0)
                << "," << (event.has_time_of_arrival ? event.time_of_arrival_ms : 0.0)
                << "," << event.host_boottime_ns
                << "," << event.host_realtime_ns
                << "," << (event.has_backend_timestamp
                        ? static_cast<double>(event.host_realtime_ns) / 1e6
                              - event.backend_timestamp_ms
                        : 0.0)
                << "," << (event.has_time_of_arrival
                        ? static_cast<double>(event.host_realtime_ns) / 1e6
                              - event.time_of_arrival_ms
                        : 0.0)
                << ","
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
    const auto event_storage_plan = make_storage_plan(
        opts.frames,
        opts.measurement_duration_ms,
        opts.fps,
        expected_stream_count(opts.stream_mode),
        opts.delivery == "callback");

    std::vector<std::unique_ptr<camera_runtime>> cameras;
    shared_control control;
    cameras.reserve(serials.size());
    for (const auto &serial : serials)
    {
        auto camera = std::make_unique<camera_runtime>(context);
        camera->serial = serial;
        prepare_camera_metrics(camera->metrics, opts.stream_mode, event_storage_plan);
        configure_streams(camera->config, opts, serial);
        cameras.emplace_back(std::move(camera));
    }
    rs_trace_phase_marker("after_event_storage_allocation");

    rs_trace_phase_marker("hardware_sync_configure_begin");
    rs_camera::hardware_sync_session hardware_sync(
        context, opts.hardware_sync_master, opts.hardware_sync_slaves);
    if (hardware_sync.enabled())
    {
        std::cout << "RS_HARDWARE_SYNC {\"master\":"
                  << quoted(hardware_sync.master_serial())
                  << ",\"slaves\":[";
        bool first = true;
        for (const auto &serial : hardware_sync.slave_serials())
        {
            if (!first)
                std::cout << ",";
            first = false;
            std::cout << quoted(serial);
        }
        std::cout << "],\"assignments\":[";
        first = true;
        for (const auto &assignment : hardware_sync.assignments())
        {
            if (!first)
                std::cout << ",";
            first = false;
            std::cout << "{\"serial\":" << quoted(assignment.serial)
                      << ",\"role\":" << quoted(assignment.role)
                      << ",\"previous_mode\":" << assignment.previous_mode
                      << ",\"effective_mode\":" << assignment.effective_mode
                      << "}";
        }
        std::cout << "],\"applied\":"
                  << (hardware_sync.all_applied() ? "true" : "false")
                  << "}\n";
    }
    rs_trace_phase_marker("hardware_sync_configure_end");
    const auto start_order = pipeline_start_order(opts, cameras);

    rs_trace_phase_marker("before_pipeline_start");
    for (const size_t i : start_order)
    {
        auto &camera = *cameras[i];
        camera.metrics.start_begin_ns = rs_trace_boottime_ns();
        if (opts.delivery == "callback")
        {
            camera.profile = camera.pipe.start(camera.config, [&, selected = &camera](rs2::frame frame) {
                record_delivery(
                    *selected,
                    frame,
                    rs_trace_boottime_ns(),
                    rs_trace_realtime_ns(),
                    -1.0,
                    opts,
                    control);
            });
        }
        else
            camera.profile = camera.pipe.start(camera.config);
        camera.metrics.start_end_ns = rs_trace_boottime_ns();
        const auto device = camera.profile.get_device();
        camera.name = device_info(device, RS2_CAMERA_INFO_NAME);
        camera.firmware = device_info(device, RS2_CAMERA_INFO_FIRMWARE_VERSION);
        camera.physical_port = device_info(device, RS2_CAMERA_INFO_PHYSICAL_PORT);
        camera.usb_type = device_info(device, RS2_CAMERA_INFO_USB_TYPE_DESCRIPTOR);
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
                                opts.deadline_profile,
                                !opts.deadline_allow_partial_profile));
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
    std::vector<size_t> stop_order = start_order;
    if (hardware_sync.enabled())
        std::reverse(stop_order.begin(), stop_order.end());
    for (const size_t index : stop_order)
    {
        auto &camera = cameras[index];
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

    rs_trace_phase_marker("hardware_sync_restore_begin");
    const std::string hardware_sync_restore_error = hardware_sync.restore();
    if (!hardware_sync_restore_error.empty() && !control.failed.load())
        set_failure(control, "Hardware-sync restore failed: " +
                                 hardware_sync_restore_error);
    rs_trace_phase_marker("hardware_sync_restore_end");

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
                      hardware_sync,
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
