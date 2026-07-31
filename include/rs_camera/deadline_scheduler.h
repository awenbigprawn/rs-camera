#ifndef RS_CAMERA_DEADLINE_SCHEDULER_H
#define RS_CAMERA_DEADLINE_SCHEDULER_H

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace rs_camera
{
struct deadline_assignment
{
    int tid = 0;
    std::string signature;
    unsigned int instance = 0;
    std::string name;
    uint64_t runtime_ns = 0;
    uint64_t deadline_ns = 0;
    uint64_t period_ns = 0;
    bool applied = false;
    int error_number = 0;
};

struct deadline_application
{
    std::string profile_path;
    size_t profile_entries = 0;
    size_t live_threads = 0;
    std::vector<deadline_assignment> assignments;
};

deadline_application apply_deadline_profile(const std::string &path);
std::string deadline_application_json(const deadline_application &result);
} // namespace rs_camera

#endif
