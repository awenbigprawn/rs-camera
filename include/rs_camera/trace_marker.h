#ifndef RS_CAMERA_TRACE_MARKER_H
#define RS_CAMERA_TRACE_MARKER_H

#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#ifdef __cplusplus
extern "C" {
#endif

static inline uint64_t rs_trace_boottime_ns(void)
{
    struct timespec ts;
    /* LiME uses bpf_ktime_get_boot_ns(); keep one clock domain. */
    clock_gettime(CLOCK_BOOTTIME, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

static inline uint64_t rs_trace_realtime_ns(void)
{
    struct timespec ts;
    /* Librealsense BACKEND_TIMESTAMP and TIME_OF_ARRIVAL use the epoch clock. */
    clock_gettime(CLOCK_REALTIME, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

static inline long rs_trace_gettid(void)
{
    return syscall(SYS_gettid);
}

static inline int rs_trace_marker_fd(void)
{
    static int fd = -2;
    if (fd != -2)
        return fd;

    const char *path = getenv("RS_THREAD_TRACE_FILE");
    if (!path || !path[0])
        path = "thread_trace.jsonl";

    fd = open(path, O_APPEND | O_CREAT | O_WRONLY | O_CLOEXEC, 0644);
    return fd;
}

static inline void rs_trace_append_json_string(char *dst, size_t dst_size, const char *src)
{
    if (!dst_size)
        return;

    size_t out = 0;
    dst[out++] = '"';
    for (const unsigned char *p = (const unsigned char *)src; *p && out + 7 < dst_size; ++p)
    {
        if (*p == '"' || *p == '\\')
        {
            dst[out++] = '\\';
            dst[out++] = (char)*p;
        }
        else if (*p >= 0x20 && *p < 0x7f)
        {
            dst[out++] = (char)*p;
        }
        else
        {
            int n = snprintf(dst + out, dst_size - out, "\\u%04x", *p);
            if (n < 0)
                break;
            out += (size_t)n;
        }
    }
    if (out < dst_size)
        dst[out++] = '"';
    if (out >= dst_size)
        out = dst_size - 1;
    dst[out] = '\0';
}

static inline void rs_trace_phase_marker(const char *name)
{
    int fd = rs_trace_marker_fd();
    if (fd < 0)
        return;

    char escaped[256];
    char line[512];
    rs_trace_append_json_string(escaped, sizeof(escaped), name ? name : "");

    int n = snprintf(line,
                     sizeof(line),
                     "{\"event\":\"phase_marker\",\"timestamp_ns\":%llu,\"tid\":%ld,\"name\":%s}\n",
                     (unsigned long long)rs_trace_boottime_ns(),
                     rs_trace_gettid(),
                     escaped);
    if (n > 0)
    {
        ssize_t written = write(fd, line, (size_t)n < sizeof(line) ? (size_t)n : sizeof(line) - 1);
        (void)written;
    }
}

#ifdef __cplusplus
}
#endif

#endif
