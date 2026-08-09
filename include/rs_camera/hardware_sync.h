#pragma once

#include <librealsense2/rs.hpp>

#include <string>
#include <vector>

namespace rs_camera
{
struct hardware_sync_assignment
{
    std::string serial;
    std::string role;
    float requested_mode = 0.0f;
    float previous_mode = 0.0f;
    float effective_mode = 0.0f;
    float restored_mode = 0.0f;
    bool applied = false;
    bool restored = false;
    std::string restore_error;
    rs2::sensor sensor;
};

class hardware_sync_session
{
public:
    hardware_sync_session(rs2::context &context,
                          const std::string &master_serial,
                          const std::vector<std::string> &slave_serials);
    ~hardware_sync_session();

    hardware_sync_session(const hardware_sync_session &) = delete;
    hardware_sync_session &operator=(const hardware_sync_session &) = delete;

    bool enabled() const { return !_assignments.empty(); }
    const std::string &master_serial() const { return _master_serial; }
    const std::vector<std::string> &slave_serials() const { return _slave_serials; }
    const std::vector<hardware_sync_assignment> &assignments() const
    {
        return _assignments;
    }

    bool all_applied() const;
    bool all_restored() const;
    std::string restore() noexcept;

private:
    std::string _master_serial;
    std::vector<std::string> _slave_serials;
    std::vector<hardware_sync_assignment> _assignments;
    bool _restore_attempted = false;
};
} // namespace rs_camera
