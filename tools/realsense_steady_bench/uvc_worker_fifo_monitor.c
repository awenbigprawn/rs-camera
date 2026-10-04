// SPDX-License-Identifier: BSD-2-Clause
// Promote kworkers observed in uvc_video_copy_data_work, then restore them.

#define _GNU_SOURCE

#include <bpf/libbpf.h>
#include <errno.h>
#include <getopt.h>
#include <linux/sched.h>
#include <sched.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <time.h>
#include <unistd.h>

struct worker_event
{
    uint32_t tid;
    uint32_t tgid;
    uint64_t timestamp_ns;
    char comm[16];
};

struct worker_state
{
    pid_t tid;
    int original_policy;
    struct sched_param original_parameter;
    unsigned long long start_time;
    bool changed;
    char comm[17];
};

struct controller
{
    struct worker_state workers[256];
    size_t worker_count;
    int policy;
    int priority;
    FILE *log;
};

static volatile sig_atomic_t stop_requested;

static void request_stop(int signal_number)
{
    (void)signal_number;
    stop_requested = 1;
}

static double monotonic_seconds(void)
{
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return (double)now.tv_sec + (double)now.tv_nsec / 1000000000.0;
}

static unsigned long long task_start_time(pid_t tid)
{
    char path[64];
    char buffer[4096];
    char *cursor;
    char *save = NULL;
    int field = 3;
    FILE *file;

    snprintf(path, sizeof(path), "/proc/%d/stat", tid);
    file = fopen(path, "r");
    if (!file)
        return 0;
    if (!fgets(buffer, sizeof(buffer), file))
    {
        fclose(file);
        return 0;
    }
    fclose(file);

    cursor = strrchr(buffer, ')');
    if (!cursor || cursor[1] != ' ')
        return 0;
    cursor += 2;
    for (char *token = strtok_r(cursor, " ", &save); token;
         token = strtok_r(NULL, " ", &save), ++field)
    {
        if (field == 22)
            return strtoull(token, NULL, 10);
    }
    return 0;
}

static const char *policy_name(int policy)
{
    switch (policy)
    {
    case -1: return "UNCHANGED";
    case SCHED_OTHER: return "SCHED_OTHER";
    case SCHED_FIFO: return "SCHED_FIFO";
    case SCHED_RR: return "SCHED_RR";
    case SCHED_BATCH: return "SCHED_BATCH";
    case SCHED_IDLE: return "SCHED_IDLE";
    case SCHED_DEADLINE: return "SCHED_DEADLINE";
    default: return "UNKNOWN";
    }
}

static void log_action(
    struct controller *controller,
    const char *action,
    const struct worker_state *worker,
    int error_number)
{
    const double timestamp = monotonic_seconds();
    fprintf(
        controller->log,
        "{\"timestamp_s\":%.9f,\"action\":\"%s\",\"tid\":%d,"
        "\"comm\":\"%s\",\"original_policy\":\"%s\","
        "\"original_priority\":%d,\"target_policy\":\"%s\","
        "\"target_priority\":%d,\"errno\":%d}\n",
        timestamp,
        action,
        worker->tid,
        worker->comm,
        policy_name(worker->original_policy),
        worker->original_parameter.sched_priority,
        policy_name(controller->policy),
        controller->priority,
        error_number);
    fflush(controller->log);
}

static int handle_worker(void *context, void *data, size_t size)
{
    struct controller *controller = context;
    const struct worker_event *event = data;
    struct sched_param target = {.sched_priority = 0};
    struct worker_state *worker;
    int policy;

    if (size < sizeof(*event) || controller->worker_count >= 256)
        return 0;
    for (size_t index = 0; index < controller->worker_count; ++index)
    {
        if (controller->workers[index].tid == (pid_t)event->tid)
            return 0;
    }

    worker = &controller->workers[controller->worker_count++];
    memset(worker, 0, sizeof(*worker));
    worker->tid = (pid_t)event->tid;
    memcpy(worker->comm, event->comm, sizeof(event->comm));
    worker->comm[sizeof(worker->comm) - 1] = '\0';
    worker->start_time = task_start_time(worker->tid);

    policy = sched_getscheduler(worker->tid);
    if (policy < 0 || sched_getparam(worker->tid, &worker->original_parameter) != 0)
    {
        log_action(controller, "inspect-failed", worker, errno);
        return 0;
    }
    worker->original_policy = policy;

    if (controller->policy == -1)
    {
        log_action(controller, "observed", worker, 0);
        return 0;
    }

    if (policy == SCHED_DEADLINE ||
        (policy == controller->policy &&
         worker->original_parameter.sched_priority >= controller->priority))
    {
        log_action(controller, "kept-existing-policy", worker, 0);
        return 0;
    }

    target.sched_priority = controller->priority;
    if (sched_setscheduler(worker->tid, controller->policy, &target) != 0)
    {
        log_action(controller, "promote-failed", worker, errno);
        return 0;
    }
    worker->changed = true;
    log_action(controller, "promoted", worker, 0);
    return 0;
}

