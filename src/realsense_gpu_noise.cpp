#include <algorithm>
#include <chrono>
#include <csignal>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

#include <cpu.h>
#include <datareader.h>
#include <gpu.h>
#include <net.h>

#ifndef RS_CAMERA_MOBILENET_V2_PARAM
#define RS_CAMERA_MOBILENET_V2_PARAM "mobilenet_v2.param"
#endif

namespace
{
volatile std::sig_atomic_t stop_requested = 0;

void handle_signal(int)
{
    stop_requested = 1;
}

class ZeroDataReader : public ncnn::DataReader
{
public:
    int scan(const char*, void*) const override
    {
        return 0;
    }

    size_t read(void* buffer, size_t size) const override
    {
        std::memset(buffer, 0, size);
        return size;
    }
};

struct Options
{
    std::string model_param = RS_CAMERA_MOBILENET_V2_PARAM;
    std::string ready_file;
    std::string summary_output;
    int gpu_device = 0;
    int warmup_iterations = 10;
    int num_threads = 1;
    bool allow_cpu_vulkan = false;
};

std::string json_escape(const std::string& value)
{
    std::ostringstream output;
    for (const unsigned char character : value)
    {
        switch (character)
        {
        case '"': output << "\\\""; break;
        case '\\': output << "\\\\"; break;
        case '\b': output << "\\b"; break;
        case '\f': output << "\\f"; break;
        case '\n': output << "\\n"; break;
        case '\r': output << "\\r"; break;
        case '\t': output << "\\t"; break;
        default:
            if (character < 0x20)
            {
                output << "\\u" << std::hex << std::setw(4) << std::setfill('0')
                       << static_cast<int>(character) << std::dec;
            }
            else
            {
                output << character;
            }
        }
    }
    return output.str();
}

void write_text_file(const std::string& path, const std::string& contents)
{
    if (path.empty())
        return;

    std::ofstream output(path, std::ios::out | std::ios::trunc);
    if (!output)
        throw std::runtime_error("cannot open output file: " + path);
    output << contents << '\n';
    output.close();
    if (!output)
        throw std::runtime_error("cannot write output file: " + path);
}

double percentile(std::vector<double> values, double fraction)
{
    if (values.empty())
        return 0.0;

    std::sort(values.begin(), values.end());
    const double position = fraction * static_cast<double>(values.size() - 1);
    const size_t lower = static_cast<size_t>(position);
    const size_t upper = std::min(lower + 1, values.size() - 1);
    const double weight = position - static_cast<double>(lower);
    return values[lower] * (1.0 - weight) + values[upper] * weight;
}

Options parse_options(int argc, char** argv)
{
    Options options;
    for (int index = 1; index < argc; ++index)
    {
        const std::string argument = argv[index];
        auto require_value = [&]() -> std::string {
            if (++index >= argc)
                throw std::runtime_error("missing value after " + argument);
            return argv[index];
        };

        if (argument == "--model-param")
            options.model_param = require_value();
        else if (argument == "--ready-file")
            options.ready_file = require_value();
        else if (argument == "--summary-output")
            options.summary_output = require_value();
        else if (argument == "--gpu-device")
            options.gpu_device = std::stoi(require_value());
        else if (argument == "--warmup-iterations")
            options.warmup_iterations = std::stoi(require_value());
        else if (argument == "--num-threads")
            options.num_threads = std::stoi(require_value());
        else if (argument == "--allow-cpu-vulkan")
            options.allow_cpu_vulkan = true;
        else if (argument == "--help" || argument == "-h")
        {
            std::cout
                << "Usage: realsense_gpu_noise [OPTIONS]\n"
                << "Continuously execute the pinned MobileNetV2 graph with ncnn Vulkan.\n\n"
                << "Options:\n"
                << "  --model-param PATH       ncnn MobileNetV2 parameter file\n"
                << "  --ready-file PATH        write JSON after Vulkan warm-up\n"
                << "  --summary-output PATH    write final JSON on graceful shutdown\n"
                << "  --gpu-device INDEX       ncnn Vulkan device index (default: 0)\n"
                << "  --warmup-iterations N    iterations before ready (default: 10)\n"
                << "  --num-threads N          CPU worker count (default: 1)\n"
                << "  --allow-cpu-vulkan       permit a software Vulkan CPU device\n";
            std::exit(0);
        }
        else
            throw std::runtime_error("unknown argument: " + argument);
    }

    if (options.ready_file.empty())
        throw std::runtime_error("--ready-file is required");
    if (options.summary_output.empty())
        throw std::runtime_error("--summary-output is required");
    if (options.gpu_device < 0 || options.warmup_iterations < 1 || options.num_threads < 1)
        throw std::runtime_error("GPU index must be non-negative and counts must be positive");
    return options;
}

double run_inference(
    ncnn::Net& network,
    const std::vector<const char*>& input_names,
    const std::vector<const char*>& output_names,
    const ncnn::Mat& input)
{
    const auto begin = std::chrono::steady_clock::now();
    ncnn::Extractor extractor = network.create_extractor();
    for (const char* input_name : input_names)
    {
        if (extractor.input(input_name, input) != 0)
            throw std::runtime_error("failed to set MobileNetV2 input tensor");
    }
    for (const char* output_name : output_names)
    {
        ncnn::Mat output;
        if (extractor.extract(output_name, output) != 0)
            throw std::runtime_error("failed to extract MobileNetV2 output tensor");
    }
    const auto end = std::chrono::steady_clock::now();
    return std::chrono::duration<double, std::milli>(end - begin).count();
}
} // namespace

