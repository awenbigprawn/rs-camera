cmake_minimum_required(VERSION 3.16)

project(rs_camera LANGUAGES C CXX)

option(
    RS_CAMERA_USE_SYSTEM_LIBREALSENSE
    "Use an installed librealsense2 package instead of the vendored submodule"
    OFF
)
option(
    RS_CAMERA_BUILD_GPU_NOISE
    "Build the pinned ncnn MobileNetV2 Vulkan GPU-noise workload"
    OFF
)
option(
    RS_CAMERA_V4L2_DIAGNOSTICS
    "Compile the low-overhead V4L2 stage diagnostic trace into vendored librealsense"
    OFF
)

if(RS_CAMERA_USE_SYSTEM_LIBREALSENSE AND RS_CAMERA_V4L2_DIAGNOSTICS)
    message(
        FATAL_ERROR
        "RS_CAMERA_V4L2_DIAGNOSTICS requires the vendored librealsense build"
    )
endif()

find_package(Threads REQUIRED)

if(RS_CAMERA_USE_SYSTEM_LIBREALSENSE)
    find_package(realsense2 CONFIG REQUIRED)

    if(TARGET realsense2::realsense2)
        set(RS_CAMERA_LIBREALSENSE_TARGET realsense2::realsense2)
    elseif(TARGET realsense2)
        set(RS_CAMERA_LIBREALSENSE_TARGET realsense2)
    else()
        message(FATAL_ERROR "The installed realsense2 package does not define a usable CMake target")
    endif()