static void restore_workers(struct controller *controller)
{
    for (size_t index = controller->worker_count; index > 0; --index)
    {
        struct worker_state *worker = &controller->workers[index - 1];
        int saved_errno = 0;

        if (!worker->changed)
            continue;
        if (worker->start_time == 0 || task_start_time(worker->tid) != worker->start_time)
        {
            log_action(controller, "restore-skipped-task-gone", worker, 0);
            continue;
        }
        if (sched_setscheduler(
                worker->tid,
                worker->original_policy,
                &worker->original_parameter) != 0)
            saved_errno = errno;
        log_action(
            controller,
            saved_errno ? "restore-failed" : "restored",
            worker,
            saved_errno);
    }
}

static void usage(const char *program)
{
    fprintf(
        stderr,
        "Usage: %s --bpf-object FILE [--policy keep|fifo|rr] [--priority 89] "
        "[--duration SECONDS] [--log FILE]\n",
        program);
}

static int parse_policy(const char *value)
{
    if (strcmp(value, "keep") == 0)
        return -1;
    if (strcmp(value, "fifo") == 0)
        return SCHED_FIFO;
    if (strcmp(value, "rr") == 0)
        return SCHED_RR;
    return -2;
}

int main(int argc, char **argv)
{
    const char *object_path = NULL;
    const char *log_path = NULL;
    double duration = 0.0;
    double deadline = 0.0;
    struct controller controller = {
        .policy = SCHED_FIFO,
        .priority = 89,
        .log = stdout,
    };
    struct bpf_object *object = NULL;
    struct bpf_program *program;
    struct bpf_link *link = NULL;
    struct ring_buffer *ring = NULL;
    int map_fd;
    int result = EXIT_FAILURE;
    int option;
    const struct option options[] = {
        {"bpf-object", required_argument, NULL, 'b'},
        {"policy", required_argument, NULL, 's'},
        {"priority", required_argument, NULL, 'p'},
        {"duration", required_argument, NULL, 'd'},
        {"log", required_argument, NULL, 'l'},
        {"help", no_argument, NULL, 'h'},
        {NULL, 0, NULL, 0},
    };

    while ((option = getopt_long(argc, argv, "b:s:p:d:l:h", options, NULL)) != -1)
    {
        switch (option)
        {
        case 'b': object_path = optarg; break;
        case 's': controller.policy = parse_policy(optarg); break;
        case 'p': controller.priority = atoi(optarg); break;
        case 'd': duration = strtod(optarg, NULL); break;
        case 'l': log_path = optarg; break;
        case 'h': usage(argv[0]); return EXIT_SUCCESS;
        default: usage(argv[0]); return EXIT_FAILURE;
        }
    }
    if (!object_path ||
        (controller.policy != -1 &&
         controller.policy != SCHED_FIFO && controller.policy != SCHED_RR) ||
        controller.priority < 1 || controller.priority > 99 || duration < 0.0)
    {
        usage(argv[0]);
        return EXIT_FAILURE;
    }
    if (log_path)
    {
        controller.log = fopen(log_path, "w");
        if (!controller.log)
        {
            perror("fopen log");
            return EXIT_FAILURE;
        }
    }

    signal(SIGINT, request_stop);
    signal(SIGTERM, request_stop);
    libbpf_set_strict_mode(LIBBPF_STRICT_ALL);
    object = bpf_object__open_file(object_path, NULL);
    if (libbpf_get_error(object))
    {
        object = NULL;
        fprintf(stderr, "Cannot open BPF object %s\n", object_path);
        goto cleanup;
    }
    if (bpf_object__load(object) != 0)
    {
        fprintf(stderr, "Cannot load BPF object %s\n", object_path);
        goto cleanup;
    }
    program = bpf_object__next_program(object, NULL);
    if (!program)
    {
        fprintf(stderr, "BPF object contains no program\n");
        goto cleanup;
    }
    link = bpf_program__attach(program);
    if (libbpf_get_error(link))
    {
        link = NULL;
        fprintf(stderr, "Cannot attach kprobe to uvc_video_copy_data_work\n");
        goto cleanup;
    }
    map_fd = bpf_object__find_map_fd_by_name(object, "events");
    if (map_fd < 0)
    {
        fprintf(stderr, "Cannot find events ring buffer\n");
        goto cleanup;
    }
    ring = ring_buffer__new(map_fd, handle_worker, &controller, NULL);
    if (!ring)
    {
        fprintf(stderr, "Cannot create ring-buffer reader\n");
        goto cleanup;
    }

    fprintf(
        stderr,
        "UVC worker monitor active: %s priority %d\n",
        policy_name(controller.policy),
        controller.priority);
    if (duration > 0.0)
        deadline = monotonic_seconds() + duration;
    while (!stop_requested && (deadline == 0.0 || monotonic_seconds() < deadline))
    {
        const int poll_result = ring_buffer__poll(ring, 100);
        if (poll_result < 0 && poll_result != -EINTR)
        {
            fprintf(stderr, "Ring-buffer poll failed: %d\n", poll_result);
            goto cleanup;
        }
    }
    result = EXIT_SUCCESS;

cleanup:
    restore_workers(&controller);
    ring_buffer__free(ring);
    bpf_link__destroy(link);
    bpf_object__close(object);
    if (controller.log != stdout)
        fclose(controller.log);
    return result;
}
