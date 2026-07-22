#include "rs_camera/trace_marker.h"

#include <librealsense2/rs.hpp>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <csignal>
#include <cstdlib>
#include <cctype>
#include <dirent.h>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <map>
#include <mutex>
#include <pthread.h>
#include <sched.h>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <tuple>
#include <unistd.h>
#include <vector>

namespace
{
using clock_type = std::chrono::steady_clock;

std::atomic_bool keep_running{true};

struct options
{
    std::string serial;
    int frames = 0;
    int cycles = 1;
    int frame_timeout_ms = 1500;
    int join_timeout_ms = 10;
    int cycle_delay_ms = 0;
    int reset_timeout_ms = 5000;
    bool hardware_reset = false;
    bool enable_all_streams = false;
    bool try_all_ir = true;
    bool strict_streams = false;
    bool list_only = false;
};

struct thread_info
{
    int tid = 0;
    std::string name;
};

struct stream_key
{
    rs2_stream stream = RS2_STREAM_ANY;
    int index = -1;

    bool operator<(const stream_key &other) const
    {
        return std::make_pair(stream, index) < std::make_pair(other.stream, other.index);
    }
};

struct selected_profile
{
    rs2::stream_profile profile;
    int score = 0;
};

void on_signal(int)
{
    keep_running = false;
}

double elapsed_ms(clock_type::time_point begin, clock_type::time_point end)
{
    return std::chrono::duration<double, std::milli>(end - begin).count();
}

std::string read_first_line(const std::string &path)
{
    std::ifstream in(path);
    std::string line;
    std::getline(in, line);
    return line;
}

std::vector<thread_info> read_threads()
{
    std::vector<thread_info> threads;
    DIR *dir = opendir("/proc/self/task");
    if (!dir)
        return threads;

    while (auto *entry = readdir(dir))
    {
        const std::string name = entry->d_name;
        if (name.empty()
            || !std::all_of(name.begin(), name.end(), [](unsigned char ch) { return std::isdigit(ch); }))
            continue;

        threads.push_back({std::stoi(name), read_first_line("/proc/self/task/" + name + "/comm")});
    }
    closedir(dir);
    std::sort(threads.begin(), threads.end(), [](const auto &a, const auto &b) { return a.tid < b.tid; });
    return threads;
}

std::set<int> thread_ids(const std::vector<thread_info> &threads)
{
    std::set<int> result;
    for (const auto &thread : threads)
        result.insert(thread.tid);
    return result;
}

std::vector<thread_info> extra_threads(const std::set<int> &baseline)
{
    std::vector<thread_info> result;
    for (const auto &thread : read_threads())
    {
        if (baseline.count(thread.tid) == 0)
            result.push_back(thread);
    }
    return result;
}

std::vector<thread_info> wait_for_threads_to_join(const std::set<int> &baseline, int timeout_ms)
{
    const auto deadline = clock_type::now() + std::chrono::milliseconds(timeout_ms);
    auto extra = extra_threads(baseline);
    while (!extra.empty() && clock_type::now() < deadline)
    {
        std::this_thread::sleep_for(std::chrono::milliseconds(5));
        extra = extra_threads(baseline);
    }
    return extra;
}

std::string cycle_marker(int cycle, const std::string &phase)
{
    std::ostringstream out;
    out << "cycle_" << std::setw(2) << std::setfill('0') << cycle << "_" << phase;
    return out.str();
}

void mark_cycle(int cycle, const std::string &phase)
{
    const auto marker = cycle_marker(cycle, phase);
    rs_trace_phase_marker(marker.c_str());
}

std::string json_escape(const std::string &value)
{
    std::ostringstream out;
    for (const unsigned char ch : value)
    {
        switch (ch)
        {
        case '\\': out << "\\\\"; break;
        case '"': out << "\\\""; break;
        case '\n': out << "\\n"; break;
        case '\r': out << "\\r"; break;
        case '\t': out << "\\t"; break;
        default:
            if (ch >= 0x20)
                out << static_cast<char>(ch);
        }
    }
    return out.str();
}

std::string scheduler_name(int policy)
{
    switch (policy)
    {
    case SCHED_OTHER: return "SCHED_OTHER";
    case SCHED_RR: return "SCHED_RR";
    case SCHED_FIFO: return "SCHED_FIFO";
#ifdef SCHED_BATCH
    case SCHED_BATCH: return "SCHED_BATCH";
#endif
#ifdef SCHED_IDLE
    case SCHED_IDLE: return "SCHED_IDLE";
#endif
#ifdef SCHED_DEADLINE
    case SCHED_DEADLINE: return "SCHED_DEADLINE";
#endif
    default: return "UNKNOWN(" + std::to_string(policy) + ")";
    }
}

void print_scheduler()
{
    const int policy = sched_getscheduler(0);
    sched_param param{};
    if (policy < 0 || sched_getparam(0, &param) != 0)
        throw std::runtime_error("Unable to read the effective scheduling policy");

    std::cout << "RS_SCHEDULER {\"policy\":\"" << scheduler_name(policy)
              << "\",\"policy_id\":" << policy
              << ",\"priority\":" << param.sched_priority << "}\n";
}

std::string info_or_unknown(const rs2::device &dev, rs2_camera_info info)
{
    return dev.supports(info) ? dev.get_info(info) : "unknown";
}

rs2::device select_device(rs2::context &ctx, const std::string &serial)
{
    const auto devices = ctx.query_devices();
    if (devices.size() == 0)
        throw std::runtime_error("No RealSense devices found");

    for (const auto &dev : devices)
    {
        if (serial.empty()
            || (dev.supports(RS2_CAMERA_INFO_SERIAL_NUMBER)
                && serial == dev.get_info(RS2_CAMERA_INFO_SERIAL_NUMBER)))
            return dev;
    }
    throw std::runtime_error("Requested serial number was not found: " + serial);
}

void hardware_reset_and_wait(const std::string &requested_serial, int timeout_ms)
{
    rs2::context ctx;
    auto dev = select_device(ctx, requested_serial);
    const auto serial = info_or_unknown(dev, RS2_CAMERA_INFO_SERIAL_NUMBER);
    const auto physical_port = info_or_unknown(dev, RS2_CAMERA_INFO_PHYSICAL_PORT);
    std::mutex mutex;
    std::condition_variable changed;
    bool removed = false;
    bool added = false;

    ctx.set_devices_changed_callback([&](rs2::event_information info) {
        std::lock_guard<std::mutex> lock(mutex);
        if (info.was_removed(dev))
            removed = true;
        for (const auto &candidate : info.get_new_devices())
        {
            try
            {
                if (candidate.supports(RS2_CAMERA_INFO_SERIAL_NUMBER)
                    && serial == candidate.get_info(RS2_CAMERA_INFO_SERIAL_NUMBER))
                    added = true;
            }
            catch (const rs2::error &)
            {
            }
        }
        changed.notify_all();
    });

    std::cout << "RS_HARDWARE_RESET {\"state\":\"requested\",\"serial\":\""
              << json_escape(serial) << "\",\"physical_port\":\""
              << json_escape(physical_port) << "\"}\n" << std::flush;
    std::this_thread::sleep_for(std::chrono::seconds(1));
    dev.hardware_reset();

    const auto deadline = clock_type::now() + std::chrono::milliseconds(timeout_ms);
    std::unique_lock<std::mutex> lock(mutex);
    changed.wait_until(lock, deadline, [&] { return removed || added; });
    if (!removed && !added)
        throw std::runtime_error("No disconnect/reconnect event after camera hardware reset");
    if (!added)
        changed.wait_until(lock, deadline, [&] { return added; });
    if (!added)
        throw std::runtime_error("Camera did not reconnect after hardware reset");
    lock.unlock();
    ctx.set_devices_changed_callback([](rs2::event_information) {});
    std::cout << "RS_HARDWARE_RESET {\"state\":\"complete\",\"serial\":\""
              << json_escape(serial) << "\",\"removed_observed\":"
              << (removed ? "true" : "false") << "}\n";
}

std::string profile_to_string(const rs2::stream_profile &profile)
{
    std::ostringstream out;
    out << profile.stream_name() << " idx=" << profile.stream_index()
        << " fmt=" << rs2_format_to_string(profile.format())
        << " fps=" << profile.fps();
    if (auto video = profile.as<rs2::video_stream_profile>())
        out << " " << video.width() << "x" << video.height();
    return out.str();
}

bool wanted_stream(rs2_stream stream)
{
    return stream == RS2_STREAM_COLOR || stream == RS2_STREAM_DEPTH || stream == RS2_STREAM_INFRARED
        || stream == RS2_STREAM_GYRO || stream == RS2_STREAM_ACCEL;
}

int profile_score(const rs2::stream_profile &profile)
{
    int score = profile.is_default() ? 5000 : 0;
    if (auto video = profile.as<rs2::video_stream_profile>())
    {
        score += video.fps() == 30 ? 1000 : -std::abs(video.fps() - 30);
        if (video.stream_type() == RS2_STREAM_DEPTH && video.format() == RS2_FORMAT_Z16)
            score += 3000;
        if (video.stream_type() == RS2_STREAM_INFRARED && video.format() == RS2_FORMAT_Y8)
            score += 3000;
        if (video.stream_type() == RS2_STREAM_COLOR)
        {
            if (video.format() == RS2_FORMAT_RGB8 || video.format() == RS2_FORMAT_BGR8)
                score += 3000;
            if (video.width() == 640 && video.height() == 480)
                score += 1000;
        }
        else if (video.width() == 848 && video.height() == 480)
            score += 1000;
    }
    else if (profile.format() == RS2_FORMAT_MOTION_XYZ32F)
    {
        score += 3000;
    }
    return score;
}

std::vector<selected_profile> choose_profiles(const rs2::device &dev, bool include_all_ir)
{
    std::map<stream_key, std::vector<rs2::stream_profile>> grouped;
    for (const auto &sensor : dev.query_sensors())
    {
        for (const auto &profile : sensor.get_stream_profiles())
        {
            if (wanted_stream(profile.stream_type()))
                grouped[{profile.stream_type(), profile.stream_index()}].push_back(profile);
        }
    }

    std::vector<selected_profile> result;
    bool have_ir = false;
    for (auto &entry : grouped)
    {
        if (entry.first.stream == RS2_STREAM_INFRARED && have_ir && !include_all_ir)
            continue;
        auto best = std::max_element(entry.second.begin(), entry.second.end(), [](const auto &a, const auto &b) {
            return profile_score(a) < profile_score(b);
        });
        if (best != entry.second.end())
        {
            result.push_back({*best, profile_score(*best)});
            if (entry.first.stream == RS2_STREAM_INFRARED)
                have_ir = true;
        }
    }
    std::sort(result.begin(), result.end(), [](const auto &a, const auto &b) {
        return stream_key{a.profile.stream_type(), a.profile.stream_index()}
            < stream_key{b.profile.stream_type(), b.profile.stream_index()};
    });
    return result;
}

void enable_profile(rs2::config &cfg, const rs2::stream_profile &profile)
{
    if (auto video = profile.as<rs2::video_stream_profile>())
    {
        cfg.enable_stream(profile.stream_type(),
                          profile.stream_index(),
                          video.width(),
                          video.height(),
                          profile.format(),
                          profile.fps());
    }
    else
    {
        cfg.enable_stream(profile.stream_type(), profile.stream_index(), profile.format(), profile.fps());
    }
}

rs2::pipeline_profile start_pipeline(rs2::pipeline &pipe,
                                     const options &opts,
                                     const std::vector<selected_profile> &selected)
{
    rs2::config cfg;
    if (!opts.serial.empty())
        cfg.enable_device(opts.serial);

    if (opts.enable_all_streams)
    {
        cfg.enable_all_streams();
    }
    else
    {
        if (selected.empty())
            throw std::runtime_error("No RGB/depth/infrared/motion stream profiles were found");
        for (const auto &item : selected)
            enable_profile(cfg, item.profile);
    }
    return pipe.start(cfg);
}

void print_inventory(const rs2::device &dev, const std::vector<selected_profile> &selected)
{
    std::cout << "RS_DEVICE {\"name\":\""
              << json_escape(info_or_unknown(dev, RS2_CAMERA_INFO_NAME))
              << "\",\"serial\":\""
              << json_escape(info_or_unknown(dev, RS2_CAMERA_INFO_SERIAL_NUMBER))
              << "\",\"physical_port\":\""
              << json_escape(info_or_unknown(dev, RS2_CAMERA_INFO_PHYSICAL_PORT))
              << "\",\"product_id\":\""
              << json_escape(info_or_unknown(dev, RS2_CAMERA_INFO_PRODUCT_ID))
              << "\"}\n";
    std::cout << "Device: " << info_or_unknown(dev, RS2_CAMERA_INFO_NAME)
              << " serial=" << info_or_unknown(dev, RS2_CAMERA_INFO_SERIAL_NUMBER)
              << " firmware=" << info_or_unknown(dev, RS2_CAMERA_INFO_FIRMWARE_VERSION)
              << " usb=" << info_or_unknown(dev, RS2_CAMERA_INFO_USB_TYPE_DESCRIPTOR) << '\n';
    std::cout << "Selected stream requests (" << selected.size() << "):\n";
    for (const auto &item : selected)
        std::cout << "  " << profile_to_string(item.profile) << " score=" << item.score << '\n';
}

void print_active_profiles(const rs2::pipeline_profile &profile)
{
    std::cout << "Active streams:\n";
    for (const auto &stream : profile.get_streams())
        std::cout << "  " << profile_to_string(stream) << '\n';
}

options parse_args(int argc, char **argv)
{
    options opts;
    for (int i = 1; i < argc; ++i)
    {
        const std::string arg = argv[i];
        auto value = [&]() -> std::string {
            if (i + 1 >= argc)
                throw std::runtime_error("Missing value for " + arg);
            return argv[++i];
        };

        if (arg == "--serial")
            opts.serial = value();
        else if (arg == "--frames")
            opts.frames = std::stoi(value());
        else if (arg == "--cycles")
            opts.cycles = std::stoi(value());
        else if (arg == "--frame-timeout-ms")
            opts.frame_timeout_ms = std::stoi(value());
        else if (arg == "--join-timeout-ms")
            opts.join_timeout_ms = std::stoi(value());
        else if (arg == "--cycle-delay-ms")
            opts.cycle_delay_ms = std::stoi(value());
        else if (arg == "--reset-timeout-ms")
            opts.reset_timeout_ms = std::stoi(value());
        else if (arg == "--hardware-reset")
            opts.hardware_reset = true;
        else if (arg == "--enable-all")
            opts.enable_all_streams = true;
        else if (arg == "--single-ir")
            opts.try_all_ir = false;
        else if (arg == "--strict-streams")
            opts.strict_streams = true;
        else if (arg == "--list-only")
            opts.list_only = true;
        else if (arg == "--help" || arg == "-h")
        {
            std::cout
                << "Usage: " << argv[0] << " [options]\n"
                << "  --serial SERIAL          select one camera\n"
                << "  --frames N               framesets per cycle (0 means run until signal)\n"
                << "  --cycles N               fully construct/start/stop/destroy N times\n"
                << "  --frame-timeout-ms N     timeout for each frame wait (default 1500)\n"
                << "  --join-timeout-ms N      wait for all cycle-created threads (default 10)\n"
                << "  --cycle-delay-ms N       delay after joining and before next cycle\n"
                << "  --hardware-reset         send D400 firmware reset and wait for reconnect\n"
                << "  --reset-timeout-ms N     hardware-reset reconnect timeout (default 5000)\n"
                << "  --enable-all             use config.enable_all_streams()\n"
                << "  --single-ir              request only the first infrared stream\n"
                << "  --strict-streams         fail instead of retrying with one infrared stream\n"
                << "  --list-only              print selected profiles without streaming\n";
            std::exit(0);
        }
        else
            throw std::runtime_error("Unknown argument: " + arg);
    }

    if (opts.cycles < 1 || opts.frames < 0 || opts.frame_timeout_ms <= 0
        || opts.join_timeout_ms < 0 || opts.cycle_delay_ms < 0
        || opts.reset_timeout_ms <= 0)
        throw std::runtime_error(
            "cycles, frame timeout, and reset timeout must be positive; "
            "frames and other timeouts must be non-negative");
    if (opts.cycles > 1 && opts.frames == 0)
        throw std::runtime_error("--cycles greater than one requires a finite --frames value");
    return opts;
}
}  // namespace