else()
    if(NOT EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/deps/librealsense/CMakeLists.txt")
        message(
            FATAL_ERROR
            "The librealsense submodule is missing. Run: git submodule update --init --recursive"
        )
    endif()

    # Only build the librealsense functionality needed by smallest_test.
    set(BUILD_EXAMPLES OFF CACHE BOOL "")
    set(BUILD_GRAPHICAL_EXAMPLES OFF CACHE BOOL "")
    set(BUILD_GLSL_EXTENSIONS OFF CACHE BOOL "")
    set(BUILD_ROSBAG2 OFF CACHE BOOL "")
    set(BUILD_RS2_ALL OFF CACHE BOOL "")
    set(BUILD_TOOLS OFF CACHE BOOL "")
    set(BUILD_UNIT_TESTS OFF CACHE BOOL "")
    set(CHECK_FOR_UPDATES OFF CACHE BOOL "")

    add_subdirectory(deps/librealsense EXCLUDE_FROM_ALL)
    if(RS_CAMERA_V4L2_DIAGNOSTICS)
        target_compile_definitions(
            realsense2
            PRIVATE RS_CAMERA_V4L2_DIAGNOSTICS=1
        )
    endif()
    set(RS_CAMERA_LIBREALSENSE_TARGET realsense2)
endif()

add_executable(smallest_test src/smallest_test.cpp)
add_executable(d435_sensor_probe src/d435_sensor_probe.cpp)
add_executable(realsense_thread_lifecycle_probe src/realsense_thread_lifecycle_probe.cpp)
add_library(
    rs_camera_probe_support
    STATIC
        src/benchmark_utils.cpp
        src/realsense_utils.cpp
        src/steady_metrics.cpp
)
add_executable(
    realsense_steady_probe
    src/realsense_steady_probe.cpp
    src/deadline_scheduler.cpp
)

target_compile_features(smallest_test PRIVATE cxx_std_17)
target_compile_features(d435_sensor_probe PRIVATE cxx_std_17)
target_compile_features(realsense_thread_lifecycle_probe PRIVATE cxx_std_17)
target_compile_features(rs_camera_probe_support PUBLIC cxx_std_17)
target_compile_features(realsense_steady_probe PRIVATE cxx_std_17)

target_include_directories(
    rs_camera_probe_support
    PUBLIC
        "${CMAKE_CURRENT_SOURCE_DIR}/include"
)
target_link_libraries(
    rs_camera_probe_support
    PUBLIC
        "${RS_CAMERA_LIBREALSENSE_TARGET}"
)

target_include_directories(
    smallest_test
    PRIVATE
        "${CMAKE_CURRENT_SOURCE_DIR}/include"
)
target_include_directories(
    d435_sensor_probe
    PRIVATE
        "${CMAKE_CURRENT_SOURCE_DIR}/include"
)
target_include_directories(
    realsense_thread_lifecycle_probe
    PRIVATE
        "${CMAKE_CURRENT_SOURCE_DIR}/include"
)
target_include_directories(
    realsense_steady_probe
    PRIVATE
        "${CMAKE_CURRENT_SOURCE_DIR}/include"
)

target_link_libraries(
    smallest_test
    PRIVATE
        "${RS_CAMERA_LIBREALSENSE_TARGET}"
        Threads::Threads
)
target_link_libraries(
    d435_sensor_probe
    PRIVATE
        "${RS_CAMERA_LIBREALSENSE_TARGET}"
        Threads::Threads
)
target_link_libraries(
    realsense_thread_lifecycle_probe
    PRIVATE
        "${RS_CAMERA_LIBREALSENSE_TARGET}"
        Threads::Threads
)
target_link_libraries(
    realsense_steady_probe
    PRIVATE
        rs_camera_probe_support
        "${RS_CAMERA_LIBREALSENSE_TARGET}"
        Threads::Threads
        ${CMAKE_DL_LIBS}
)

add_executable(
    realsense_usb_storage_noise
    src/realsense_usb_storage_noise.cpp
)
target_compile_features(realsense_usb_storage_noise PRIVATE cxx_std_17)

add_executable(
    realsense_cpu_noise
    src/realsense_cpu_noise.cpp
)
target_compile_features(realsense_cpu_noise PRIVATE cxx_std_17)
target_link_libraries(realsense_cpu_noise PRIVATE Threads::Threads)

add_executable(
    realsense_memory_noise
    src/realsense_memory_noise.cpp
)
target_compile_features(realsense_memory_noise PRIVATE cxx_std_17)
target_link_libraries(realsense_memory_noise PRIVATE Threads::Threads)

if(RS_CAMERA_BUILD_GPU_NOISE)
    if(NOT EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/deps/ncnn/CMakeLists.txt")
        message(
            FATAL_ERROR
            "The ncnn submodule is missing. Run: git submodule update --init --recursive deps/ncnn"
        )
    endif()

    set(NCNN_VULKAN ON CACHE BOOL "" FORCE)
    set(NCNN_BUILD_BENCHMARK OFF CACHE BOOL "" FORCE)
    set(NCNN_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
    set(NCNN_BUILD_TESTS OFF CACHE BOOL "" FORCE)
    set(NCNN_BUILD_TOOLS OFF CACHE BOOL "" FORCE)
    set(NCNN_C_API OFF CACHE BOOL "" FORCE)
    set(NCNN_BF16 OFF CACHE BOOL "" FORCE)
    set(NCNN_INT8 OFF CACHE BOOL "" FORCE)
    set(NCNN_INSTALL_SDK OFF CACHE BOOL "" FORCE)
    set(NCNN_PIXEL OFF CACHE BOOL "" FORCE)
    set(NCNN_PYTHON OFF CACHE BOOL "" FORCE)
    set(NCNN_RUNTIME_CPU ON CACHE BOOL "" FORCE)

    add_subdirectory(deps/ncnn EXCLUDE_FROM_ALL)

    add_executable(realsense_gpu_noise src/realsense_gpu_noise.cpp)
    target_compile_features(realsense_gpu_noise PRIVATE cxx_std_17)
    target_compile_definitions(
        realsense_gpu_noise
        PRIVATE
            RS_CAMERA_MOBILENET_V2_PARAM="${CMAKE_CURRENT_SOURCE_DIR}/deps/ncnn/benchmark/models/mobilenet_v2.param"
    )
    target_link_libraries(
        realsense_gpu_noise
        PRIVATE
            ncnn
            Threads::Threads
    )
endif()
