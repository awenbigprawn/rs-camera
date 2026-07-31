#define _GNU_SOURCE

#include <dlfcn.h>
#include <execinfo.h>
#include <fcntl.h>
#include <inttypes.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#include "rs_camera/thread_trace_api.h"

#define TRACE_MAX_STACK 10
#define TRACE_LINE_SIZE 4096
#define TRACE_MAX_THREADS 512

struct trace_live_thread
{
    pid_t tid;
    uint64_t creation_sequence;
    int alive;
    char signature[RS_THREAD_TRACE_SIGNATURE_SIZE];
};

static pthread_mutex_t trace_registry_mutex = PTHREAD_MUTEX_INITIALIZER;
static struct trace_live_thread trace_registry[TRACE_MAX_THREADS];
static size_t trace_registry_count = 0;
static uint64_t trace_next_creation_sequence = 1;

struct trace_start_context
{
    void *(*start_routine)(void *);
    void *arg;
    pid_t parent_tid;
    uint64_t create_timestamp_ns;
    uint64_t creation_sequence;
    char signature[RS_THREAD_TRACE_SIGNATURE_SIZE];
};

static int trace_fd = -1;
static __thread int trace_guard = 0;
static __thread void *trace_tls_entry = NULL;
static __thread uint64_t trace_tls_start_ns = 0;
static __thread pid_t trace_tls_parent_tid = 0;

static int (*real_pthread_create)(pthread_t *, const pthread_attr_t *, void *(*)(void *), void *) = NULL;
static int (*real_pthread_join)(pthread_t, void **) = NULL;
static int (*real_pthread_detach)(pthread_t) = NULL;
static void (*real_pthread_exit)(void *) = NULL;
static int (*real_pthread_setname_np)(pthread_t, const char *) = NULL;
static int (*real_pthread_getname_np)(pthread_t, char *, size_t) = NULL;

