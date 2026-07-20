#include <librealsense2/rs.hpp>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdlib>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <map>
#include <mutex>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

namespace
{
using clock_type = std::chrono::steady_clock;

struct options
{
    std::string serial;
    std::string stream_mode = "depth_color";
    std::string delivery = "wait";
    std::string output;
    int width = 640;
    int height = 480;
    int fps = 30;
    int duration_sec = 30;
    int warmup_frames = 30;
    int timeout_ms = 5000;
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

struct stream_metrics
{
    uint64_t frames = 0;
    uint64_t drops = 0;
    bool has_last_frame_number = false;
    uint64_t last_frame_number = 0;
    bool has_last_sensor_timestamp = false;
    double last_sensor_timestamp_ms = 0.0;
    bool has_last_host_time = false;
    clock_type::time_point last_host_time;
    std::string timestamp_domain;
    std::vector<double> sensor_interarrival_ms;
    std::vector<double> host_interarrival_ms;
};

struct run_metrics
{
    uint64_t framesets = 0;
    uint64_t callbacks = 0;
    uint64_t frames = 0;
    uint64_t timeouts = 0;
    std::vector<double> wait_ms;
    std::vector<double> frameset_interarrival_ms;
    std::vector<double> callback_gap_ms;
    std::map<std::string, stream_metrics> streams;
};

options parse_args(int argc, char **argv)
{
    options opts;
    for (int i = 1; i < argc; ++i)
    {
        const std::string arg = argv[i];
        auto require_value = [&](const std::string &name) -> std::string {
            if (i + 1 >= argc)
                throw std::runtime_error("Missing value for " + name);
            return argv[++i];
        };

        if (arg == "--serial")
            opts.serial = require_value(arg);
        else if (arg == "--stream-mode")
            opts.stream_mode = require_value(arg);
        else if (arg == "--delivery")
            opts.delivery = require_value(arg);
        else if (arg == "--width")
            opts.width = std::stoi(require_value(arg));
        else if (arg == "--height")
            opts.height = std::stoi(require_value(arg));
        else if (arg == "--fps")
            opts.fps = std::stoi(require_value(arg));
        else if (arg == "--duration-sec")
            opts.duration_sec = std::stoi(require_value(arg));
        else if (arg == "--warmup-frames")
            opts.warmup_frames = std::stoi(require_value(arg));
        else if (arg == "--timeout-ms")
            opts.timeout_ms = std::stoi(require_value(arg));
        else if (arg == "--output")
            opts.output = require_value(arg);
        else if (arg == "--help" || arg == "-h")
        {
            std::cout
                << "Usage: " << argv[0] << " [options]\n"
                << "  --serial SERIAL\n"
                << "  --stream-mode depth|depth_color|d435_all\n"
                << "  --delivery wait|callback\n"
                << "  --width N --height N --fps N\n"
                << "  --duration-sec N --warmup-frames N --timeout-ms N\n"
                << "  --output PATH\n";
            std::exit(0);
        }
        else
        {
            throw std::runtime_error("Unknown argument: " + arg);
        }
    }
    return opts;
}

std::string json_escape(const std::string &value)
{
    std::ostringstream out;
    for (char c : value)
    {
        switch (c)
        {
        case '\\': out << "\\\\"; break;
        case '"': out << "\\\""; break;
        case '\n': out << "\\n"; break;
        case '\r': out << "\\r"; break;
        case '\t': out << "\\t"; break;
        default:
            if (static_cast<unsigned char>(c) < 0x20)
                out << "\\u" << std::hex << std::setw(4) << std::setfill('0') << static_cast<int>(c);
            else
                out << c;
        }
    }
    return out.str();
}

std::string quoted(const std::string &value)
{
    return "\"" + json_escape(value) + "\"";
}

double ms_between(clock_type::time_point a, clock_type::time_point b)
{
    return std::chrono::duration<double, std::milli>(b - a).count();
}

stats summarize(std::vector<double> values)
{
    stats s;
    s.n = values.size();
    if (values.empty())
        return s;

    std::sort(values.begin(), values.end());
    s.min = values.front();
    s.max = values.back();
    double sum = 0.0;
    for (double v : values)
        sum += v;
    s.mean = sum / static_cast<double>(values.size());

    double sq = 0.0;
    for (double v : values)
    {
        const double d = v - s.mean;
        sq += d * d;
    }
    s.stddev = std::sqrt(sq / static_cast<double>(values.size()));

    auto percentile = [&](double p) {
        if (values.size() == 1)
            return values.front();
        const double pos = p * static_cast<double>(values.size() - 1);
        const auto lo = static_cast<size_t>(std::floor(pos));
        const auto hi = static_cast<size_t>(std::ceil(pos));
        const double frac = pos - static_cast<double>(lo);
        return values[lo] * (1.0 - frac) + values[hi] * frac;
    };

    s.p50 = percentile(0.50);
    s.p90 = percentile(0.90);
    s.p99 = percentile(0.99);
    s.p999 = percentile(0.999);
    return s;
}

void write_stats(std::ostream &out, const stats &s)
{
    out << "{"
        << "\"n\":" << s.n
        << ",\"min\":" << s.min
        << ",\"max\":" << s.max
        << ",\"mean\":" << s.mean
        << ",\"stddev\":" << s.stddev
        << ",\"p50\":" << s.p50
        << ",\"p90\":" << s.p90
        << ",\"p99\":" << s.p99
        << ",\"p999\":" << s.p999
        << "}";
}

std::string device_info_or_unknown(const rs2::device &dev, rs2_camera_info info)
{
    if (dev.supports(info))
        return dev.get_info(info);
    return "unknown";
}

std::string stream_key(const rs2::frame &frame)
{
    const auto profile = frame.get_profile();
    return std::string(rs2_stream_to_string(profile.stream_type())) + "#" + std::to_string(profile.stream_index());
}

void configure_streams(rs2::config &cfg, const options &opts)
{
    if (!opts.serial.empty())
        cfg.enable_device(opts.serial);

    if (opts.stream_mode == "depth")
    {
        cfg.enable_stream(RS2_STREAM_DEPTH, opts.width, opts.height, RS2_FORMAT_Z16, opts.fps);
    }
    else if (opts.stream_mode == "depth_color")
    {
        cfg.enable_stream(RS2_STREAM_DEPTH, opts.width, opts.height, RS2_FORMAT_Z16, opts.fps);
        cfg.enable_stream(RS2_STREAM_COLOR, opts.width, opts.height, RS2_FORMAT_BGR8, opts.fps);
    }
    else if (opts.stream_mode == "d435_all")
    {
        cfg.enable_stream(RS2_STREAM_DEPTH, opts.width, opts.height, RS2_FORMAT_Z16, opts.fps);
        cfg.enable_stream(RS2_STREAM_INFRARED, 1, opts.width, opts.height, RS2_FORMAT_Y8, opts.fps);
        cfg.enable_stream(RS2_STREAM_INFRARED, 2, opts.width, opts.height, RS2_FORMAT_Y8, opts.fps);
        cfg.enable_stream(RS2_STREAM_COLOR, opts.width, opts.height, RS2_FORMAT_BGR8, opts.fps);
    }
    else
    {
        throw std::runtime_error("Unsupported stream mode: " + opts.stream_mode);
    }
}

void record_frame(run_metrics &metrics, const rs2::frame &frame, clock_type::time_point host_time, bool measure)
{
    if (!frame)
        return;

    const auto key = stream_key(frame);
    auto &stream = metrics.streams[key];
    stream.timestamp_domain = rs2_timestamp_domain_to_string(frame.get_frame_timestamp_domain());

    const uint64_t frame_number = frame.get_frame_number();
    if (stream.has_last_frame_number && frame_number > stream.last_frame_number + 1)
        stream.drops += frame_number - stream.last_frame_number - 1;
    stream.has_last_frame_number = true;
    stream.last_frame_number = frame_number;

    const double timestamp_ms = frame.get_timestamp();
    if (measure)
    {
        stream.frames += 1;
        metrics.frames += 1;
        if (stream.has_last_sensor_timestamp)
            stream.sensor_interarrival_ms.push_back(timestamp_ms - stream.last_sensor_timestamp_ms);
        if (stream.has_last_host_time)
            stream.host_interarrival_ms.push_back(ms_between(stream.last_host_time, host_time));
    }

    stream.has_last_sensor_timestamp = true;
    stream.last_sensor_timestamp_ms = timestamp_ms;
    stream.has_last_host_time = true;
    stream.last_host_time = host_time;
}

void record_frame_or_frameset(run_metrics &metrics, const rs2::frame &frame, clock_type::time_point host_time, bool measure)
{
    if (frame.is<rs2::frameset>())
    {
        const auto frames = frame.as<rs2::frameset>();
        for (auto &&child : frames)
            record_frame(metrics, child, host_time, measure);
    }
    else
    {
        record_frame(metrics, frame, host_time, measure);
    }
}

run_metrics run_wait_mode(rs2::pipeline &pipe, const options &opts)
{
    run_metrics metrics;
    bool has_last_frameset_time = false;
    clock_type::time_point last_frameset_time;
    int warmup_remaining = opts.warmup_frames;

    auto measurement_start = clock_type::now();
    bool measurement_started = false;
    while (true)
    {
        const auto wait_begin = clock_type::now();
        rs2::frameset frames;
        try
        {
            frames = pipe.wait_for_frames(opts.timeout_ms);
        }
        catch (const rs2::error &)
        {
            metrics.timeouts += 1;
            throw;
        }
        const auto wait_end = clock_type::now();
        const bool measure = warmup_remaining <= 0;
        if (!measure)
        {
            --warmup_remaining;
        }
        else
        {
            if (!measurement_started)
            {
                measurement_start = wait_end;
                measurement_started = true;
            }
            metrics.framesets += 1;
            metrics.wait_ms.push_back(ms_between(wait_begin, wait_end));
            if (has_last_frameset_time)
                metrics.frameset_interarrival_ms.push_back(ms_between(last_frameset_time, wait_end));
        }

        for (auto &&frame : frames)
            record_frame(metrics, frame, wait_end, measure);

        if (measure)
        {
            has_last_frameset_time = true;
            last_frameset_time = wait_end;
            if (ms_between(measurement_start, wait_end) >= static_cast<double>(opts.duration_sec) * 1000.0)
                break;
        }
    }
    return metrics;
}

run_metrics run_callback_mode(rs2::pipeline &pipe, rs2::config &cfg, const options &opts)
{
    run_metrics metrics;
    std::mutex mutex;
    std::condition_variable cv;
    bool measurement_started = false;
    clock_type::time_point measurement_start;
    bool has_last_callback_time = false;
    clock_type::time_point last_callback_time;
    int warmup_remaining = opts.warmup_frames;

    auto callback = [&](const rs2::frame &frame) {
        const auto now = clock_type::now();
        std::lock_guard<std::mutex> lock(mutex);
        const bool measure = warmup_remaining <= 0;
        if (!measure)
        {
            --warmup_remaining;
        }
        else
        {
            if (!measurement_started)
            {
                measurement_start = now;
                measurement_started = true;
            }
            metrics.callbacks += 1;
            if (has_last_callback_time)
                metrics.callback_gap_ms.push_back(ms_between(last_callback_time, now));
            has_last_callback_time = true;
            last_callback_time = now;
        }
        record_frame_or_frameset(metrics, frame, now, measure);
        cv.notify_all();
    };

    auto profile = pipe.start(cfg, callback);
    (void)profile;

    std::unique_lock<std::mutex> lock(mutex);
    cv.wait_for(lock, std::chrono::seconds(opts.timeout_ms / 1000 + 1), [&]() { return measurement_started; });
    while (!measurement_started || ms_between(measurement_start, clock_type::now()) < static_cast<double>(opts.duration_sec) * 1000.0)
        cv.wait_for(lock, std::chrono::milliseconds(20));
    lock.unlock();

    pipe.stop();
    return metrics;
}

void write_json(std::ostream &out, const options &opts, const rs2::pipeline_profile &profile, const run_metrics &metrics)
{
    const auto dev = profile.get_device();
    out << std::fixed << std::setprecision(6);
    out << "{\n";
    out << "  \"schema_version\": 1,\n";
    out << "  \"run\": {"
        << "\"stream_mode\":" << quoted(opts.stream_mode)
        << ",\"delivery\":" << quoted(opts.delivery)
        << ",\"width\":" << opts.width
        << ",\"height\":" << opts.height
        << ",\"fps\":" << opts.fps
        << ",\"duration_sec\":" << opts.duration_sec
        << ",\"warmup_frames\":" << opts.warmup_frames
        << "},\n";
    out << "  \"device\": {"
        << "\"name\":" << quoted(device_info_or_unknown(dev, RS2_CAMERA_INFO_NAME))
        << ",\"serial\":" << quoted(device_info_or_unknown(dev, RS2_CAMERA_INFO_SERIAL_NUMBER))
        << ",\"firmware\":" << quoted(device_info_or_unknown(dev, RS2_CAMERA_INFO_FIRMWARE_VERSION))
        << ",\"physical_port\":" << quoted(device_info_or_unknown(dev, RS2_CAMERA_INFO_PHYSICAL_PORT))
        << ",\"usb_type\":" << quoted(device_info_or_unknown(dev, RS2_CAMERA_INFO_USB_TYPE_DESCRIPTOR))
        << "},\n";
    out << "  \"active_streams\": [";
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
    out << "],\n";

    uint64_t total_drops = 0;
    for (const auto &entry : metrics.streams)
        total_drops += entry.second.drops;

    out << "  \"summary\": {\n";
    out << "    \"framesets\": " << metrics.framesets << ",\n";
    out << "    \"callbacks\": " << metrics.callbacks << ",\n";
    out << "    \"frames\": " << metrics.frames << ",\n";
    out << "    \"drops\": " << total_drops << ",\n";
    out << "    \"timeouts\": " << metrics.timeouts << ",\n";
    out << "    \"wait_ms\": "; write_stats(out, summarize(metrics.wait_ms)); out << ",\n";
    out << "    \"frameset_interarrival_ms\": "; write_stats(out, summarize(metrics.frameset_interarrival_ms)); out << ",\n";
    out << "    \"callback_gap_ms\": "; write_stats(out, summarize(metrics.callback_gap_ms)); out << ",\n";
    out << "    \"streams\": {";
    bool first_stream = true;
    for (const auto &entry : metrics.streams)
    {
        if (!first_stream)
            out << ",";
        first_stream = false;
        const auto &stream = entry.second;
        out << "\n      " << quoted(entry.first) << ": {"
            << "\"frames\":" << stream.frames
            << ",\"drops\":" << stream.drops
            << ",\"timestamp_domain\":" << quoted(stream.timestamp_domain)
            << ",\"sensor_interarrival_ms\":";
        write_stats(out, summarize(stream.sensor_interarrival_ms));
        out << ",\"host_interarrival_ms\":";
        write_stats(out, summarize(stream.host_interarrival_ms));
        out << "}";
    }
    out << "\n    }\n";
    out << "  }\n";
    out << "}\n";
}
} // namespace

int main(int argc, char **argv)
try
{
    const auto opts = parse_args(argc, argv);
    if (opts.delivery != "wait" && opts.delivery != "callback")
        throw std::runtime_error("Unsupported delivery mode: " + opts.delivery);

    rs2::pipeline pipe;
    rs2::config cfg;
    configure_streams(cfg, opts);

    rs2::pipeline_profile profile;
    run_metrics metrics;
    if (opts.delivery == "wait")
    {
        profile = pipe.start(cfg);
        metrics = run_wait_mode(pipe, opts);
        pipe.stop();
    }
    else
    {
        profile = cfg.resolve(pipe);
        metrics = run_callback_mode(pipe, cfg, opts);
    }

    std::ostringstream json;
    write_json(json, opts, profile, metrics);
    std::cout << json.str();
    if (!opts.output.empty())
    {
        std::ofstream file(opts.output);
        file << json.str();
    }
    return 0;
}
catch (const rs2::error &e)
{
    std::cerr << "RealSense error: " << e.what() << '\n';
    return 2;
}
catch (const std::exception &e)
{
    std::cerr << "Error: " << e.what() << '\n';
    return 1;
}
