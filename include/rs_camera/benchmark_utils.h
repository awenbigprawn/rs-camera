#pragma once

#include <cstddef>
#include <cstdint>
#include <ostream>
#include <string>
#include <vector>

namespace rs_camera
{
struct distribution_stats
{
    size_t n = 0;
    double min = 0.0;
    double max = 0.0;
    double mean = 0.0;
    double stddev = 0.0;
    double p50 = 0.0;
    double p90 = 0.0;
    double p99 = 0.0;
    double p999 = 0.0;
};

std::string json_escape(const std::string &value);
std::string quoted(const std::string &value);
std::string csv_field(const std::string &value);
distribution_stats summarize(std::vector<double> values);
void write_stats_json(std::ostream &out, const distribution_stats &value);
double ns_to_ms(uint64_t ns);
} // namespace rs_camera
