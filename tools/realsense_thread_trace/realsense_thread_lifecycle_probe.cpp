#include "trace_marker.h"

#include <librealsense2/rs.hpp>

#include <cerrno>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <map>
#include <memory>
#include <pthread.h>
#include <stdexcept>
#include <string>
#include <thread>

namespace
{
struct options
{
    int frames = 300;
    int steady_state_after = 30;
    int sleep_after_start_ms = 0;
    int sched_fifo_priority = 80;
    std::string serial;
    bool d435_max_streams = false;
    bool list_profiles_only = false;
    bool sched_fifo = false;
};

options parse_args(int argc, char **argv)
{
    options opts;
    for (int i = 1; i < argc; ++i)
    {
        const std::string arg = argv[i];
        if (arg == "--frames" && i + 1 < argc)
            opts.frames = std::stoi(argv[++i]);
        else if (arg == "--steady-state-after" && i + 1 < argc)
            opts.steady_state_after = std::stoi(argv[++i]);
        else if (arg == "--sleep-after-start-ms" && i + 1 < argc)
            opts.sleep_after_start_ms = std::stoi(argv[++i]);
        else if (arg == "--sched-fifo")
            opts.sched_fifo = true;
        else if (arg == "--sched-fifo-priority" && i + 1 < argc)
        {
            opts.sched_fifo = true;
            opts.sched_fifo_priority = std::stoi(argv[++i]);
        }
        else if (arg == "--serial" && i + 1 < argc)
            opts.serial = argv[++i];
        else if (arg == "--d435-max-streams")
            opts.d435_max_streams = true;
        else if (arg == "--list-profiles-only")
            opts.list_profiles_only = true;
        else if (arg == "--help" || arg == "-h")
        {
            std::cout
                << "Usage: " << argv[0]
                << " [--frames N] [--steady-state-after N] [--serial SERIAL]\n"
                << "       [--sleep-after-start-ms N] [--sched-fifo] [--sched-fifo-priority N]\n"
                << "       [--d435-max-streams] [--list-profiles-only]\n";
            std::exit(0);
        }
        else
        {
            throw std::runtime_error("Unknown or incomplete argument: " + arg);
        }
    }
    return opts;
}

std::string info_or_unknown(const rs2::device &dev, rs2_camera_info info)
{
    if (dev.supports(info))
        return dev.get_info(info);
    return "unknown";
}

void enable_sched_fifo(int priority)
{
    const int min_priority = sched_get_priority_min(SCHED_FIFO);
    const int max_priority = sched_get_priority_max(SCHED_FIFO);
    if (min_priority == -1 || max_priority == -1)
    {
        throw std::runtime_error(
            std::string("sched_get_priority_min/max(SCHED_FIFO) failed: ") + std::strerror(errno));
    }
    if (priority < min_priority || priority > max_priority)
    {
        throw std::runtime_error(
            "SCHED_FIFO priority must be in [" + std::to_string(min_priority) + ", "
            + std::to_string(max_priority) + "], got " + std::to_string(priority));
    }

    sched_param param{};
    param.sched_priority = priority;
    const int result = pthread_setschedparam(pthread_self(), SCHED_FIFO, &param);
    if (result != 0)
    {
        throw std::runtime_error(
            "pthread_setschedparam(SCHED_FIFO, priority=" + std::to_string(priority)
            + ") failed: " + std::strerror(result)
            + ". Run as root or grant CAP_SYS_NICE to the executable.");
    }

    int policy = 0;
    sched_param actual{};
    const int get_result = pthread_getschedparam(pthread_self(), &policy, &actual);
    if (get_result == 0)
    {
        std::cout << "Scheduling policy: "
                  << (policy == SCHED_FIFO ? "SCHED_FIFO" : std::to_string(policy))
                  << " priority=" << actual.sched_priority << '\n';
    }
}

std::string sensor_name_or_unknown(const rs2::sensor &sensor)
{
    if (sensor.supports(RS2_CAMERA_INFO_NAME))
        return sensor.get_info(RS2_CAMERA_INFO_NAME);
    return "unknown";
}

void print_stream_profile(const std::string &prefix, const rs2::stream_profile &profile)
{
    std::cout << prefix << rs2_stream_to_string(profile.stream_type())
              << " #" << profile.stream_index()
              << " " << rs2_format_to_string(profile.format());
    if (profile.is<rs2::video_stream_profile>())
    {
        const auto video = profile.as<rs2::video_stream_profile>();
        std::cout << " " << video.width() << "x" << video.height();
    }
    std::cout << " @" << profile.fps() << "Hz\n";
}

void print_sensor_profiles(const rs2::device_list &devices)
{
    for (auto &&dev : devices)
    {
        const auto sensors = dev.query_sensors();
        std::cout << "  sensors=" << sensors.size() << '\n';
        for (auto &&sensor : sensors)
        {
            const auto profiles = sensor.get_stream_profiles();
            std::map<std::pair<rs2_stream, int>, int> stream_counts;
            for (auto &&profile : profiles)
                ++stream_counts[{profile.stream_type(), profile.stream_index()}];

            std::cout << "    sensor=" << sensor_name_or_unknown(sensor)
                      << " profiles=" << profiles.size() << '\n';
            for (const auto &entry : stream_counts)
            {
                std::cout << "      stream=" << rs2_stream_to_string(entry.first.first)
                          << " #" << entry.first.second
                          << " profile_count=" << entry.second << '\n';
            }
        }
    }
}

void print_active_streams(const rs2::pipeline_profile &profile)
{
    std::cout << "Active streams:\n";
    for (auto &&stream : profile.get_streams())
        print_stream_profile("  ", stream);
}

void print_devices(const rs2::device_list &devices)
{
    std::cout << "RealSense devices: " << devices.size() << '\n';
    for (auto &&dev : devices)
    {
        std::cout << "  name=" << info_or_unknown(dev, RS2_CAMERA_INFO_NAME)
                  << " serial=" << info_or_unknown(dev, RS2_CAMERA_INFO_SERIAL_NUMBER)
                  << " firmware=" << info_or_unknown(dev, RS2_CAMERA_INFO_FIRMWARE_VERSION)
                  << " physical_port=" << info_or_unknown(dev, RS2_CAMERA_INFO_PHYSICAL_PORT)
                  << '\n';
    }
}
} // namespace