static uint64_t trace_now_ns(void)
{
    struct timespec ts;
    /* Match LiME/eBPF bpf_ktime_get_boot_ns(). */
    clock_gettime(CLOCK_BOOTTIME, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

static void append_fmt(char **p, char *end, const char *fmt, ...);

static pid_t trace_gettid(void)
{
    return (pid_t)syscall(SYS_gettid);
}

static const char *trace_basename(const char *path)
{
    const char *slash;
    if (!path)
        return "";
    slash = strrchr(path, '/');
    return slash ? slash + 1 : path;
}

static void trace_build_signature(void *entry,
                                  void **frames,
                                  int frame_count,
                                  char *output,
                                  size_t output_size)
{
    Dl_info entry_info;
    char entry_module[256] = "";
    char *position = output;
    char *end = output + output_size;
    int included = 0;

    if (output_size == 0)
        return;
    output[0] = '\0';
    memset(&entry_info, 0, sizeof(entry_info));
    if (dladdr(entry, &entry_info) && entry_info.dli_fname)
        snprintf(entry_module, sizeof(entry_module), "%s",
                 trace_basename(entry_info.dli_fname));

    if (strcmp(entry_module, "realsense_steady_probe") == 0)
        append_fmt(&position, end, "entry=%s", entry_module);
    else
        append_fmt(&position, end, "entry=%s@0x%" PRIxPTR,
                   entry_module,
                   entry_info.dli_fbase
                       ? (uintptr_t)entry - (uintptr_t)entry_info.dli_fbase
                       : (uintptr_t)entry);

    for (int index = 0; index < frame_count && included < 6; ++index)
    {
        Dl_info info;
        const char *module;
        if (!dladdr(frames[index], &info) || !info.dli_fname)
            continue;
        module = trace_basename(info.dli_fname);
        if (strstr(module, "libtrace_pthreads.so") != NULL)
            continue;
        if (entry_module[0] && strcmp(module, entry_module) == 0)
            continue;
        if (strcmp(module, "realsense_steady_probe") == 0)
            append_fmt(&position, end, "|%s", module);
        else
            append_fmt(&position, end, "|%s@0x%" PRIxPTR,
                       module,
                       (uintptr_t)frames[index] - (uintptr_t)info.dli_fbase);
        ++included;
    }
}

static void trace_registry_add(const struct trace_start_context *ctx)
{
    pthread_mutex_lock(&trace_registry_mutex);
    if (trace_registry_count < TRACE_MAX_THREADS)
    {
        struct trace_live_thread *record = &trace_registry[trace_registry_count++];
        memset(record, 0, sizeof(*record));
        record->tid = trace_gettid();
        record->creation_sequence = ctx->creation_sequence;
        record->alive = 1;
        snprintf(record->signature, sizeof(record->signature), "%s", ctx->signature);
    }
    pthread_mutex_unlock(&trace_registry_mutex);
}

static void trace_registry_exit(pid_t tid)
{
    pthread_mutex_lock(&trace_registry_mutex);
    for (size_t index = trace_registry_count; index > 0; --index)
    {
        struct trace_live_thread *record = &trace_registry[index - 1];
        if (record->tid == tid && record->alive)
        {
            record->alive = 0;
            break;
        }
    }
    pthread_mutex_unlock(&trace_registry_mutex);
}

__attribute__((visibility("default")))
size_t rs_thread_trace_snapshot(struct rs_thread_trace_info *records,
                                size_t capacity)
{
    size_t live = 0;
    pthread_mutex_lock(&trace_registry_mutex);
    for (size_t index = 0; index < trace_registry_count; ++index)
    {
        const struct trace_live_thread *source = &trace_registry[index];
        if (!source->alive)
            continue;
        if (records && live < capacity)
        {
            struct rs_thread_trace_info *destination = &records[live];
            memset(destination, 0, sizeof(*destination));
            destination->size = sizeof(*destination);
            destination->tid = source->tid;
            destination->creation_sequence = source->creation_sequence;
            snprintf(destination->signature, sizeof(destination->signature),
                     "%s", source->signature);
        }
        ++live;
    }
    pthread_mutex_unlock(&trace_registry_mutex);
    return live;
}

static void trace_load_real_symbols(void)
{
    if (!real_pthread_create)
        real_pthread_create = dlsym(RTLD_NEXT, "pthread_create");
    if (!real_pthread_join)
        real_pthread_join = dlsym(RTLD_NEXT, "pthread_join");
    if (!real_pthread_detach)
        real_pthread_detach = dlsym(RTLD_NEXT, "pthread_detach");
    if (!real_pthread_exit)
        real_pthread_exit = dlsym(RTLD_NEXT, "pthread_exit");
    if (!real_pthread_setname_np)
        real_pthread_setname_np = dlsym(RTLD_NEXT, "pthread_setname_np");
    if (!real_pthread_getname_np)
        real_pthread_getname_np = dlsym(RTLD_NEXT, "pthread_getname_np");
}

static void trace_open_file(void)
{
    if (trace_fd >= 0)
        return;

    const char *path = getenv("RS_THREAD_TRACE_FILE");
    if (!path || !path[0])
        path = "thread_trace.jsonl";

    int fd = open(path, O_APPEND | O_CREAT | O_WRONLY | O_CLOEXEC, 0644);
    if (fd >= 0)
        trace_fd = fd;
}

static void append_fmt(char **p, char *end, const char *fmt, ...)
{
    if (*p >= end)
        return;

    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(*p, (size_t)(end - *p), fmt, ap);
    va_end(ap);

    if (n < 0)
        return;
    if (n >= end - *p)
        *p = end - 1;
    else
        *p += n;
}

static void append_json_string(char **p, char *end, const char *s)
{
    append_fmt(p, end, "\"");
    if (!s)
        s = "";

    for (const unsigned char *c = (const unsigned char *)s; *c && *p < end - 8; ++c)
    {
        if (*c == '"' || *c == '\\')
            append_fmt(p, end, "\\%c", *c);
        else if (*c == '\n')
            append_fmt(p, end, "\\n");
        else if (*c == '\r')
            append_fmt(p, end, "\\r");
        else if (*c == '\t')
            append_fmt(p, end, "\\t");
        else if (*c >= 0x20 && *c < 0x7f)
            append_fmt(p, end, "%c", *c);
        else
            append_fmt(p, end, "\\u%04x", *c);
    }
    append_fmt(p, end, "\"");
}

static void append_address_info(char **p, char *end, const char *prefix, void *addr)
{
    Dl_info info;
    uintptr_t offset = 0;

    append_fmt(p, end, "\"%s_address\":\"0x%" PRIxPTR "\"", prefix, (uintptr_t)addr);
    if (dladdr(addr, &info) && info.dli_fname)
    {
        offset = (uintptr_t)addr - (uintptr_t)info.dli_fbase;
        append_fmt(p, end, ",\"%s_module\":", prefix);
        append_json_string(p, end, info.dli_fname);
        append_fmt(p, end, ",\"%s_module_offset\":\"0x%" PRIxPTR "\"", prefix, offset);
        append_fmt(p, end, ",\"%s_symbol\":", prefix);
        append_json_string(p, end, info.dli_sname ? info.dli_sname : "");
    }
}

static void append_stack(char **p, char *end, void **frames, int frame_count)
{
    append_fmt(p, end, "\"stack\":[");
    for (int i = 0; i < frame_count; ++i)
    {
        Dl_info info;
        if (i)
            append_fmt(p, end, ",");
        append_fmt(p, end, "{\"address\":\"0x%" PRIxPTR "\"", (uintptr_t)frames[i]);
        if (dladdr(frames[i], &info) && info.dli_fname)
        {
            append_fmt(p, end, ",\"module\":");
            append_json_string(p, end, info.dli_fname);
            append_fmt(p, end, ",\"module_offset\":\"0x%" PRIxPTR "\"",
                       (uintptr_t)frames[i] - (uintptr_t)info.dli_fbase);
            append_fmt(p, end, ",\"symbol\":");
            append_json_string(p, end, info.dli_sname ? info.dli_sname : "");
        }
        append_fmt(p, end, "}");
    }
    append_fmt(p, end, "]");
}

static void trace_write_line(char *line, char *p)
{
    if (trace_fd < 0)
        trace_open_file();
    if (trace_fd < 0)
        return;

    if (p <= line || p[-1] != '\n')
    {
        if (p < line + TRACE_LINE_SIZE - 1)
            *p++ = '\n';
        else
            line[TRACE_LINE_SIZE - 2] = '\n';
    }
    size_t len = (size_t)(p - line);
    if (len > TRACE_LINE_SIZE)
        len = TRACE_LINE_SIZE;
    ssize_t written = write(trace_fd, line, len);
    (void)written;
}

static void trace_emit_simple(const char *event)
{
    char line[TRACE_LINE_SIZE];
    char *p = line;
    char *end = line + sizeof(line);

    append_fmt(&p, end, "{\"event\":");
    append_json_string(&p, end, event);
    append_fmt(&p, end, ",\"timestamp_ns\":%" PRIu64 ",\"tid\":%ld}",
               trace_now_ns(), (long)trace_gettid());
    trace_write_line(line, p);
}

static void trace_emit_pthread_create(uint64_t timestamp_ns,
                                      uint64_t return_timestamp_ns,
                                      pid_t caller_tid,
                                      pthread_t pthread_value,
                                      void *entry,
                                      void *arg,
                                      int result,
                                      void **frames,
                                      int frame_count)
{
    char line[TRACE_LINE_SIZE];
    char *p = line;
    char *end = line + sizeof(line);

    append_fmt(&p, end,
               "{\"event\":\"pthread_create\",\"timestamp_ns\":%" PRIu64
               ",\"return_timestamp_ns\":%" PRIu64
               ",\"caller_tid\":%ld,\"pthread_value\":\"0x%" PRIxPTR "\"",
               timestamp_ns,
               return_timestamp_ns,
               (long)caller_tid,
               (uintptr_t)pthread_value);
    append_fmt(&p, end, ",");
    append_address_info(&p, end, "entry", entry);
    append_fmt(&p, end, ",\"arg_address\":\"0x%" PRIxPTR "\",\"result\":%d,\"success\":%s,",
               (uintptr_t)arg,
               result,
               result == 0 ? "true" : "false");
    append_stack(&p, end, frames, frame_count);
    append_fmt(&p, end, "}");
    trace_write_line(line, p);
}

static void trace_emit_thread_start(struct trace_start_context *ctx)
{
    char name[64] = "";
    pthread_t self = pthread_self();
    if (real_pthread_getname_np)
        real_pthread_getname_np(self, name, sizeof(name));

    char line[TRACE_LINE_SIZE];
    char *p = line;
    char *end = line + sizeof(line);

    append_fmt(&p, end,
               "{\"event\":\"thread_start\",\"timestamp_ns\":%" PRIu64
               ",\"tid\":%ld,\"parent_tid\":%ld,\"pthread_value\":\"0x%" PRIxPTR "\",",
               trace_tls_start_ns,
               (long)trace_gettid(),
               (long)ctx->parent_tid,
               (uintptr_t)self);
    append_address_info(&p, end, "entry", ctx->start_routine);
    append_fmt(&p, end,
               ",\"arg_address\":\"0x%" PRIxPTR
               "\",\"create_timestamp_ns\":%" PRIu64
               ",\"creation_sequence\":%" PRIu64 ",\"signature\":",
               (uintptr_t)ctx->arg,
               ctx->create_timestamp_ns,
               ctx->creation_sequence);
    append_json_string(&p, end, ctx->signature);
    append_fmt(&p, end, ",\"name\":");
    append_json_string(&p, end, name);
    append_fmt(&p, end, "}");
    trace_write_line(line, p);
}

static void trace_emit_thread_exit(const char *exit_kind, void *retval)
{
    char name[64] = "";
    pthread_t self = pthread_self();
    if (real_pthread_getname_np)
        real_pthread_getname_np(self, name, sizeof(name));

    char line[TRACE_LINE_SIZE];
    char *p = line;
    char *end = line + sizeof(line);

    append_fmt(&p, end,
               "{\"event\":\"thread_exit\",\"timestamp_ns\":%" PRIu64
               ",\"tid\":%ld,\"parent_tid\":%ld,\"pthread_value\":\"0x%" PRIxPTR "\"",
               trace_now_ns(),
               (long)trace_gettid(),
               (long)trace_tls_parent_tid,
               (uintptr_t)self);
    append_fmt(&p, end, ",");
    append_address_info(&p, end, "entry", trace_tls_entry);
    append_fmt(&p, end,
               ",\"retval\":\"0x%" PRIxPTR "\",\"lifetime_ns\":%" PRIu64
               ",\"exit_kind\":",
               (uintptr_t)retval,
               trace_tls_start_ns ? trace_now_ns() - trace_tls_start_ns : 0);
    append_json_string(&p, end, exit_kind);
    append_fmt(&p, end, ",\"name\":");
    append_json_string(&p, end, name);
    append_fmt(&p, end, "}");
    trace_write_line(line, p);
}

static void *trace_start_trampoline(void *opaque)
{
    struct trace_start_context ctx = *(struct trace_start_context *)opaque;
    free(opaque);

    trace_tls_entry = (void *)ctx.start_routine;
    trace_tls_parent_tid = ctx.parent_tid;
    trace_tls_start_ns = trace_now_ns();
    trace_registry_add(&ctx);
    trace_emit_thread_start(&ctx);

    void *retval = ctx.start_routine(ctx.arg);
    trace_registry_exit(trace_gettid());
    trace_emit_thread_exit("return", retval);
    return retval;
}

__attribute__((constructor)) static void trace_constructor(void)
{
    trace_load_real_symbols();
    trace_open_file();
    trace_emit_simple("tracer_loaded");
}

__attribute__((destructor)) static void trace_destructor(void)
{
    trace_emit_simple("tracer_unloaded");
    if (trace_fd >= 0)
        close(trace_fd);
}

int pthread_create(pthread_t *thread,
                   const pthread_attr_t *attr,
                   void *(*start_routine)(void *),
                   void *arg)
{
    trace_load_real_symbols();
    if (trace_guard || !real_pthread_create)
        return real_pthread_create ? real_pthread_create(thread, attr, start_routine, arg) : -1;

    trace_guard = 1;
    uint64_t timestamp_ns = trace_now_ns();
    pid_t caller_tid = trace_gettid();
    void *frames[TRACE_MAX_STACK];
    int frame_count = backtrace(frames, TRACE_MAX_STACK);

    struct trace_start_context *ctx = calloc(1, sizeof(*ctx));
    int result;
    if (ctx)
    {
        ctx->start_routine = start_routine;
        ctx->arg = arg;
        ctx->parent_tid = caller_tid;
        ctx->create_timestamp_ns = timestamp_ns;
        ctx->creation_sequence =
            __atomic_fetch_add(&trace_next_creation_sequence, 1, __ATOMIC_RELAXED);
        trace_build_signature((void *)start_routine,
                              frames,
                              frame_count,
                              ctx->signature,
                              sizeof(ctx->signature));
        result = real_pthread_create(thread, attr, trace_start_trampoline, ctx);
        if (result != 0)
            free(ctx);
    }
    else
    {
        result = real_pthread_create(thread, attr, start_routine, arg);
    }

    pthread_t pthread_value = (result == 0) ? *thread : (pthread_t)0;
    trace_emit_pthread_create(timestamp_ns,
                              trace_now_ns(),
                              caller_tid,
                              pthread_value,
                              (void *)start_routine,
                              arg,
                              result,
                              frames,
                              frame_count);
    trace_guard = 0;
    return result;
}

int pthread_join(pthread_t thread, void **retval)
{
    trace_load_real_symbols();
    if (trace_guard || !real_pthread_join)
        return real_pthread_join ? real_pthread_join(thread, retval) : -1;

    trace_guard = 1;
    uint64_t begin_ns = trace_now_ns();
    pid_t caller_tid = trace_gettid();

    char line[TRACE_LINE_SIZE];
    char *p = line;
    char *end = line + sizeof(line);
    append_fmt(&p, end,
               "{\"event\":\"pthread_join_begin\",\"timestamp_ns\":%" PRIu64
               ",\"caller_tid\":%ld,\"pthread_value\":\"0x%" PRIxPTR "\"}",
               begin_ns,
               (long)caller_tid,
               (uintptr_t)thread);
    trace_write_line(line, p);

    int result = real_pthread_join(thread, retval);
    uint64_t end_ns = trace_now_ns();

    p = line;
    append_fmt(&p, end,
               "{\"event\":\"pthread_join_end\",\"timestamp_ns\":%" PRIu64
               ",\"caller_tid\":%ld,\"pthread_value\":\"0x%" PRIxPTR
               "\",\"duration_ns\":%" PRIu64 ",\"result\":%d}",
               end_ns,
               (long)caller_tid,
               (uintptr_t)thread,
               end_ns - begin_ns,
               result);
    trace_write_line(line, p);
    trace_guard = 0;
    return result;
}

int pthread_detach(pthread_t thread)
{
    trace_load_real_symbols();
    if (trace_guard || !real_pthread_detach)
        return real_pthread_detach ? real_pthread_detach(thread) : -1;

    trace_guard = 1;
    int result = real_pthread_detach(thread);

    char line[TRACE_LINE_SIZE];
    char *p = line;
    char *end = line + sizeof(line);
    append_fmt(&p, end,
               "{\"event\":\"pthread_detach\",\"timestamp_ns\":%" PRIu64
               ",\"caller_tid\":%ld,\"pthread_value\":\"0x%" PRIxPTR "\",\"result\":%d}",
               trace_now_ns(),
               (long)trace_gettid(),
               (uintptr_t)thread,
               result);
    trace_write_line(line, p);
    trace_guard = 0;
    return result;
}

int pthread_setname_np(pthread_t thread, const char *name)
{
    trace_load_real_symbols();
    if (trace_guard || !real_pthread_setname_np)
        return real_pthread_setname_np ? real_pthread_setname_np(thread, name) : -1;

    trace_guard = 1;
    int result = real_pthread_setname_np(thread, name);

    char line[TRACE_LINE_SIZE];
    char *p = line;
    char *end = line + sizeof(line);
    append_fmt(&p, end,
               "{\"event\":\"thread_name\",\"timestamp_ns\":%" PRIu64
               ",\"caller_tid\":%ld,\"pthread_value\":\"0x%" PRIxPTR "\",\"result\":%d,\"name\":",
               trace_now_ns(),
               (long)trace_gettid(),
               (uintptr_t)thread,
               result);
    append_json_string(&p, end, name);
    append_fmt(&p, end, "}");
    trace_write_line(line, p);
    trace_guard = 0;
    return result;
}

void pthread_exit(void *retval)
{
    trace_load_real_symbols();
    if (!trace_guard)
    {
        trace_guard = 1;
        trace_registry_exit(trace_gettid());
        trace_emit_thread_exit("pthread_exit", retval);
        trace_guard = 0;
    }
    real_pthread_exit(retval);
    __builtin_unreachable();
}
