#pragma once

#include <librealsense2/rs.hpp>

#include <cstddef>
#include <cstdint>
#include <map>
#include <mutex>
#include <string>
#include <vector>

namespace rs_camera::steady
{
struct frame_event
{
    uint64_t host_boottime_ns = 0;
    uint64_t delivery = 0;
    rs2_stream stream = RS2_STREAM_ANY;
    int stream_index = 0;
    uint64_t frame_number = 0;
    double sensor_timestamp_ms = 0.0;
    rs2_timestamp_domain timestamp_domain = RS2_TIMESTAMP_DOMAIN_HARDWARE_CLOCK;
};

struct stream_metrics
{
    uint64_t frames = 0;
    uint64_t drops = 0;
    bool has_last = false;
    uint64_t last_frame_number = 0;
    double last_sensor_timestamp_ms = 0.0;
    uint64_t last_host_ns = 0;
    rs2_timestamp_domain timestamp_domain = RS2_TIMESTAMP_DOMAIN_HARDWARE_CLOCK;
    std::vector<double> sensor_interarrival_ms;
    std::vector<double> host_interarrival_ms;
};

struct warmup_stream_metrics
{
    uint64_t observed_frames = 0;
    uint64_t duplicate_frames = 0;
    uint64_t sequence_gaps = 0;
    uint64_t out_of_order_frames = 0;
    bool has_last = false;
    uint64_t last_frame_number = 0;
};

struct storage_plan
{
    size_t delivery_capacity = 0;
    size_t event_capacity = 0;
    size_t stream_sample_capacity = 0;
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
    uint64_t warmup_health_deliveries = 0;
    bool scheduler_ready = false;
    bool warmed = false;
    bool completed = false;
    storage_plan storage;
    std::vector<double> delivery_interarrival_ms;
    std::vector<double> wait_ms;
    std::map<std::string, stream_metrics> streams;
    std::map<std::string, warmup_stream_metrics> warmup_streams;
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

size_t expected_stream_count(const std::string &stream_mode);
storage_plan make_storage_plan(int frames,
                               int measurement_duration_ms,
                               int fps,
                               size_t stream_count,
                               bool callback_delivery);
void prepare_camera_metrics(camera_metrics &metrics,
                            const std::string &stream_mode,
                            const storage_plan &plan);

std::string record_warmup_frame(camera_metrics &metrics, const rs2::frame &frame);
std::string warmup_health_error(const camera_metrics &metrics,
                                const std::string &serial,
                                size_t expected_streams,
                                bool require_complete_frameset);
std::string record_measured_frame(camera_metrics &metrics,
                                  const rs2::frame &frame,
                                  uint64_t host_ns,
                                  uint64_t delivery);

uint64_t total_drops(const camera_metrics &metrics);
camera_freshness_metrics analyze_freshness(const camera_metrics &metrics);
void add_freshness(camera_freshness_metrics &destination,
                   const camera_freshness_metrics &source);
} // namespace rs_camera::steady
