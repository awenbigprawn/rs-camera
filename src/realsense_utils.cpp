#include "rs_camera/realsense_utils.h"

#include <librealsense2/h/rs_frame.h>
#include <librealsense2/h/rs_pipeline.h>
#include <librealsense2/h/rs_types.h>

namespace rs_camera
{
std::string device_info(const rs2::device &device, rs2_camera_info key)
{
    return device.supports(key) ? device.get_info(key) : "unknown";
}

std::string stream_key(rs2_stream stream, int stream_index)
{
    return std::string(rs2_stream_to_string(stream)) + "#" +
           std::to_string(stream_index);
}

std::string stream_key(const rs2::frame &frame)
{
    const auto profile = frame.get_profile();
    return stream_key(profile.stream_type(), profile.stream_index());
}

pipeline_wait_result wait_for_pipeline_frame(rs2_pipeline *pipeline,
                                             unsigned int timeout_ms)
{
    pipeline_wait_result result;
    rs2_error *error = nullptr;
    rs2_frame *frame = rs2_pipeline_wait_for_frames(pipeline, timeout_ms, &error);
    if (error)
    {
        const char *message = rs2_get_error_message(error);
        result.error = message ? message : "Unknown librealsense pipeline error";
        rs2_free_error(error);
        if (frame)
            rs2_release_frame(frame);
        return result;
    }
    if (!frame)
    {
        result.error = "Frame didn't arrive within " + std::to_string(timeout_ms);
        return result;
    }
    result.frame = rs2::frame(frame);
    return result;
}
} // namespace rs_camera