int main(int argc, char **argv)
try
{
    pthread_setname_np(pthread_self(), "d435-probe");
    std::signal(SIGINT, on_signal);
    std::signal(SIGTERM, on_signal);

    const auto opts = parse_args(argc, argv);
    if (opts.hardware_reset)
    {
        hardware_reset_and_wait(opts.serial, opts.reset_timeout_ms);
        return 0;
    }
    const auto baseline_threads = read_threads();
    const auto baseline_ids = thread_ids(baseline_threads);

    rs_trace_phase_marker("process_start");
    print_scheduler();

    int completed_cycles = 0;
    for (int cycle = 1; cycle <= opts.cycles && keep_running; ++cycle)
    {
        mark_cycle(cycle, "begin");
        const auto cycle_begin = clock_type::now();
        double start_call_ms = 0.0;
        double first_frame_ms = 0.0;
        double first_frame_wait_ms = 0.0;
        double stop_call_ms = 0.0;
        int framesets = 0;
        size_t threads_after_start = 0;

        mark_cycle(cycle, "before_context");
        auto ctx = std::make_unique<rs2::context>();
        mark_cycle(cycle, "after_context");

        auto dev = select_device(*ctx, opts.serial);
        auto selected = choose_profiles(dev, opts.try_all_ir);
        if (cycle == 1)
            print_inventory(dev, selected);
        if (opts.list_only)
        {
            rs_trace_phase_marker("process_exit");
            return 0;
        }

        mark_cycle(cycle, "before_pipeline_construction");
        auto pipe = std::make_unique<rs2::pipeline>(*ctx);
        mark_cycle(cycle, "after_pipeline_construction");

        mark_cycle(cycle, "before_pipeline_start");
        const auto start_begin = clock_type::now();
        rs2::pipeline_profile active;
        try
        {
            active = start_pipeline(*pipe, opts, selected);
        }
        catch (const std::exception &error)
        {
            const auto ir_count = std::count_if(selected.begin(), selected.end(), [](const auto &item) {
                return item.profile.stream_type() == RS2_STREAM_INFRARED;
            });
            if (opts.strict_streams || opts.enable_all_streams || ir_count <= 1)
                throw;

            std::cerr << "Multiple infrared streams failed: " << error.what()
                      << "\nRetrying this cycle with one infrared stream.\n";
            pipe.reset();
            selected = choose_profiles(dev, false);
            pipe = std::make_unique<rs2::pipeline>(*ctx);
            active = start_pipeline(*pipe, opts, selected);
        }
        const auto start_end = clock_type::now();
        start_call_ms = elapsed_ms(start_begin, start_end);
        mark_cycle(cycle, "after_pipeline_start");

        const auto after_start_threads = read_threads();
        threads_after_start = after_start_threads.size() >= baseline_threads.size()
            ? after_start_threads.size() - baseline_threads.size()
            : 0;
        if (cycle == 1)
            print_active_profiles(active);

        bool first_frame = true;
        while (keep_running && (opts.frames == 0 || framesets < opts.frames))
        {
            const auto wait_begin = clock_type::now();
            auto frames = pipe->wait_for_frames(
                static_cast<unsigned int>(opts.frame_timeout_ms));
            (void)frames;
            ++framesets;
            if (first_frame)
            {
                const auto first_frame_time = clock_type::now();
                first_frame_ms = elapsed_ms(cycle_begin, first_frame_time);
                first_frame_wait_ms = elapsed_ms(wait_begin, first_frame_time);
                mark_cycle(cycle, "first_frame");
                first_frame = false;
            }
        }
        mark_cycle(cycle, "frames_complete");

        mark_cycle(cycle, "before_pipeline_stop");
        const auto stop_begin = clock_type::now();
        pipe->stop();
        stop_call_ms = elapsed_ms(stop_begin, clock_type::now());
        mark_cycle(cycle, "after_pipeline_stop");

        mark_cycle(cycle, "before_object_destruction");
        pipe.reset();
        active = rs2::pipeline_profile();
        selected.clear();
        dev = rs2::device();
        ctx.reset();
        mark_cycle(cycle, "after_object_destruction");

        const auto join_begin = clock_type::now();
        const auto remaining = wait_for_threads_to_join(baseline_ids, opts.join_timeout_ms);
        const double join_wait_ms = elapsed_ms(join_begin, clock_type::now());
        if (remaining.empty())
            mark_cycle(cycle, "threads_joined");
        else
            mark_cycle(cycle, "thread_join_timeout");

        const double cycle_ms = elapsed_ms(cycle_begin, clock_type::now());
        const bool success = remaining.empty() && framesets == opts.frames;
        std::cout << std::fixed << std::setprecision(3)
                  << "RS_STARTUP_CYCLE {\"cycle\":" << cycle
                  << ",\"success\":" << (success ? "true" : "false")
                  << ",\"framesets\":" << framesets
                  << ",\"start_call_ms\":" << start_call_ms
                  << ",\"first_frame_ms\":" << first_frame_ms
                  << ",\"first_frame_wait_ms\":" << first_frame_wait_ms
                  << ",\"stop_call_ms\":" << stop_call_ms
                  << ",\"join_wait_ms\":" << join_wait_ms
                  << ",\"cycle_ms\":" << cycle_ms
                  << ",\"threads_after_start\":" << threads_after_start
                  << ",\"extra_threads_after_join\":" << remaining.size() << "}\n";

        if (!remaining.empty())
        {
            for (const auto &thread : remaining)
                std::cerr << "Thread did not join: tid=" << thread.tid << " name=" << thread.name << '\n';
            throw std::runtime_error("Timed out waiting for all cycle-created threads to exit");
        }
        if (framesets != opts.frames)
            break;

        ++completed_cycles;
        mark_cycle(cycle, "end");
        if (opts.cycle_delay_ms > 0 && cycle < opts.cycles)
            std::this_thread::sleep_for(std::chrono::milliseconds(opts.cycle_delay_ms));
    }

    std::cout << "RS_STARTUP_RESULT {\"success\":"
              << (completed_cycles == opts.cycles ? "true" : "false")
              << ",\"completed_cycles\":" << completed_cycles
              << ",\"requested_cycles\":" << opts.cycles << "}\n";
    rs_trace_phase_marker("process_exit");
    return completed_cycles == opts.cycles ? 0 : 3;
}
catch (const rs2::error &error)
{
    rs_trace_phase_marker("process_error");
    std::cout << "RS_STARTUP_ERROR {\"kind\":\"librealsense\",\"message\":\""
              << json_escape(error.what()) << "\"}\n";
    std::cerr << "RealSense error: " << error.what() << '\n';
    return 2;
}
catch (const std::exception &error)
{
    rs_trace_phase_marker("process_error");
    std::cout << "RS_STARTUP_ERROR {\"kind\":\"application\",\"message\":\""
              << json_escape(error.what()) << "\"}\n";
    std::cerr << "Error: " << error.what() << '\n';
    return 1;
}
