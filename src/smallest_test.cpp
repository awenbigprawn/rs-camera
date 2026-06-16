#include <librealsense2/rs.hpp> // Include RealSense Cross Platform API
#include <iostream>
#include <thread>
#include <chrono>
#include "sched_deadline.hpp"

using namespace std::chrono;

struct FrameTimestamps {
    time_point<steady_clock> start_time_point;
    time_point<steady_clock> last_idle_wait_finish_time_point;
    time_point<steady_clock> idle_wait_finish_time_point;
    time_point<steady_clock> update_frames_finish_time_point;
    time_point<steady_clock> images_work_finish_time_point;
};


int main() {
    pthread_setname_np(pthread_self(), "getframe");
    bool use_sched_deadline = true;
    if (use_sched_deadline) {
        // Set SCHED_DEADLINE for the perception thread
        uint64_t runtime_ns = 10 * 1000 * 1000; // 5ms
        uint64_t deadline_ns = 30000000; // 30ms
        uint64_t period_ns = 30000000; // 30ms
        set_sched_deadline(gettid(), runtime_ns, deadline_ns, period_ns);

        uint64_t dl_budget_runtime  = runtime_ns + 1000000 * 1000 * 1000ULL; // 35ms
        create_dl_budget(dl_budget_runtime, deadline_ns, period_ns);
    }

    std::vector<FrameTimestamps> timestamps;
    sleep(1);

    rs2::pipeline p;
    p.start();
    static int frame_count = 0;
    while (true) {
        FrameTimestamps frame_timestamps;

        frame_timestamps.start_time_point = steady_clock::now();
        auto f = p.wait_for_frames();
        frame_timestamps.idle_wait_finish_time_point = steady_clock::now();

        rs2::frame color_frame = f.get_color_frame();
        rs2::frame depth_frame = f.get_depth_frame();

        if (color_frame && depth_frame)
        {
            // Get the width and height of the frame
            const int width = color_frame.as<rs2::video_frame>().get_width();
            const int height = color_frame.as<rs2::video_frame>().get_height();
            const int depth_width = depth_frame.as<rs2::video_frame>().get_width();
            const int depth_height = depth_frame.as<rs2::video_frame>().get_height();

            // std::cout << " colorRes: " << width << "x" << height
            //           << " depthRes: " << depth_width << "x" << depth_height << std::endl;
        }
        frame_timestamps.images_work_finish_time_point = steady_clock::now();
        frame_timestamps.last_idle_wait_finish_time_point = frame_count > 0 ? timestamps.back().idle_wait_finish_time_point : frame_timestamps.idle_wait_finish_time_point;
        timestamps.push_back(frame_timestamps);

        if (frame_timestamps.idle_wait_finish_time_point - frame_timestamps.last_idle_wait_finish_time_point > milliseconds(35)) {
            std::cout << "Warning: Frame " << frame_count << " had a long interval of "
                      << duration_cast<milliseconds>(frame_timestamps.idle_wait_finish_time_point - frame_timestamps.last_idle_wait_finish_time_point).count()
                      << " ms between frames." << std::endl;
        }
        frame_count++;
    }

    return 0;
}

