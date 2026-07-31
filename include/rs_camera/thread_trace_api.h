#ifndef RS_CAMERA_THREAD_TRACE_API_H
#define RS_CAMERA_THREAD_TRACE_API_H

#include <stddef.h>
#include <stdint.h>
#include <sys/types.h>

#ifdef __cplusplus
extern "C" {
#endif

#define RS_THREAD_TRACE_SIGNATURE_SIZE 1024

struct rs_thread_trace_info
{
    uint32_t size;
    pid_t tid;
    uint64_t creation_sequence;
    char signature[RS_THREAD_TRACE_SIGNATURE_SIZE];
};

/*
 * Optional API exported by libtrace_pthreads.so. The steady probe resolves it
 * with dlsym(), so ordinary runs remain independent from the preload library.
 * The return value is the number of live traced pthreads. When capacity is
 * smaller than that number, the first capacity records are copied.
 */
size_t rs_thread_trace_snapshot(struct rs_thread_trace_info *records,
                                size_t capacity);

#ifdef __cplusplus
}
#endif

#endif
