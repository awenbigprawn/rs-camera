#include <librealsense2/rs.hpp>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdlib>
#include <cctype>
#include <dirent.h>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <map>
#include <memory>
#include <pthread.h>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <tuple>
#include <unistd.h>
#include <vector>

namespace
{
std::atomic_bool keep_running{ true };

struct options
{
    std::string serial;
    int frames = 0;
    bool enable_all_streams = false;
    bool try_all_ir = true;
    bool list_only = false;
};

struct thread_info
{
    int tid = 0;
    std::string name;
};

struct stream_key
{
    rs2_stream stream = RS2_STREAM_ANY;
    int index = -1;

    bool operator<( const stream_key & other ) const
    {
        return std::make_pair( stream, index ) < std::make_pair( other.stream, other.index );
    }
};

struct selected_profile
{
    rs2::stream_profile profile;
    std::string reason;
};

struct stream_sample
{
    unsigned long long count = 0;
    unsigned long long frame_number = 0;
    double timestamp_ms = 0.0;
    rs2_format format = RS2_FORMAT_ANY;
    int width = 0;
    int height = 0;
    int stride = 0;
    int bytes = 0;
    float center_depth_m = 0.0f;
    rs2_vector motion = {};
    bool has_motion = false;
    bool has_depth = false;
};

void on_signal( int )
{
    keep_running = false;
}

std::string read_first_line( const std::string & path )
{
    std::ifstream in( path );
    std::string line;
    std::getline( in, line );
    return line;
}

std::vector< thread_info > read_threads()
{
    std::vector< thread_info > threads;
    DIR * dir = opendir( "/proc/self/task" );
    if( ! dir )
        return threads;

    while( auto * entry = readdir( dir ) )
    {
        const std::string name = entry->d_name;
        if( name.empty() || ! std::all_of( name.begin(), name.end(), []( unsigned char ch ) { return std::isdigit( ch ); } ) )
            continue;

        thread_info info;
        info.tid = std::stoi( name );
        info.name = read_first_line( "/proc/self/task/" + name + "/comm" );
        threads.push_back( info );
    }

    closedir( dir );
    std::sort( threads.begin(), threads.end(), []( const thread_info & a, const thread_info & b ) { return a.tid < b.tid; } );
    return threads;
}

void print_thread_delta( const std::string & title,
                         const std::vector< thread_info > & before,
                         const std::vector< thread_info > & after )
{
    std::set< int > previous;
    for( const auto & t : before )
        previous.insert( t.tid );

    std::cout << "\n[" << title << "] threads: " << before.size() << " -> " << after.size()
              << " (delta " << static_cast< long >( after.size() ) - static_cast< long >( before.size() ) << ")\n";

    for( const auto & t : after )
    {
        if( previous.count( t.tid ) == 0 )
            std::cout << "  + tid=" << t.tid << " name=" << t.name << '\n';
    }
}

void print_thread_model_notes()
{
    std::cout
        << "\nSource-backed librealsense thread model notes for this probe:\n"
        << "  - pipeline::pipeline owns one dispatcher used for pipeline control/restart callbacks.\n"
        << "  - dispatcher creates one std::thread and runs queued actions serially.\n"
        << "  - processing_block/syncer/aggregator run inline on backend callback threads; they do not create extra worker threads here.\n"
        << "  - Linux native UVC/V4L2 creates one capture thread per active UVC device, normally Stereo Module and RGB Camera for D435.\n"
        << "  - RSUSB/libusb backend creates dispatchers, a libusb event thread, and per-stream active_object publish/watchdog workers.\n"
        << "  - HID motion sensors create HID read/power-management threads, but plain D435 PID 0x0b07 is not in the D400 HID/IMU PID set; D435i is.\n";
}

std::string info_or_unknown( const rs2::device & dev, rs2_camera_info info )
{
    return dev.supports( info ) ? dev.get_info( info ) : "unknown";
}

std::string sensor_info_or_unknown( const rs2::sensor & sensor, rs2_camera_info info )
{
    return sensor.supports( info ) ? sensor.get_info( info ) : "unknown";
}

std::string profile_to_string( const rs2::stream_profile & profile )
{
    std::ostringstream out;
    out << profile.stream_name() << " idx=" << profile.stream_index()
        << " fmt=" << rs2_format_to_string( profile.format() )
        << " fps=" << profile.fps();
    if( auto video = profile.as< rs2::video_stream_profile >() )
        out << " " << video.width() << "x" << video.height();
    if( profile.is_default() )
        out << " default";
    return out.str();
}

void print_device_inventory( const rs2::device & dev )
{
    std::cout << "Device:\n";
    std::cout << "  name: " << info_or_unknown( dev, RS2_CAMERA_INFO_NAME ) << '\n';
    std::cout << "  serial: " << info_or_unknown( dev, RS2_CAMERA_INFO_SERIAL_NUMBER ) << '\n';
    std::cout << "  product line: " << info_or_unknown( dev, RS2_CAMERA_INFO_PRODUCT_LINE ) << '\n';
    std::cout << "  product id: " << info_or_unknown( dev, RS2_CAMERA_INFO_PRODUCT_ID ) << '\n';
    std::cout << "  firmware: " << info_or_unknown( dev, RS2_CAMERA_INFO_FIRMWARE_VERSION ) << '\n';

    auto sensors = dev.query_sensors();
    std::cout << "Sensors: " << sensors.size() << '\n';
    for( size_t i = 0; i < sensors.size(); ++i )
    {
        const auto & sensor = sensors[i];
        std::cout << "  [" << i << "] " << sensor_info_or_unknown( sensor, RS2_CAMERA_INFO_NAME ) << '\n';

        auto profiles = sensor.get_stream_profiles();
        std::sort( profiles.begin(), profiles.end(), []( const rs2::stream_profile & a, const rs2::stream_profile & b ) {
            auto av = a.as< rs2::video_stream_profile >();
            auto bv = b.as< rs2::video_stream_profile >();
            return std::make_tuple( a.stream_type(), a.stream_index(), a.fps(), av ? av.width() : 0, av ? av.height() : 0, a.format() )
                < std::make_tuple( b.stream_type(), b.stream_index(), b.fps(), bv ? bv.width() : 0, bv ? bv.height() : 0, b.format() );
        } );

        std::map< stream_key, int > profile_counts;
        for( const auto & profile : profiles )
            ++profile_counts[{ profile.stream_type(), profile.stream_index() }];

        for( const auto & entry : profile_counts )
        {
            std::cout << "      " << rs2_stream_to_string( entry.first.stream )
                      << " idx=" << entry.first.index << " profiles=" << entry.second << '\n';
        }
    }
}

bool wanted_stream( rs2_stream stream )
{
    return stream == RS2_STREAM_COLOR
        || stream == RS2_STREAM_DEPTH
        || stream == RS2_STREAM_INFRARED
        || stream == RS2_STREAM_GYRO
        || stream == RS2_STREAM_ACCEL;
}

int video_preference_score( const rs2::video_stream_profile & video )
{
    const auto stream = video.stream_type();
    const auto format = video.format();
    int score = 0;

    if( video.fps() == 30 )
        score += 500;
    else if( video.fps() == 15 )
        score += 250;
    else
        score -= std::abs( video.fps() - 30 );

    if( stream == RS2_STREAM_DEPTH )
    {
        if( format == RS2_FORMAT_Z16 )
            score += 2000;
        if( video.width() == 848 && video.height() == 480 )
            score += 1000;
        else if( video.width() == 640 && video.height() == 480 )
            score += 700;
    }
    else if( stream == RS2_STREAM_INFRARED )
    {
        if( format == RS2_FORMAT_Y8 )
            score += 2000;
        if( video.width() == 848 && video.height() == 480 )
            score += 1000;
        else if( video.width() == 640 && video.height() == 480 )
            score += 700;
    }
    else if( stream == RS2_STREAM_COLOR )
    {
        if( format == RS2_FORMAT_RGB8 )
            score += 2000;
        else if( format == RS2_FORMAT_BGR8 )
            score += 1900;
        else if( format == RS2_FORMAT_YUYV )
            score += 1700;
        if( video.width() == 640 && video.height() == 480 )
            score += 1000;
        else if( video.width() == 1280 && video.height() == 720 )
            score += 800;
    }

    return score;
}

int profile_preference_score( const rs2::stream_profile & profile )
{
    int score = 0;
    if( profile.is_default() )
        score += profile.stream_type() == RS2_STREAM_INFRARED ? 200 : 5000;

    if( auto video = profile.as< rs2::video_stream_profile >() )
        score += video_preference_score( video );
    else
    {
        if( profile.stream_type() == RS2_STREAM_GYRO && profile.fps() == 200 )
            score += 1000;
        if( profile.stream_type() == RS2_STREAM_ACCEL && ( profile.fps() == 100 || profile.fps() == 63 ) )
            score += 1000;
        if( profile.format() == RS2_FORMAT_MOTION_XYZ32F )
            score += 500;
    }

    return score;
}

std::vector< selected_profile > choose_profiles( const rs2::device & dev, bool include_all_ir )
{
    std::map< stream_key, std::vector< rs2::stream_profile > > grouped;
    for( const auto & sensor : dev.query_sensors() )
    {
        for( const auto & profile : sensor.get_stream_profiles() )
        {
            if( wanted_stream( profile.stream_type() ) )
                grouped[{ profile.stream_type(), profile.stream_index() }].push_back( profile );
        }
    }

    std::vector< selected_profile > selected;
    bool selected_ir = false;

    for( auto & entry : grouped )
    {
        const auto key = entry.first;
        auto & profiles = entry.second;

        if( key.stream == RS2_STREAM_INFRARED && ! include_all_ir && selected_ir )
            continue;

        auto best = std::max_element( profiles.begin(), profiles.end(), []( const rs2::stream_profile & a, const rs2::stream_profile & b ) {
            return profile_preference_score( a ) < profile_preference_score( b );
        } );
        if( best == profiles.end() )
            continue;

        if( key.stream == RS2_STREAM_INFRARED )
            selected_ir = true;

        std::ostringstream reason;
        reason << "score=" << profile_preference_score( *best );
        selected.push_back( { *best, reason.str() } );
    }

    std::sort( selected.begin(), selected.end(), []( const selected_profile & a, const selected_profile & b ) {
        return stream_key{ a.profile.stream_type(), a.profile.stream_index() }
            < stream_key{ b.profile.stream_type(), b.profile.stream_index() };
    } );

    return selected;
}

void enable_profile( rs2::config & cfg, const rs2::stream_profile & profile )
{
    if( auto video = profile.as< rs2::video_stream_profile >() )
    {
        cfg.enable_stream( profile.stream_type(),
                           profile.stream_index(),
                           video.width(),
                           video.height(),
                           profile.format(),
                           profile.fps() );
    }
    else
    {
        cfg.enable_stream( profile.stream_type(),
                           profile.stream_index(),
                           profile.format(),
                           profile.fps() );
    }
}

void print_selected_profiles( const std::vector< selected_profile > & selected )
{
    std::cout << "Selected stream requests:\n";
    for( const auto & item : selected )
        std::cout << "  " << profile_to_string( item.profile ) << " (" << item.reason << ")\n";
}

rs2::pipeline_profile start_pipeline( rs2::pipeline & pipe,
                                      const options & opts,
                                      const std::vector< selected_profile > & selected )
{
    rs2::config cfg;
    if( ! opts.serial.empty() )
        cfg.enable_device( opts.serial );

    if( opts.enable_all_streams )
    {
        std::cout << "Using librealsense config.enable_all_streams().\n";
        cfg.enable_all_streams();
    }
    else
    {
        if( selected.empty() )
            throw std::runtime_error( "No RGB/depth/infrared/motion profiles were found on the selected device" );
        for( const auto & item : selected )
            enable_profile( cfg, item.profile );
    }

    return pipe.start( cfg );
}

void print_active_profiles( const rs2::pipeline_profile & profile )
{
    auto streams = profile.get_streams();
    std::sort( streams.begin(), streams.end(), []( const rs2::stream_profile & a, const rs2::stream_profile & b ) {
        return stream_key{ a.stream_type(), a.stream_index() } < stream_key{ b.stream_type(), b.stream_index() };
    } );

    std::cout << "Active streams:\n";
    for( const auto & stream : streams )
    {
        std::cout << "  " << profile_to_string( stream ) << '\n';
        if( auto video = stream.as< rs2::video_stream_profile >() )
        {
            try
            {
                const auto intr = video.get_intrinsics();
                std::cout << "      intrinsics: fx=" << intr.fx << " fy=" << intr.fy
                          << " ppx=" << intr.ppx << " ppy=" << intr.ppy
                          << " model=" << intr.model << '\n';
            }
            catch( const std::exception & e )
            {
                std::cout << "      intrinsics unavailable: " << e.what() << '\n';
            }
        }
    }

    for( size_t i = 0; i < streams.size(); ++i )
    {
        for( size_t j = i + 1; j < streams.size(); ++j )
        {
            try
            {
                const auto ex = streams[i].get_extrinsics_to( streams[j] );
                std::cout << "  extrinsics " << streams[i].stream_name() << " -> " << streams[j].stream_name()
                          << ": t=[" << ex.translation[0] << ", " << ex.translation[1] << ", " << ex.translation[2] << "]\n";
            }
            catch( const std::exception & )
            {
            }
        }
    }
}

std::string sample_key( const rs2::stream_profile & profile )
{
    std::ostringstream out;
    out << rs2_stream_to_string( profile.stream_type() ) << "#" << profile.stream_index();
    return out.str();
}

void update_sample( std::map< std::string, stream_sample > & samples, const rs2::frame & frame )
{
    const auto profile = frame.get_profile();
    auto & sample = samples[sample_key( profile )];
    sample.count += 1;
    sample.frame_number = frame.get_frame_number();
    sample.timestamp_ms = frame.get_timestamp();
    sample.format = profile.format();
    sample.bytes = frame.get_data_size();

    if( auto video = frame.as< rs2::video_frame >() )
    {
        sample.width = video.get_width();
        sample.height = video.get_height();
        sample.stride = video.get_stride_in_bytes();
    }

    if( auto depth = frame.as< rs2::depth_frame >() )
    {
        sample.has_depth = true;
        sample.center_depth_m = depth.get_distance( depth.get_width() / 2, depth.get_height() / 2 );
    }

    if( auto motion = frame.as< rs2::motion_frame >() )
    {
        sample.has_motion = true;
        sample.motion = motion.get_motion_data();
    }
}

void print_samples( const std::map< std::string, stream_sample > & samples )
{
    std::cout << "Samples:\n";
    for( const auto & entry : samples )
    {
        const auto & s = entry.second;
        std::cout << "  " << std::setw( 12 ) << entry.first
                  << " count=" << s.count
                  << " frame=" << s.frame_number
                  << " ts_ms=" << std::fixed << std::setprecision( 3 ) << s.timestamp_ms
                  << " fmt=" << rs2_format_to_string( s.format );
        if( s.width > 0 )
            std::cout << " " << s.width << "x" << s.height << " stride=" << s.stride << " bytes=" << s.bytes;
        if( s.has_depth )
            std::cout << " center_depth_m=" << std::setprecision( 4 ) << s.center_depth_m;
        if( s.has_motion )
            std::cout << " xyz=[" << s.motion.x << ", " << s.motion.y << ", " << s.motion.z << "]";
        std::cout << '\n';
    }
}

rs2::device select_device( rs2::context & ctx, const std::string & serial )
{
    auto devices = ctx.query_devices();
    if( devices.size() == 0 )
        throw std::runtime_error( "No RealSense devices found" );

    for( const auto & dev : devices )
    {
        if( serial.empty() || ( dev.supports( RS2_CAMERA_INFO_SERIAL_NUMBER ) && serial == dev.get_info( RS2_CAMERA_INFO_SERIAL_NUMBER ) ) )
            return dev;
    }

    throw std::runtime_error( "Requested serial number was not found: " + serial );
}

options parse_args( int argc, char ** argv )
{
    options opts;
    for( int i = 1; i < argc; ++i )
    {
        const std::string arg = argv[i];
        if( arg == "--serial" && i + 1 < argc )
            opts.serial = argv[++i];
        else if( arg == "--frames" && i + 1 < argc )
            opts.frames = std::stoi( argv[++i] );
        else if( arg == "--enable-all" )
            opts.enable_all_streams = true;
        else if( arg == "--single-ir" )
            opts.try_all_ir = false;
        else if( arg == "--list-only" )
            opts.list_only = true;
        else if( arg == "--help" || arg == "-h" )
        {
            std::cout << "Usage: " << argv[0] << " [--serial SERIAL] [--frames N] [--enable-all] [--single-ir] [--list-only]\n";
            std::exit( 0 );
        }
        else
            throw std::runtime_error( "Unknown or incomplete argument: " + arg );
    }
    return opts;
}
}  // namespace

