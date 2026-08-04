#include "rs_camera/deadline_scheduler.h"
#include "rs_camera/thread_trace_api.h"

#include <algorithm>
#include <cerrno>
#include <climits>
#include <csignal>
#include <cstring>
#include <dlfcn.h>
#include <fstream>
#include <map>
#include <sched.h>
#include <set>
#include <sstream>
#include <stdexcept>
#include <sys/syscall.h>
#include <unistd.h>

#ifndef SCHED_DEADLINE
#define SCHED_DEADLINE 6
#endif

#ifndef SCHED_FLAG_DL_OVERRUN
#define SCHED_FLAG_DL_OVERRUN 0x04
#endif

namespace rs_camera
{
namespace
{
volatile sig_atomic_t deadline_overrun_signals = 0;

void deadline_overrun_handler(int)
{
    if (deadline_overrun_signals < SIG_ATOMIC_MAX)
        ++deadline_overrun_signals;
}

void enable_deadline_overrun_signals()
{
    struct sigaction action{};
    action.sa_handler = deadline_overrun_handler;
    sigemptyset(&action.sa_mask);
    action.sa_flags = SA_RESTART;
    if (sigaction(SIGXCPU, &action, nullptr) != 0)
        throw std::runtime_error(
            "Cannot install SCHED_DEADLINE overrun handler: " +
            std::string(std::strerror(errno)));
    deadline_overrun_signals = 0;
}

struct sched_attr_compat
{
    uint32_t size;
    uint32_t sched_policy;
    uint64_t sched_flags;
    int32_t sched_nice;
    uint32_t sched_priority;
    uint64_t sched_runtime;
    uint64_t sched_deadline;
    uint64_t sched_period;
    uint32_t sched_util_min;
    uint32_t sched_util_max;
};

struct profile_entry
{
    std::string signature;
    unsigned int instance = 0;
    std::string name;
    uint64_t runtime_ns = 0;
    uint64_t deadline_ns = 0;
    uint64_t period_ns = 0;
};

std::vector<std::string> split_csv(const std::string &line)
{
    std::vector<std::string> fields;
    std::string current;
    bool quoted = false;
    for (size_t index = 0; index < line.size(); ++index)
    {
        const char value = line[index];
        if (value == '"')
        {
            if (quoted && index + 1 < line.size() && line[index + 1] == '"')
            {
                current += '"';
                ++index;
            }
            else
                quoted = !quoted;
        }
        else if (value == ',' && !quoted)
        {
            fields.push_back(current);
            current.clear();
        }
        else
            current += value;
    }
    if (quoted)
        throw std::runtime_error("Unterminated quoted CSV field");
    fields.push_back(current);
    return fields;
}

uint64_t parse_u64(const std::string &value, const std::string &field, size_t line)
{
    try
    {
        size_t consumed = 0;
        const uint64_t result = std::stoull(value, &consumed);
        if (consumed != value.size())
            throw std::invalid_argument("trailing characters");
        return result;
    }
    catch (const std::exception &)
    {
        throw std::runtime_error("Invalid " + field + " at profile line " +
                                 std::to_string(line));
    }
}

std::vector<profile_entry> load_profile(const std::string &path)
{
    std::ifstream input(path);
    if (!input)
        throw std::runtime_error("Cannot open scheduler model profile: " + path);

    std::string line;
    if (!std::getline(input, line))
        throw std::runtime_error("Empty scheduler model profile: " + path);
    if (!line.empty() && line.back() == '\r')
        line.pop_back();
    const auto header = split_csv(line);
    const std::vector<std::string> expected = {
        "signature", "instance", "name", "runtime_ns", "deadline_ns", "period_ns"};
    if (header != expected)
        throw std::runtime_error(
            "Scheduler model profile header must be: signature,instance,name,"
            "runtime_ns,deadline_ns,period_ns");

    std::vector<profile_entry> entries;
    size_t line_number = 1;
    while (std::getline(input, line))
    {
        ++line_number;
        if (!line.empty() && line.back() == '\r')
            line.pop_back();
        if (line.empty())
            continue;
        const auto fields = split_csv(line);
        if (fields.size() != expected.size())
            throw std::runtime_error("Wrong CSV field count at profile line " +
                                     std::to_string(line_number));
        profile_entry entry;
        entry.signature = fields[0];
        entry.instance =
            static_cast<unsigned int>(parse_u64(fields[1], "instance", line_number));
        entry.name = fields[2];
        entry.runtime_ns = parse_u64(fields[3], "runtime_ns", line_number);
        entry.deadline_ns = parse_u64(fields[4], "deadline_ns", line_number);
        entry.period_ns = parse_u64(fields[5], "period_ns", line_number);
        if (entry.signature.empty() || entry.instance == 0 || entry.runtime_ns == 0 ||
            entry.runtime_ns > entry.deadline_ns || entry.deadline_ns > entry.period_ns)
            throw std::runtime_error("Invalid scheduler model parameters at profile line " +
                                     std::to_string(line_number));
        const auto key = std::make_pair(entry.signature, entry.instance);
        if (std::any_of(entries.begin(), entries.end(), [&](const auto &existing) {
                return std::make_pair(existing.signature, existing.instance) == key;
            }))
            throw std::runtime_error(
                "Duplicate scheduler model profile identity at line " +
                std::to_string(line_number));
        entries.push_back(std::move(entry));
    }
    if (entries.empty())
        throw std::runtime_error("Scheduler model profile has no thread entries");
    return entries;
}

std::vector<rs_thread_trace_info> snapshot_threads()
{
    using snapshot_fn = size_t (*)(rs_thread_trace_info *, size_t);
    auto snapshot =
        reinterpret_cast<snapshot_fn>(dlsym(RTLD_DEFAULT, "rs_thread_trace_snapshot"));
    if (!snapshot)
        throw std::runtime_error(
            "Modeled scheduling requires libtrace_pthreads.so in LD_PRELOAD");

    const size_t count = snapshot(nullptr, 0);
    std::vector<rs_thread_trace_info> records(count);
    for (auto &record : records)
        record.size = sizeof(record);
    const size_t second_count = snapshot(records.data(), records.size());
    if (second_count > records.size())
        throw std::runtime_error(
            "Thread set changed while applying scheduler model profile");
    records.resize(second_count);

    std::sort(records.begin(), records.end(), [](const auto &left, const auto &right) {
        return left.creation_sequence < right.creation_sequence;
    });
    return records;
}

int set_attributes(int tid, sched_attr_compat &attributes)
{
    if (syscall(SYS_sched_setattr, tid, &attributes, 0) == 0)
        return 0;
    return errno;
}

int set_deadline(int tid, uint64_t runtime_ns, uint64_t deadline_ns, uint64_t period_ns)
{
    sched_attr_compat attributes{};
    attributes.size = sizeof(attributes);
    attributes.sched_policy = SCHED_DEADLINE;
    attributes.sched_flags = SCHED_FLAG_DL_OVERRUN;
    attributes.sched_runtime = runtime_ns;
    attributes.sched_deadline = deadline_ns;
    attributes.sched_period = period_ns;
    return set_attributes(tid, attributes);
}

void restore_other(const std::vector<deadline_assignment> &assignments)
{
    for (const auto &assignment : assignments)
    {
        if (!assignment.applied)
            continue;
        sched_attr_compat attributes{};
        attributes.size = sizeof(attributes);
        attributes.sched_policy = SCHED_OTHER;
        set_attributes(assignment.tid, attributes);
    }
}

void restore_other(const std::vector<rate_monotonic_assignment> &assignments)
{
    for (const auto &assignment : assignments)
    {
        if (!assignment.applied)
            continue;
        sched_param parameter{};
        sched_setscheduler(assignment.tid, SCHED_OTHER, &parameter);
    }
}

std::string fixed_policy_name(int policy)
{
    if (policy == SCHED_RR)
        return "SCHED_RR";
    if (policy == SCHED_FIFO)
        return "SCHED_FIFO";
    throw std::runtime_error("Rate-monotonic policy must be SCHED_RR or SCHED_FIFO");
}

std::string escape_json(const std::string &value)
{
    std::ostringstream output;
    for (unsigned char character : value)
    {
        switch (character)
        {
        case '\\': output << "\\\\"; break;
        case '"': output << "\\\""; break;
        case '\n': output << "\\n"; break;
        case '\r': output << "\\r"; break;
        case '\t': output << "\\t"; break;
        default: output << static_cast<char>(character); break;
        }
    }
    return output.str();
}
} // namespace

deadline_application apply_deadline_profile(const std::string &path,
                                            bool require_all_live_threads)
{
    enable_deadline_overrun_signals();
    const auto profile = load_profile(path);
    const auto threads = snapshot_threads();
    deadline_application result;
    result.profile_path = path;
    result.profile_entries = profile.size();
    result.live_threads = threads.size();

    std::map<std::string, unsigned int> instance_counts;
    std::map<std::pair<std::string, unsigned int>, const rs_thread_trace_info *> live;
    for (const auto &thread : threads)
    {
        const std::string signature(thread.signature);
        const unsigned int instance = ++instance_counts[signature];
        live.emplace(std::make_pair(signature, instance), &thread);
    }

    if (require_all_live_threads && live.size() != profile.size())
        throw std::runtime_error(
            "SCHED_DEADLINE profile/live-thread count mismatch: profile=" +
            std::to_string(profile.size()) + ", live=" + std::to_string(live.size()));
    if (profile.size() > live.size())
        throw std::runtime_error(
            "SCHED_DEADLINE profile has more entries than live threads: profile=" +
            std::to_string(profile.size()) + ", live=" + std::to_string(live.size()));

    result.partial_profile = live.size() != profile.size();
    result.unassigned_live_threads = live.size() - profile.size();

    for (const auto &entry : profile)
    {
        const auto key = std::make_pair(entry.signature, entry.instance);
        const auto match = live.find(key);
        if (match == live.end())
            throw std::runtime_error(
                "No live thread matches SCHED_DEADLINE profile entry instance " +
                std::to_string(entry.instance) + " with signature " + entry.signature);
        deadline_assignment assignment;
        assignment.tid = match->second->tid;
        assignment.signature = entry.signature;
        assignment.instance = entry.instance;
        assignment.name = entry.name;
        assignment.runtime_ns = entry.runtime_ns;
        assignment.deadline_ns = entry.deadline_ns;
        assignment.period_ns = entry.period_ns;
        result.assignments.push_back(std::move(assignment));
    }

    for (auto &assignment : result.assignments)
    {
        assignment.error_number = set_deadline(
            assignment.tid, assignment.runtime_ns, assignment.deadline_ns,
            assignment.period_ns);
        assignment.applied = assignment.error_number == 0;
        if (!assignment.applied)
        {
            const std::string message =
                "sched_setattr(SCHED_DEADLINE) failed for TID " +
                std::to_string(assignment.tid) + ": " +
                std::strerror(assignment.error_number);
            restore_other(result.assignments);
            throw std::runtime_error(message);
        }
    }
    return result;
}

uint64_t deadline_overrun_signal_count()
{
    return static_cast<uint64_t>(deadline_overrun_signals);
}

std::string deadline_application_json(const deadline_application &result)
{
    std::ostringstream output;
    output << "{\"profile_path\":\"" << escape_json(result.profile_path)
           << "\",\"profile_entries\":" << result.profile_entries
           << ",\"live_threads\":" << result.live_threads
           << ",\"unassigned_live_threads\":" << result.unassigned_live_threads
           << ",\"partial_profile\":"
           << (result.partial_profile ? "true" : "false")
           << ",\"overrun_signals\":" << deadline_overrun_signal_count()
           << ",\"assignments\":[";
    for (size_t index = 0; index < result.assignments.size(); ++index)
    {
        if (index)
            output << ",";
        const auto &entry = result.assignments[index];
        output << "{\"tid\":" << entry.tid
               << ",\"signature\":\"" << escape_json(entry.signature)
               << "\",\"instance\":" << entry.instance
               << ",\"name\":\"" << escape_json(entry.name)
               << "\",\"runtime_ns\":" << entry.runtime_ns
               << ",\"deadline_ns\":" << entry.deadline_ns
               << ",\"period_ns\":" << entry.period_ns
               << ",\"applied\":" << (entry.applied ? "true" : "false")
               << ",\"errno\":" << entry.error_number << "}";
    }
    output << "]}";
    return output.str();
}

rate_monotonic_application apply_rate_monotonic_profile(const std::string &path,
                                                        int policy,
                                                        int highest_priority)
{
    const std::string policy_name = fixed_policy_name(policy);
    const int minimum_priority = sched_get_priority_min(policy);
    const int maximum_priority = sched_get_priority_max(policy);
    if (minimum_priority < 0 || maximum_priority < 0)
        throw std::runtime_error("Cannot query fixed-priority scheduler range");
    if (highest_priority < minimum_priority || highest_priority > maximum_priority)
        throw std::runtime_error(
            "Rate-monotonic highest priority is outside the scheduler range");

    const auto profile = load_profile(path);
    const auto threads = snapshot_threads();
    rate_monotonic_application result;
    result.profile_path = path;
    result.policy = policy_name;
    result.highest_priority = highest_priority;
    result.profile_entries = profile.size();
    result.live_threads = threads.size();

    std::map<std::string, unsigned int> instance_counts;
    std::map<std::pair<std::string, unsigned int>, const rs_thread_trace_info *> live;
    for (const auto &thread : threads)
    {
        const std::string signature(thread.signature);
        const unsigned int instance = ++instance_counts[signature];
        live.emplace(std::make_pair(signature, instance), &thread);
    }
    if (live.size() != profile.size())
        throw std::runtime_error(
            "Rate-monotonic profile/live-thread count mismatch: profile=" +
            std::to_string(profile.size()) + ", live=" + std::to_string(live.size()));

    std::set<uint64_t> unique_periods;
    for (const auto &entry : profile)
        unique_periods.insert(entry.period_ns);
    result.priority_levels = unique_periods.size();
    result.lowest_priority =
        highest_priority - static_cast<int>(result.priority_levels) + 1;
    if (result.lowest_priority < minimum_priority)
        throw std::runtime_error(
            "Rate-monotonic profile needs " +
            std::to_string(result.priority_levels) +
            " distinct priorities, but the requested priority band is too small");

    std::map<uint64_t, int> priorities;
    int priority = highest_priority;
    // Linux fixed-priority classes use larger numeric values for higher
    // priorities. Equal modeled periods intentionally share one priority.
    for (const uint64_t period : unique_periods)
        priorities.emplace(period, priority--);

    for (const auto &entry : profile)
    {
        const auto key = std::make_pair(entry.signature, entry.instance);
        const auto match = live.find(key);
        if (match == live.end())
            throw std::runtime_error(
                "No live thread matches rate-monotonic profile entry instance " +
                std::to_string(entry.instance) + " with signature " + entry.signature);
        rate_monotonic_assignment assignment;
        assignment.tid = match->second->tid;
        assignment.signature = entry.signature;
        assignment.instance = entry.instance;
        assignment.name = entry.name;
        assignment.period_ns = entry.period_ns;
        assignment.priority = priorities.at(entry.period_ns);
        result.assignments.push_back(std::move(assignment));
    }

    for (auto &assignment : result.assignments)
    {
        sched_param parameter{};
        parameter.sched_priority = assignment.priority;
        if (sched_setscheduler(assignment.tid, policy, &parameter) == 0)
            assignment.error_number = 0;
        else
            assignment.error_number = errno;
        assignment.applied = assignment.error_number == 0;
        if (!assignment.applied)
        {
            const std::string message =
                "sched_setscheduler(" + policy_name + ") failed for TID " +
                std::to_string(assignment.tid) + ": " +
                std::strerror(assignment.error_number);
            restore_other(result.assignments);
            throw std::runtime_error(message);
        }
    }
    return result;
}

std::string rate_monotonic_application_json(
    const rate_monotonic_application &result)
{
    std::ostringstream output;
    output << "{\"profile_path\":\"" << escape_json(result.profile_path)
           << "\",\"policy\":\"" << escape_json(result.policy)
           << "\",\"highest_priority\":" << result.highest_priority
           << ",\"lowest_priority\":" << result.lowest_priority
           << ",\"priority_levels\":" << result.priority_levels
           << ",\"profile_entries\":" << result.profile_entries
           << ",\"live_threads\":" << result.live_threads
           << ",\"assignments\":[";
    for (size_t index = 0; index < result.assignments.size(); ++index)
    {
        if (index)
            output << ",";
        const auto &entry = result.assignments[index];
        output << "{\"tid\":" << entry.tid
               << ",\"signature\":\"" << escape_json(entry.signature)
               << "\",\"instance\":" << entry.instance
               << ",\"name\":\"" << escape_json(entry.name)
               << "\",\"period_ns\":" << entry.period_ns
               << ",\"priority\":" << entry.priority
               << ",\"applied\":" << (entry.applied ? "true" : "false")
               << ",\"errno\":" << entry.error_number << "}";
    }
    output << "]}";
    return output.str();
}
} // namespace rs_camera
