#include "rs_camera/steady_metrics.h"

#include "rs_camera/benchmark_utils.h"
#include "rs_camera/realsense_utils.h"

#include <algorithm>
#include <limits>
#include <sstream>
#include <stdexcept>
#include <utility>

namespace rs_camera::steady
{
namespace
{
struct stream_descriptor
{
    rs2_stream stream;
    int index;
};

std::vector<stream_descriptor> expected_streams(const std::string &stream_mode)
{
    std::vector<stream_descriptor> result{{RS2_STREAM_DEPTH, 0}};
    if (stream_mode == "depth")
        return result;
    const bool stereo_all =
        stream_mode == "stereo_all" || stream_mode == "d435_all";
    const bool depth_ir = stream_mode == "depth_ir";
    if (stream_mode == "depth_color" || stereo_all)
        result.push_back({RS2_STREAM_COLOR, 0});
    if (stereo_all || depth_ir)
    {
        result.push_back({RS2_STREAM_INFRARED, 1});
        result.push_back({RS2_STREAM_INFRARED, 2});
    }
    return result;
}

size_t checked_product(size_t left, size_t right, const char *description)
{
    if (right && left > std::numeric_limits<size_t>::max() / right)
        throw std::runtime_error(std::string(description) + " capacity overflows size_t");
    return left * right;
}

size_t capacity_with_headroom(uint64_t expected,
                              uint64_t nominal_rate,
                              const char *description)
{
    const uint64_t headroom = std::max<uint64_t>(
        nominal_rate * 2ULL,
        (expected + 19ULL) / 20ULL); // At least two seconds and 5 percent.
    if (expected > std::numeric_limits<uint64_t>::max() - headroom - 1ULL ||
        expected + headroom + 1ULL > std::numeric_limits<size_t>::max())
        throw std::runtime_error(std::string(description) + " capacity overflows size_t");
    return static_cast<size_t>(expected + headroom + 1ULL);
}

template <class T>
bool append_without_reallocation(std::vector<T> &destination, T value)
{
    if (destination.size() >= destination.capacity())
        return false;
    destination.push_back(std::move(value));
    return true;
}

template <class T>
void allocate_and_prefault(std::vector<T> &destination, size_t capacity)
{
    // Value-initialize the whole allocation before streaming starts so neither
    // vector growth nor first-touch page faults occur in the measured phase.
    destination.resize(capacity);
    destination.clear();
}
} // namespace

size_t expected_stream_count(const std::string &stream_mode)
{
    if (stream_mode == "depth")
        return 1;
    if (stream_mode == "depth_color")
        return 2;
    if (stream_mode == "depth_ir")
        return 3;
    if (stream_mode == "stereo_all" || stream_mode == "d435_all")
        return 4;
    return 0;
}

storage_plan make_storage_plan(int frames,
                               int measurement_duration_ms,
                               int fps,
                               size_t stream_count,
                               bool callback_delivery)
{
    if (frames <= 0 || measurement_duration_ms < 0 || fps <= 0 || !stream_count)
        throw std::runtime_error("Invalid steady measurement storage parameters");

    storage_plan plan;
    plan.delivery_capacity = static_cast<size_t>(frames);
    plan.stream_sample_capacity = static_cast<size_t>(frames);
    plan.event_capacity = checked_product(
        static_cast<size_t>(frames), stream_count, "Frame event");
    if (measurement_duration_ms > 0)
    {
        const uint64_t numerator =
            static_cast<uint64_t>(measurement_duration_ms) * static_cast<uint64_t>(fps);
        const uint64_t expected_per_stream = (numerator + 999ULL) / 1000ULL;
        plan.stream_sample_capacity = capacity_with_headroom(
            expected_per_stream,
            static_cast<uint64_t>(fps),
            "Per-stream sample");
        plan.event_capacity = checked_product(
            plan.stream_sample_capacity, stream_count, "Frame event");

        const uint64_t delivery_multiplier = callback_delivery ? stream_count : 1ULL;
        plan.delivery_capacity = capacity_with_headroom(
            expected_per_stream * delivery_multiplier,
            static_cast<uint64_t>(fps) * delivery_multiplier,
            "Delivery sample");
    }
    return plan;
}

void prepare_camera_metrics(camera_metrics &metrics,
                            const std::string &stream_mode,
                            const storage_plan &plan)
{
    metrics.storage = plan;
    allocate_and_prefault(metrics.delivery_interarrival_ms, plan.delivery_capacity);
    allocate_and_prefault(metrics.wait_ms, plan.delivery_capacity);
    allocate_and_prefault(metrics.events, plan.event_capacity);

    for (const auto &descriptor : expected_streams(stream_mode))
    {
        const std::string key = stream_key(descriptor.stream, descriptor.index);
        auto [stream_it, inserted] = metrics.streams.emplace(key, stream_metrics{});
        (void)inserted;
        allocate_and_prefault(
            stream_it->second.sensor_interarrival_ms,
            plan.stream_sample_capacity);
        allocate_and_prefault(
            stream_it->second.host_interarrival_ms,
            plan.stream_sample_capacity);
        metrics.warmup_streams.emplace(key, warmup_stream_metrics{});
    }
}

std::string record_warmup_frame(camera_metrics &metrics, const rs2::frame &frame)
{
    if (!frame)
        return {};
    const std::string key = stream_key(frame);
    const auto found = metrics.warmup_streams.find(key);
    if (found == metrics.warmup_streams.end())
        return "Unexpected warm-up stream " + key;

    auto &stream = found->second;
    const uint64_t number = frame.get_frame_number();
    if (stream.has_last)
    {
        if (number == stream.last_frame_number)
            ++stream.duplicate_frames;
        else if (number < stream.last_frame_number)
            ++stream.out_of_order_frames;
        else if (number > stream.last_frame_number + 1)
            stream.sequence_gaps += number - stream.last_frame_number - 1;
    }
    stream.has_last = true;
    stream.last_frame_number = number;
    ++stream.observed_frames;
    return {};
}

std::string warmup_health_error(const camera_metrics &metrics,
                                const std::string &serial,
                                size_t expected_streams,
                                bool require_complete_frameset,
                                bool allow_unsynchronized_color_reuse)
{
    if (metrics.warmup_streams.size() != expected_streams)
        return serial + ": warm-up freshness expected " +
               std::to_string(expected_streams) + " streams but observed " +
               std::to_string(metrics.warmup_streams.size());

    for (const auto &[key, stream] : metrics.warmup_streams)
    {
        // D400 external synchronization controls the depth sensor, not the
        // independent RGB sensor.  The frameset synchronizer may therefore
        // reuse a recent color frame while pairing it with synchronized depth.
        // Keep that narrowly bounded and continue treating gaps or reordering
        // on every stream as a hard warm-up failure.
        const bool unsynchronized_color =
            allow_unsynchronized_color_reuse && key.rfind("Color#", 0) == 0;
        const uint64_t allowed_color_duplicates = unsynchronized_color
            ? std::max<uint64_t>(1, metrics.warmup_health_deliveries / 20)
            : 0;
        const bool missing_from_framesets =
            require_complete_frameset &&
            stream.observed_frames != metrics.warmup_health_deliveries;
        if (stream.observed_frames < 2 || missing_from_framesets ||
            stream.duplicate_frames > allowed_color_duplicates ||
            stream.sequence_gaps ||
            stream.out_of_order_frames)
        {
            std::ostringstream message;
            message << serial << ": warm-up freshness failed for " << key
                    << " (window_deliveries=" << metrics.warmup_health_deliveries
                    << ", observed=" << stream.observed_frames
                    << ", duplicates=" << stream.duplicate_frames
                    << ", gaps=" << stream.sequence_gaps
                    << ", out_of_order=" << stream.out_of_order_frames << ")";
            return message.str();
        }
    }
    return {};
}

std::string record_measured_frame(camera_metrics &metrics,
                                  const rs2::frame &frame,
                                  uint64_t host_boottime_ns,
                                  uint64_t host_realtime_ns,
                                  uint64_t delivery)
{
    if (!frame)
        return {};

    const auto profile = frame.get_profile();
    const rs2_stream stream_type = profile.stream_type();
    const int stream_index = profile.stream_index();
    const std::string key = stream_key(stream_type, stream_index);
    const auto found = metrics.streams.find(key);
    if (found == metrics.streams.end())
        return "Unexpected measured stream " + key;
    if (metrics.events.size() >= metrics.events.capacity())
        return "Preallocated frame event capacity exhausted";

    auto &stream = found->second;
    const uint64_t number = frame.get_frame_number();
    const double sensor_ms = frame.get_timestamp();
    const bool has_frame_timestamp =
        frame.supports_frame_metadata(RS2_FRAME_METADATA_FRAME_TIMESTAMP);
    const bool has_backend_timestamp =
        frame.supports_frame_metadata(RS2_FRAME_METADATA_BACKEND_TIMESTAMP);
    const bool has_time_of_arrival =
        frame.supports_frame_metadata(RS2_FRAME_METADATA_TIME_OF_ARRIVAL);
    const double frame_timestamp_ms = has_frame_timestamp
        ? static_cast<double>(frame.get_frame_metadata(RS2_FRAME_METADATA_FRAME_TIMESTAMP))
        : 0.0;
    const double backend_timestamp_ms = has_backend_timestamp
        ? static_cast<double>(frame.get_frame_metadata(RS2_FRAME_METADATA_BACKEND_TIMESTAMP))
        : 0.0;
    const double time_of_arrival_ms = has_time_of_arrival
        ? static_cast<double>(frame.get_frame_metadata(RS2_FRAME_METADATA_TIME_OF_ARRIVAL))
        : 0.0;
    if (stream.has_last)
    {
        if (stream.sensor_interarrival_ms.size() >=
                stream.sensor_interarrival_ms.capacity() ||
            stream.host_interarrival_ms.size() >=
                stream.host_interarrival_ms.capacity())
            return "Preallocated stream sample capacity exhausted for " + key;
    }

    if (stream.has_last)
    {
        if (number > stream.last_frame_number + 1)
            stream.drops += number - stream.last_frame_number - 1;
        append_without_reallocation(
            stream.sensor_interarrival_ms,
            sensor_ms - stream.last_sensor_timestamp_ms);
        append_without_reallocation(
            stream.host_interarrival_ms,
            ns_to_ms(host_boottime_ns - stream.last_host_ns));
    }
    stream.has_last = true;
    stream.last_frame_number = number;
    stream.last_sensor_timestamp_ms = sensor_ms;
    stream.last_host_ns = host_boottime_ns;
    stream.timestamp_domain = frame.get_frame_timestamp_domain();
    ++stream.frames;
    ++metrics.frames;
    append_without_reallocation(
        metrics.events,
        frame_event{host_boottime_ns,
                    host_realtime_ns,
                    delivery,
                    stream_type,
                    stream_index,
                    number,
                    sensor_ms,
                    frame_timestamp_ms,
                    backend_timestamp_ms,
                    time_of_arrival_ms,
                    has_frame_timestamp,
                    has_backend_timestamp,
                    has_time_of_arrival,
                    stream.timestamp_domain});
    return {};
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

        const std::string key = stream_key(event.stream, event.stream_index);
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
        const uint64_t unique =
            static_cast<uint64_t>(std::distance(numbers.begin(), unique_end));

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
} // namespace rs_camera::steady