int main(int argc, char **argv)
try
{
    pthread_setname_np(pthread_self(), "rs-trace-main");
    const auto opts = parse_args(argc, argv);
    if (opts.sched_fifo)
        enable_sched_fifo(opts.sched_fifo_priority);

    rs_trace_phase_marker("process_start");

    rs_trace_phase_marker("before_context");
    auto ctx = std::make_unique<rs2::context>();
    rs_trace_phase_marker("after_context");

    rs_trace_phase_marker("before_query_devices");
    auto devices = ctx->query_devices();
    print_devices(devices);
    print_sensor_profiles(devices);
    rs_trace_phase_marker("after_query_devices");

    if (devices.size() == 0)
        throw std::runtime_error("No RealSense devices found");
    if (opts.list_profiles_only)
        return 0;

    rs_trace_phase_marker("before_pipeline_construction");
    auto pipe = std::make_unique<rs2::pipeline>(*ctx);
    rs_trace_phase_marker("after_pipeline_construction");

    rs2::config cfg;
    if (!opts.serial.empty())
        cfg.enable_device(opts.serial);
    if (opts.d435_max_streams)
    {
        std::cout << "Stream preset: D435 max hardware streams\n";
        cfg.enable_stream(RS2_STREAM_DEPTH, 640, 480, RS2_FORMAT_Z16, 30);
        cfg.enable_stream(RS2_STREAM_INFRARED, 1, 640, 480, RS2_FORMAT_Y8, 30);
        cfg.enable_stream(RS2_STREAM_INFRARED, 2, 640, 480, RS2_FORMAT_Y8, 30);
        cfg.enable_stream(RS2_STREAM_COLOR, 640, 480, RS2_FORMAT_BGR8, 30);
    }
    else
    {
        cfg.enable_stream(RS2_STREAM_DEPTH, 640, 480, RS2_FORMAT_Z16, 30);
        cfg.enable_stream(RS2_STREAM_COLOR, 640, 480, RS2_FORMAT_BGR8, 30);
    }

    rs_trace_phase_marker("before_pipeline_start");
    auto profile = pipe->start(cfg);
    print_active_streams(profile);
    rs_trace_phase_marker("after_pipeline_start");

    if (opts.sleep_after_start_ms > 0)
    {
        std::this_thread::sleep_for(std::chrono::milliseconds(opts.sleep_after_start_ms));
        rs_trace_phase_marker("steady_state_begin");
    }
    else
    {
        bool first_frame_marked = false;
        bool steady_state_marked = false;
        for (int i = 0; i < opts.frames; ++i)
        {
            auto frames = pipe->wait_for_frames();
            (void)frames;

            if (!first_frame_marked)
            {
                rs_trace_phase_marker("first_frame");
                first_frame_marked = true;
            }

            if (!steady_state_marked && i >= opts.steady_state_after)
            {
                rs_trace_phase_marker("steady_state_begin");
                steady_state_marked = true;
            }
        }
    }

    rs_trace_phase_marker("before_pipeline_stop");
    pipe->stop();
    rs_trace_phase_marker("after_pipeline_stop");

    std::this_thread::sleep_for(std::chrono::seconds(1));

    rs_trace_phase_marker("before_object_destruction");
    pipe.reset();
    ctx.reset();
    rs_trace_phase_marker("after_object_destruction");

    std::this_thread::sleep_for(std::chrono::milliseconds(100));
    rs_trace_phase_marker("before_process_exit");
    return 0;
}
catch (const rs2::error &e)
{
    std::cerr << "RealSense error: " << e.what() << '\n';
    rs_trace_phase_marker("process_error");
    return 2;
}
catch (const std::exception &e)
{
    std::cerr << "Error: " << e.what() << '\n';
    rs_trace_phase_marker("process_error");
    return 1;
}
