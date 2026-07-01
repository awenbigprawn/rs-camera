cmake_minimum_required(VERSION 3.16)

project(rs_camera LANGUAGES C CXX)

option(
    RS_CAMERA_USE_SYSTEM_LIBREALSENSE
    "Use an installed librealsense2 package instead of the vendored submodule"
    OFF
)

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
    set(RS_CAMERA_LIBREALSENSE_TARGET realsense2)
endif()

add_executable(smallest_test src/smallest_test.cpp)
add_executable(d435_sensor_probe src/d435_sensor_probe.cpp)
add_executable(
    realsense_thread_lifecycle_probe
    tools/realsense_thread_trace/realsense_thread_lifecycle_probe.cpp
)

target_compile_features(smallest_test PRIVATE cxx_std_17)
target_compile_features(d435_sensor_probe PRIVATE cxx_std_17)
target_compile_features(realsense_thread_lifecycle_probe PRIVATE cxx_std_17)

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
        "${CMAKE_CURRENT_SOURCE_DIR}/tools/realsense_thread_trace"
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
