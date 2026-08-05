#pragma once

#include <librealsense2/rs.hpp>

#include <string>

namespace rs_camera
{
struct pipeline_wait_result
{
    rs2::frame frame;
    std::string error;

    explicit operator bool() const { return static_cast<bool>(frame); }
};

std::string device_info(const rs2::device &device, rs2_camera_info key);
std::string stream_key(rs2_stream stream, int stream_index);
std::string stream_key(const rs2::frame &frame);

// Use the librealsense C error channel so the periodic wait loop does not
// throw and unwind a C++ exception on a timeout or device error.
pipeline_wait_result wait_for_pipeline_frame(rs2_pipeline *pipeline,
                                             unsigned int timeout_ms);
} // namespace rs_camera
