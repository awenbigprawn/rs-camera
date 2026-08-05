#include "rs_camera/benchmark_utils.h"

#include <algorithm>
#include <cmath>
#include <iomanip>
#include <sstream>

namespace rs_camera
{
std::string json_escape(const std::string &value)
{
    std::ostringstream out;
    for (unsigned char c : value)
    {
        switch (c)
        {
        case '\\': out << "\\\\"; break;
        case '"': out << "\\\""; break;
        case '\n': out << "\\n"; break;
        case '\r': out << "\\r"; break;
        case '\t': out << "\\t"; break;
        default:
            if (c < 0x20)
                out << "\\u" << std::hex << std::setw(4) << std::setfill('0')
                    << static_cast<int>(c);
            else
                out << static_cast<char>(c);
        }
    }
    return out.str();
}

std::string quoted(const std::string &value)
{
    return "\"" + json_escape(value) + "\"";
}

std::string csv_field(const std::string &value)
{
    if (value.find_first_of(",\"\n\r") == std::string::npos)
        return value;
    std::string escaped = "\"";
    for (char c : value)
    {
        if (c == '"')
            escaped += '"';
        escaped += c;
    }
    return escaped + '"';
}

distribution_stats summarize(std::vector<double> values)
{
    distribution_stats result;
    result.n = values.size();
    if (values.empty())
        return result;

    std::sort(values.begin(), values.end());
    result.min = values.front();
    result.max = values.back();
    double sum = 0.0;
    for (double value : values)
        sum += value;
    result.mean = sum / static_cast<double>(values.size());
    double squared = 0.0;
    for (double value : values)
        squared += (value - result.mean) * (value - result.mean);
    result.stddev = std::sqrt(squared / static_cast<double>(values.size()));

    auto percentile = [&](double p) {
        if (values.size() == 1)
            return values.front();
        const double position = p * static_cast<double>(values.size() - 1);
        const auto low = static_cast<size_t>(std::floor(position));
        const auto high = static_cast<size_t>(std::ceil(position));
        return values[low] + (values[high] - values[low]) *
                                 (position - static_cast<double>(low));
    };
    result.p50 = percentile(0.5);
    result.p90 = percentile(0.9);
    result.p99 = percentile(0.99);
    result.p999 = percentile(0.999);
    return result;
}

void write_stats_json(std::ostream &out, const distribution_stats &value)
{
    out << "{\"n\":" << value.n
        << ",\"min\":" << value.min
        << ",\"max\":" << value.max
        << ",\"mean\":" << value.mean
        << ",\"stddev\":" << value.stddev
        << ",\"p50\":" << value.p50
        << ",\"p90\":" << value.p90
        << ",\"p99\":" << value.p99
        << ",\"p999\":" << value.p999 << "}";
}

double ns_to_ms(uint64_t ns)
{
    return static_cast<double>(ns) / 1000000.0;
}
} // namespace rs_camera