int main( int argc, char ** argv )
try
{
    pthread_setname_np( pthread_self(), "d435-probe" );
    std::signal( SIGINT, on_signal );
    std::signal( SIGTERM, on_signal );

    const auto opts = parse_args( argc, argv );

    const auto threads_at_start = read_threads();
    print_thread_model_notes();

    rs2::context ctx;
    const auto threads_after_context = read_threads();
    print_thread_delta( "after rs2::context", threads_at_start, threads_after_context );

    auto dev = select_device( ctx, opts.serial );
    print_device_inventory( dev );
    const auto threads_after_inventory = read_threads();
    print_thread_delta( "after device inventory", threads_after_context, threads_after_inventory );
    if( opts.list_only )
        return 0;

    auto selected = choose_profiles( dev, opts.try_all_ir );
    if( ! opts.enable_all_streams )
        print_selected_profiles( selected );

    const auto threads_before_pipeline_object = read_threads();
    auto pipe = std::make_unique< rs2::pipeline >( ctx );
    const auto threads_after_pipeline_object = read_threads();
    print_thread_delta( "after pipeline object construction", threads_before_pipeline_object, threads_after_pipeline_object );

    rs2::pipeline_profile active_profile;
    const auto threads_before_start = read_threads();

    try
    {
        active_profile = start_pipeline( *pipe, opts, selected );
    }
    catch( const std::exception & e )
    {
        const auto ir_count = std::count_if( selected.begin(), selected.end(), []( const selected_profile & item ) {
            return item.profile.stream_type() == RS2_STREAM_INFRARED;
        } );
        if( opts.enable_all_streams || ir_count <= 1 )
            throw;

        std::cerr << "Start with multiple IR streams failed: " << e.what() << "\nRetrying with one IR stream.\n";
        selected = choose_profiles( dev, false );
        print_selected_profiles( selected );
        pipe = std::make_unique< rs2::pipeline >( ctx );
        active_profile = start_pipeline( *pipe, opts, selected );
    }

    const auto threads_after_start = read_threads();
    print_thread_delta( "after pipeline.start", threads_before_start, threads_after_start );
    print_active_profiles( active_profile );

    std::cout << "\nReading frames. Press Ctrl-C to stop";
    if( opts.frames > 0 )
        std::cout << " or wait for " << opts.frames << " framesets";
    std::cout << ".\n";

    std::map< std::string, stream_sample > samples;
    int framesets = 0;
    auto next_report = std::chrono::steady_clock::now();

    while( keep_running && ( opts.frames <= 0 || framesets < opts.frames ) )
    {
        auto frames = pipe->wait_for_frames();
        ++framesets;

        for( const auto & frame : frames )
            update_sample( samples, frame );

        const auto now = std::chrono::steady_clock::now();
        if( now >= next_report )
        {
            std::cout << "\nframesets=" << framesets << '\n';
            print_samples( samples );
            next_report = now + std::chrono::seconds( 1 );
        }
    }

    pipe->stop();
    pipe.reset();
    const auto threads_after_stop = read_threads();
    print_thread_delta( "after pipeline.stop and destroy", threads_after_start, threads_after_stop );
    return 0;
}
catch( const rs2::error & e )
{
    std::cerr << "RealSense error: " << e.what() << '\n';
    return 2;
}
catch( const std::exception & e )
{
    std::cerr << "Error: " << e.what() << '\n';
    return 1;
}
