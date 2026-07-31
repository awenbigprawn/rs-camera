#include "rs_camera/deadline_scheduler.h"
#include "rs_camera/thread_trace_api.h"

#include <algorithm>
#include <cerrno>
#include <cstring>
#include <dlfcn.h>
#include <fstream>
#include <map>
#include <sched.h>
#include <sstream>
#include <stdexcept>
#include <sys/syscall.h>
#include <unistd.h>

#ifndef SCHED_DEADLINE
#define SCHED_DEADLINE 6
#endif

namespace rs_camera
{
namespace
{
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
        throw std::runtime_error("Cannot open SCHED_DEADLINE profile: " + path);

    std::string line;
    if (!std::getline(input, line))
        throw std::runtime_error("Empty SCHED_DEADLINE profile: " + path);
    if (!line.empty() && line.back() == '\r')
        line.pop_back();
    const auto header = split_csv(line);
    const std::vector<std::string> expected = {
        "signature", "instance", "name", "runtime_ns", "deadline_ns", "period_ns"};
    if (header != expected)
        throw std::runtime_error(
            "SCHED_DEADLINE profile header must be: signature,instance,name,"
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
            throw std::runtime_error("Invalid SCHED_DEADLINE parameters at profile line " +
                                     std::to_string(line_number));
        const auto key = std::make_pair(entry.signature, entry.instance);
        if (std::any_of(entries.begin(), entries.end(), [&](const auto &existing) {
                return std::make_pair(existing.signature, existing.instance) == key;
            }))
            throw std::runtime_error(
                "Duplicate SCHED_DEADLINE profile identity at line " +
                std::to_string(line_number));
        entries.push_back(std::move(entry));
    }
    if (entries.empty())
        throw std::runtime_error("SCHED_DEADLINE profile has no thread entries");
    return entries;
}

std::vector<rs_thread_trace_info> snapshot_threads()
{
    using snapshot_fn = size_t (*)(rs_thread_trace_info *, size_t);
    auto snapshot =
        reinterpret_cast<snapshot_fn>(dlsym(RTLD_DEFAULT, "rs_thread_trace_snapshot"));
    if (!snapshot)
        throw std::runtime_error(
            "SCHED_DEADLINE requires libtrace_pthreads.so in LD_PRELOAD");

    const size_t count = snapshot(nullptr, 0);
    std::vector<rs_thread_trace_info> records(count);
    for (auto &record : records)
        record.size = sizeof(record);
    const size_t second_count = snapshot(records.data(), records.size());
    if (second_count > records.size())
        throw std::runtime_error(
            "Thread set changed while applying SCHED_DEADLINE profile");
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

deadline_application apply_deadline_profile(const std::string &path)
{
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

    if (live.size() != profile.size())
        throw std::runtime_error(
            "SCHED_DEADLINE profile/live-thread count mismatch: profile=" +
            std::to_string(profile.size()) + ", live=" + std::to_string(live.size()));

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

std::string deadline_application_json(const deadline_application &result)
{
    std::ostringstream output;
    output << "{\"profile_path\":\"" << escape_json(result.profile_path)
           << "\",\"profile_entries\":" << result.profile_entries
           << ",\"live_threads\":" << result.live_threads << ",\"assignments\":[";
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
} // namespace rs_camera