int main(int argc, char** argv)
{
    try
    {
        const Options options = parse_options(argc, argv);
        std::signal(SIGINT, handle_signal);
        std::signal(SIGTERM, handle_signal);

        ncnn::set_omp_dynamic(0);
        ncnn::set_omp_num_threads(options.num_threads);

        const int gpu_count = ncnn::get_gpu_count();
        if (options.gpu_device >= gpu_count)
        {
            throw std::runtime_error(
                "requested Vulkan device " + std::to_string(options.gpu_device)
                + ", but ncnn found " + std::to_string(gpu_count));
        }
        const ncnn::GpuInfo& gpu_info = ncnn::get_gpu_info(options.gpu_device);
        if (gpu_info.type() == 3 && !options.allow_cpu_vulkan)
        {
            throw std::runtime_error(
                std::string("refusing software Vulkan CPU device: ") + gpu_info.device_name());
        }

        ncnn::VulkanDevice* vulkan_device = ncnn::get_gpu_device(options.gpu_device);
        if (vulkan_device == nullptr)
            throw std::runtime_error("ncnn could not create the requested Vulkan device");

        ncnn::UnlockedPoolAllocator blob_pool_allocator;
        ncnn::PoolAllocator workspace_pool_allocator;
        ncnn::VkBlobAllocator blob_vkallocator(vulkan_device);
        ncnn::VkStagingAllocator staging_vkallocator(vulkan_device);
        blob_pool_allocator.set_size_compare_ratio(0.0f);
        workspace_pool_allocator.set_size_compare_ratio(0.0f);

        ncnn::Net network;
        network.opt.lightmode = true;
        network.opt.num_threads = options.num_threads;
        network.opt.blob_allocator = &blob_pool_allocator;
        network.opt.workspace_allocator = &workspace_pool_allocator;
        network.opt.blob_vkallocator = &blob_vkallocator;
        network.opt.workspace_vkallocator = &blob_vkallocator;
        network.opt.staging_vkallocator = &staging_vkallocator;
        network.opt.use_vulkan_compute = true;
        network.opt.use_fp16_packed = true;
        network.opt.use_fp16_storage = true;
        network.opt.use_fp16_arithmetic = true;
        network.opt.use_packing_layout = true;
        network.set_vulkan_device(vulkan_device);

        const auto process_begin = std::chrono::steady_clock::now();
        if (network.load_param(options.model_param.c_str()) != 0)
            throw std::runtime_error("failed to load ncnn parameter file: " + options.model_param);
        ZeroDataReader weights;
        if (network.load_model(weights) != 0)
            throw std::runtime_error("failed to initialize deterministic MobileNetV2 weights");

        const std::vector<const char*>& input_names = network.input_names();
        const std::vector<const char*>& output_names = network.output_names();
        if (input_names.size() != 1 || output_names.empty())
            throw std::runtime_error("unexpected MobileNetV2 graph inputs or outputs");

        ncnn::Mat input(224, 224, 3);
        input.fill(0.01f);
        std::vector<double> warmup_latencies;
        warmup_latencies.reserve(static_cast<size_t>(options.warmup_iterations));
        for (int iteration = 0; iteration < options.warmup_iterations; ++iteration)
        {
            if (stop_requested)
                throw std::runtime_error("terminated during Vulkan warm-up");
            warmup_latencies.push_back(run_inference(network, input_names, output_names, input));
        }

        const auto ready_time = std::chrono::steady_clock::now();
        const double startup_ms =
            std::chrono::duration<double, std::milli>(ready_time - process_begin).count();
        std::ostringstream ready_json;
        ready_json << std::fixed << std::setprecision(6)
                   << "{\"schema_version\":1"
                   << ",\"mode\":\"mobilenet_v2_vulkan\""
                   << ",\"ready\":true"
                   << ",\"gpu_device\":" << options.gpu_device
                   << ",\"gpu_name\":\"" << json_escape(gpu_info.device_name()) << "\""
                   << ",\"gpu_type\":" << gpu_info.type()
                   << ",\"driver_name\":\"" << json_escape(gpu_info.driver_name()) << "\""
                   << ",\"model_param\":\"" << json_escape(options.model_param) << "\""
                   << ",\"input_shape\":\"224x224x3\""
                   << ",\"weights\":\"deterministic_zero\""
                   << ",\"warmup_iterations\":" << options.warmup_iterations
                   << ",\"startup_ms\":" << startup_ms
                   << ",\"warmup_inference_ms_p99\":" << percentile(warmup_latencies, 0.99)
                   << '}';
        write_text_file(options.ready_file, ready_json.str());
        std::cout << "RS_GPU_NOISE_READY " << ready_json.str() << std::endl;

        std::vector<double> latencies;
        while (!stop_requested)
            latencies.push_back(run_inference(network, input_names, output_names, input));
        const auto process_end = std::chrono::steady_clock::now();

        double latency_sum = 0.0;
        for (const double value : latencies)
            latency_sum += value;
        const double mean = latencies.empty() ? 0.0 : latency_sum / latencies.size();
        const double duration_ms =
            std::chrono::duration<double, std::milli>(process_end - ready_time).count();
        const double minimum =
            latencies.empty() ? 0.0 : *std::min_element(latencies.begin(), latencies.end());
        const double maximum =
            latencies.empty() ? 0.0 : *std::max_element(latencies.begin(), latencies.end());

        std::ostringstream summary_json;
        summary_json << std::fixed << std::setprecision(6)
                     << "{\"schema_version\":1"
                     << ",\"mode\":\"mobilenet_v2_vulkan\""
                     << ",\"success\":true"
                     << ",\"gpu_device\":" << options.gpu_device
                     << ",\"gpu_name\":\"" << json_escape(gpu_info.device_name()) << "\""
                     << ",\"gpu_type\":" << gpu_info.type()
                     << ",\"driver_name\":\"" << json_escape(gpu_info.driver_name()) << "\""
                     << ",\"model_param\":\"" << json_escape(options.model_param) << "\""
                     << ",\"input_shape\":\"224x224x3\""
                     << ",\"weights\":\"deterministic_zero\""
                     << ",\"warmup_iterations\":" << options.warmup_iterations
                     << ",\"startup_ms\":" << startup_ms
                     << ",\"measurement_duration_ms\":" << duration_ms
                     << ",\"iterations\":" << latencies.size()
                     << ",\"inference_ms_min\":" << minimum
                     << ",\"inference_ms_mean\":" << mean
                     << ",\"inference_ms_p99\":" << percentile(latencies, 0.99)
                     << ",\"inference_ms_p999\":" << percentile(latencies, 0.999)
                     << ",\"inference_ms_max\":" << maximum
                     << '}';
        write_text_file(options.summary_output, summary_json.str());
        std::cout << "RS_GPU_NOISE_RESULT " << summary_json.str() << std::endl;
        return 0;
    }
    catch (const std::exception& error)
    {
        std::cerr << "RS_GPU_NOISE_ERROR {\"message\":\""
                  << json_escape(error.what()) << "\"}" << std::endl;
        return 2;
    }
}
