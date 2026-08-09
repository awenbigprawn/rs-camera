#include "rs_camera/hardware_sync.h"

#include "rs_camera/realsense_utils.h"

#include <cmath>
#include <sstream>
#include <stdexcept>

namespace rs_camera
{
namespace
{
constexpr float master_mode = 1.0f;
constexpr float slave_mode = 2.0f;
constexpr float mode_tolerance = 0.01f;

rs2::device find_device(rs2::context &context, const std::string &serial)
{
    for (auto &&device : context.query_devices())
    {
        if (device.supports(RS2_CAMERA_INFO_SERIAL_NUMBER) &&
            serial == device.get_info(RS2_CAMERA_INFO_SERIAL_NUMBER))
            return device;
    }
    throw std::runtime_error("Hardware-sync camera is unavailable: " + serial);
}

rs2::sensor find_sync_sensor(const rs2::device &device,
                             const std::string &serial)
{
    rs2::sensor selected;
    for (auto &&sensor : device.query_sensors())
    {
        if (!sensor.supports(RS2_OPTION_INTER_CAM_SYNC_MODE))
            continue;
        if (selected)
            throw std::runtime_error(
                serial + ": multiple sensors expose Inter Cam Sync Mode");
        selected = sensor;
    }
    if (!selected)
        throw std::runtime_error(
            serial + ": depth sensor does not support Inter Cam Sync Mode");
    if (selected.is_option_read_only(RS2_OPTION_INTER_CAM_SYNC_MODE))
        throw std::runtime_error(
            serial + ": Inter Cam Sync Mode is read-only");
    return selected;
}

hardware_sync_assignment make_assignment(rs2::context &context,
                                         const std::string &serial,
                                         const std::string &role,
                                         float requested_mode)
{
    hardware_sync_assignment assignment;
    assignment.serial = serial;
    assignment.role = role;
    assignment.requested_mode = requested_mode;
    assignment.sensor = find_sync_sensor(find_device(context, serial), serial);
    assignment.previous_mode =
        assignment.sensor.get_option(RS2_OPTION_INTER_CAM_SYNC_MODE);
    return assignment;
}

bool mode_matches(float actual, float expected)
{
    return std::fabs(actual - expected) <= mode_tolerance;
}
} // namespace

hardware_sync_session::hardware_sync_session(
    rs2::context &context,
    const std::string &master_serial,
    const std::vector<std::string> &slave_serials)
    : _master_serial(master_serial), _slave_serials(slave_serials)
{
    if (_master_serial.empty() && _slave_serials.empty())
        return;
    if (_master_serial.empty() || _slave_serials.empty())
        throw std::runtime_error(
            "Hardware sync requires one master and at least one slave");

    // Configure receivers before enabling the pulse source.  The pipelines
    // are also started in this order by the steady probe.
    for (const auto &serial : _slave_serials)
        _assignments.push_back(
            make_assignment(context, serial, "slave", slave_mode));
    _assignments.push_back(
        make_assignment(context, _master_serial, "master", master_mode));

    try
    {
        for (auto &assignment : _assignments)
        {
            assignment.sensor.set_option(
                RS2_OPTION_INTER_CAM_SYNC_MODE, assignment.requested_mode);
            assignment.effective_mode = assignment.sensor.get_option(
                RS2_OPTION_INTER_CAM_SYNC_MODE);
            assignment.applied = mode_matches(
                assignment.effective_mode, assignment.requested_mode);
            if (!assignment.applied)
            {
                throw std::runtime_error(
                    assignment.serial + ": requested Inter Cam Sync Mode " +
                    std::to_string(assignment.requested_mode) +
                    " but read back " +
                    std::to_string(assignment.effective_mode));
            }
        }
    }
    catch (...)
    {
        const std::string ignored_restore_error = restore();
        (void)ignored_restore_error;
        throw;
    }
}

hardware_sync_session::~hardware_sync_session()
{
    const std::string ignored_restore_error = restore();
    (void)ignored_restore_error;
}

bool hardware_sync_session::all_applied() const
{
    if (!enabled())
        return false;
    for (const auto &assignment : _assignments)
    {
        if (!assignment.applied)
            return false;
    }
    return true;
}

bool hardware_sync_session::all_restored() const
{
    if (!enabled() || !_restore_attempted)
        return false;
    for (const auto &assignment : _assignments)
    {
        if (!assignment.restored)
            return false;
    }
    return true;
}

std::string hardware_sync_session::restore() noexcept
{
    if (_restore_attempted || !enabled())
        return {};
    _restore_attempted = true;

    std::ostringstream errors;
    bool first_error = true;
    // Disable the pulse source before changing receivers back to their prior
    // modes.  This is the reverse of the configuration order.
    for (auto it = _assignments.rbegin(); it != _assignments.rend(); ++it)
    {
        auto &assignment = *it;
        try
        {
            assignment.sensor.set_option(
                RS2_OPTION_INTER_CAM_SYNC_MODE, assignment.previous_mode);
            assignment.restored_mode = assignment.sensor.get_option(
                RS2_OPTION_INTER_CAM_SYNC_MODE);
            assignment.restored = mode_matches(
                assignment.restored_mode, assignment.previous_mode);
            if (!assignment.restored)
            {
                assignment.restore_error =
                    "read-back mode " + std::to_string(assignment.restored_mode) +
                    " differs from previous mode " +
                    std::to_string(assignment.previous_mode);
            }
        }
        catch (const std::exception &error)
        {
            assignment.restore_error = error.what();
        }
        catch (...)
        {
            assignment.restore_error = "unknown restore error";
        }
        if (!assignment.restore_error.empty())
        {
            if (!first_error)
                errors << "; ";
            first_error = false;
            errors << assignment.serial << ": " << assignment.restore_error;
        }
    }
    return errors.str();
}
} // namespace rs_camera
