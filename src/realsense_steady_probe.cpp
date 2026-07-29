#include "rs_camera/trace_marker.h"

#include <librealsense2/rs.hpp>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdint>
#include <cstdlib>
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
    int measurement_timeout_ms = 0;
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
    uint64_t start_begin_ns = 0;
    uint64_t start_end_ns = 0;
    uint64_t stop_begin_ns = 0;
    uint64_t stop_end_ns = 0;
    uint64_t first_warmup_ns = 0;
    uint64_t first_measured_ns = 0;
    uint64_t last_measured_ns = 0;
    uint64_t last_delivery_ns = 0;
    bool warmed = false;
    bool completed = false;
    std::vector<double> delivery_interarrival_ms;
    std::vector<double> wait_ms;
    std::map<std::string, stream_metrics> streams;
    std::vector<frame_event> events;
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
    std::atomic<bool> stop{false};
    std::atomic<uint64_t> origin_ns{0};
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
        else if (arg == "--help" || arg == "-h")
        {
            std::cout
                << "Usage: " << argv[0] << " [options]\n"
                << "  --serial SERIAL              Repeat for each selected camera\n"
                << "  --camera-count N             Select the first N cameras when serials are omitted\n"
                << "  --stream-mode depth|depth_color|d435_all\n"
                << "  --delivery wait|callback\n"
                << "  --frames N                   Measured deliveries per camera\n"
                << "  --warmup-frames N\n"
                << "  --frame-timeout-ms N --startup-timeout-ms N\n"
                << "  --measurement-timeout-ms N   Zero selects an automatic deadline\n"
                << "  --fps N\n"
                << "  --depth-width N --depth-height N\n"
                << "  --color-width N --color-height N\n"
                << "  --summary-output PATH --events-output PATH\n";
            std::exit(0);
        }
        else
            throw std::runtime_error("Unknown argument: " + arg);
    }

    if (!opts.serials.empty())
        opts.camera_count = static_cast<int>(opts.serials.size());
    if (opts.camera_count <= 0 || opts.frames <= 0 || opts.fps <= 0)
        throw std::runtime_error("camera-count, frames, and fps must be positive");
    if (opts.warmup_frames < 0 || opts.frame_timeout_ms <= 0 || opts.startup_timeout_ms <= 0)
        throw std::runtime_error("Invalid warm-up or timeout value");
    if (opts.delivery != "wait" && opts.delivery != "callback")
        throw std::runtime_error("Unsupported delivery mode: " + opts.delivery);
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

void record_delivery(camera_runtime &camera,
                     const rs2::frame &frame,
                     uint64_t host_ns,
                     double wait_ms,
                     const options &opts,
                     shared_control &control)
{
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
            if (metrics.warmup_deliveries >= static_cast<uint64_t>(opts.warmup_frames))
            {
                metrics.warmed = true;
                became_warm = true;
            }
        }
        else if (control.measurement_enabled.load())
        {
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

            if (metrics.deliveries >= static_cast<uint64_t>(opts.frames))
            {
                metrics.completed = true;
                became_complete = true;
            }
        }
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
            }
            set_failure(control, camera.serial + ": " + error.what());
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
                   uint64_t measurement_start_ns,
                   uint64_t measurement_end_ns,
                   bool success,
                   const std::string &error)
{
    std::ofstream out(path);
    if (!out)
        throw std::runtime_error("Cannot open summary output: " + path);
    out << std::fixed << std::setprecision(6);

    uint64_t deliveries = 0;
    uint64_t frames = 0;
    uint64_t drops = 0;
    uint64_t timeouts = 0;
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
        << "  \"schema_version\": 2,\n"
        << "  \"success\": " << (success ? "true" : "false") << ",\n"
        << "  \"error\": " << quoted(error) << ",\n"
        << "  \"scheduler\": {\"policy\":" << quoted(scheduler_policy())
        << ",\"priority\":" << parameter.sched_priority << "},\n"
        << "  \"run\": {\"camera_count\":" << cameras.size()
        << ",\"stream_mode\":" << quoted(opts.stream_mode)
        << ",\"delivery\":" << quoted(opts.delivery)
        << ",\"frames_per_camera\":" << opts.frames
        << ",\"warmup_frames\":" << opts.warmup_frames
        << ",\"fps\":" << opts.fps
        << ",\"frame_timeout_ms\":" << opts.frame_timeout_ms
        << ",\"startup_timeout_ms\":" << opts.startup_timeout_ms << "},\n"
        << "  \"measurement\": {\"start_boottime_ns\":" << measurement_start_ns
        << ",\"end_boottime_ns\":" << measurement_end_ns
        << ",\"duration_ms\":"
        << (measurement_end_ns >= measurement_start_ns
                ? ns_to_ms(measurement_end_ns - measurement_start_ns)
                : 0.0)
        << "},\n"
        << "  \"aggregate\": {\"deliveries\":" << deliveries << ",\"frames\":" << frames
        << ",\"drops\":" << drops << ",\"timeouts\":" << timeouts
        << ",\"raw_events\":" << raw_events << ",\"delivery_interarrival_ms\":";
    write_stats(out, summarize(delivery_gaps));
    out << ",\"wait_ms\":";
    write_stats(out, summarize(wait_times));
    out << "},\n  \"cameras\": [";

    for (size_t i = 0; i < cameras.size(); ++i)
    {
        const auto &camera = cameras[i];
        std::lock_guard<std::mutex> lock(camera->metrics.mutex);
        const auto &metrics = camera->metrics;
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
            out << quoted(entry.first) << ":{\"frames\":" << stream.frames
                << ",\"drops\":" << stream.drops
                << ",\"timestamp_domain\":" << quoted(stream.timestamp_domain)
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

    {
        std::unique_lock<std::mutex> lock(control.mutex);
        const bool ready = control.cv.wait_for(
            lock,
            std::chrono::milliseconds(opts.startup_timeout_ms),
            [&]() { return control.failed.load() || control.warmed_cameras == cameras.size(); });
        if (!ready && !control.failed.load())
        {
            lock.unlock();
            set_failure(control, "Timed out while waiting for all cameras to warm up");
        }
    }

    uint64_t measurement_start_ns = 0;
    uint64_t measurement_end_ns = 0;
    if (!control.failed.load())
    {
        measurement_start_ns = rs_trace_boottime_ns();
        control.origin_ns.store(measurement_start_ns);
        rs_trace_phase_marker("steady_state_begin");
        control.measurement_enabled.store(true);

        const int timeout_ms = opts.measurement_timeout_ms > 0
                                   ? opts.measurement_timeout_ms
                                   : automatic_measurement_timeout_ms(opts);
        std::unique_lock<std::mutex> lock(control.mutex);
        const bool complete = control.cv.wait_for(
            lock,
            std::chrono::milliseconds(timeout_ms),
            [&]() { return control.failed.load() || control.completed_cameras == cameras.size(); });
        if (!complete && !control.failed.load())
        {
            lock.unlock();
            set_failure(control, "Timed out during steady-state measurement");
        }
        measurement_end_ns = rs_trace_boottime_ns();
        control.measurement_enabled.store(false);
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
                      measurement_start_ns,
                      measurement_end_ns,
                      !control.failed.load(),
                      control.error);

    std::cout << "RS_STEADY_RESULT {\"success\":" << (!control.failed.load() ? "true" : "false")
              << ",\"camera_count\":" << cameras.size()
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
