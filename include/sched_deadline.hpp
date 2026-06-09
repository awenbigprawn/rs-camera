#ifndef SCHED_DEADLINE_HPP
#define SCHED_DEADLINE_HPP

#include <cstdint>
#include <cstring>
#include <unistd.h>
#include <sys/syscall.h>
#include <linux/sched.h>
#include <iostream>
#include <pthread.h>
#include <chrono>
#include <cerrno>
#include <string>

#ifndef SCHED_DEADLINE
#define SCHED_DEADLINE 6
#endif

#ifndef SCHED_FLAG_RESET_ON_FORK
#define SCHED_FLAG_RESET_ON_FORK (1ULL << 0)
#endif

#ifndef SCHED_FLAG_DL_INHERIT_ON_FORK
#define SCHED_FLAG_DL_INHERIT_ON_FORK (1ULL << 10)  // match kernel bit
#endif

#ifndef SCHED_FLAG_DL_BUDGET_SKIP_ADMISSION
#define SCHED_FLAG_DL_BUDGET_SKIP_ADMISSION (1ULL << 11) // match kernel bit
#endif

#ifndef __NR_sched_setattr
#define __NR_sched_setattr 314   /* x86_64; adjust if needed */
#endif

#ifndef __NR_sched_getattr
#define __NR_sched_getattr 315   /* x86_64; adjust if needed */
#endif

#ifndef __NR_sched_create_dl_budget
#define __NR_sched_create_dl_budget 463  /* def in linux/arch/x86/entry/syscalls/syscall_64.tbl */
#endif

#ifndef __NR_sched_leave_dl_budget
#define __NR_sched_leave_dl_budget 464  /* def in linux/arch/x86/entry/syscalls/syscall_64.tbl */
#endif

#ifndef SCHED_ATTR_DEFINED
#define SCHED_ATTR_DEFINED
struct sched_attr {
    uint32_t size;
    uint32_t sched_policy;
    uint64_t sched_flags;
    int32_t sched_nice;
    uint32_t sched_priority;
    uint64_t sched_runtime;
    uint64_t sched_deadline;
    uint64_t sched_period;
    uint32_t sched_util_min;
    uint32_t sched_util_max;
};
#endif

struct sched_dl_budget_attr {
    uint32_t flags;      /* preserve as 0 */
    uint64_t runtime;    /* ns */
    uint64_t period;     /* ns */
    uint64_t deadline;   /* ns */
};

inline bool has_sched_create_dl_budget_syscall() {
    // Cache probe result: -1 unknown, 0 unsupported, 1 supported
    static int cached = -1;
    if (cached != -1) {
        return cached == 1;
    }

    errno = 0;
    // Probe with nullptr; supported kernels should return EFAULT/EINVAL, not ENOSYS.
    long probe_ret = syscall(__NR_sched_create_dl_budget, nullptr);
    if (probe_ret < 0 && errno == ENOSYS) {
        cached = 0;
        return false;
    }

    cached = 1;
    return true;
}

inline sched_attr set_sched_deadline(pid_t tid, uint64_t runtime,
    uint64_t deadline, uint64_t period,
    bool inherit_on_fork = true,
    bool skip_admission = false) {
    sched_attr attr;
    std::memset(&attr, 0, sizeof(attr));
    attr.size = sizeof(attr);
    attr.sched_policy = SCHED_DEADLINE;
    if (inherit_on_fork) {
        attr.sched_flags |= SCHED_FLAG_DL_INHERIT_ON_FORK;
        if (skip_admission) {
            attr.sched_flags |= SCHED_FLAG_DL_BUDGET_SKIP_ADMISSION;
        }
    } else {
        attr.sched_flags |= SCHED_FLAG_RESET_ON_FORK;
    }
    attr.sched_runtime = runtime;
    attr.sched_deadline = deadline;
    attr.sched_period = period;

    long ret = syscall(__NR_sched_setattr, tid, &attr, 0);
    if (ret < 0 && errno == EINVAL && attr.sched_flags != 0) {
        // Some kernels reject unknown sched_flags bits; retry with plain SCHED_DEADLINE.
        attr.sched_flags = 0;
        ret = syscall(__NR_sched_setattr, tid, &attr, 0);
    }

    if (ret < 0) {
        int err = errno;
        std::cerr << "sched_setattr failed (errno=" << err << ": " << tid << ": " << std::strerror(err) << ")\n";
        // Mark as not-applied so caller-side checks can detect failure.
        attr.sched_policy = 0;
    }

    return attr;
}

inline sched_attr get_sched_attr(pid_t tid = 0) {
    sched_attr attr;
    std::memset(&attr, 0, sizeof(attr));
    attr.size = sizeof(attr);

    long ret = syscall(__NR_sched_getattr, tid, &attr, sizeof(attr), 0);
    if (ret < 0) {
        int err = errno;
        std::cerr << "sched_getattr failed (errno=" << err << ": " << tid << ": "
                  << std::strerror(err) << ")\n";
        attr.sched_policy = 0;
    }

    return attr;
}

inline int create_dl_budget(uint64_t runtime_ns, uint64_t deadline_ns, uint64_t period_ns) {
    if (!has_sched_create_dl_budget_syscall()) {
        return -ENOSYS;
    }

    sched_dl_budget_attr battr;
    std::memset(&battr, 0, sizeof(battr));
    battr.flags    = 0;
    battr.runtime  = runtime_ns;
    battr.deadline = deadline_ns;
    battr.period   = period_ns;

    int ret = static_cast<int>(syscall(__NR_sched_create_dl_budget, &battr));
    if (ret < 0) {
        int err = errno;
        perror(("sched_create_dl_budget failed: " + std::string(std::strerror(err))).c_str());
        return -err;
    }
    return ret;
}

inline int leave_dl_budget() {
    int ret = static_cast<int>(syscall(__NR_sched_leave_dl_budget));
    if (ret < 0) {
        int err = errno;
        perror(("sched_leave_dl_budget failed: " + std::string(std::strerror(err))).c_str());
        return -err;
    }
    return ret;
}

// Reusable function to configure a thread with SCHED_DEADLINE
inline sched_attr configure_sched_deadline(
    uint64_t runtime_ns,
    uint64_t deadline_ns,
    uint64_t period_ns,
    int thread_id,
    bool inherit_on_fork = true
) {
    sched_attr attr = set_sched_deadline(0,runtime_ns, deadline_ns, period_ns, inherit_on_fork);
    if (attr.sched_policy != SCHED_DEADLINE) {
        std::cerr << "Failed to set SCHED_DEADLINE for thread " << thread_id << std::endl;
    }
    return attr;
}

inline void sched_wait_for_next_loop(const sched_attr &attr, std::chrono::steady_clock::time_point loop_start)
{

    const auto elapsed_time = std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::steady_clock::now() - loop_start).count();
    if (auto elapsed_time_uint = static_cast<uint64_t>(elapsed_time);
        elapsed_time_uint > attr.sched_period) {
        return;
    }
    if (uint64_t remaining_time = attr.sched_period - elapsed_time; remaining_time > 0) {
        usleep(remaining_time / 1000);
    }
}

// Try to query current policy via kernel (works for SCHED_DEADLINE), fallback to pthreads
inline int query_current_policy_kernel() {
    sched_attr attr{};
    std::memset(&attr, 0, sizeof(attr));
    attr.size = sizeof(attr);
    // int sched_getattr(pid_t pid, struct sched_attr *attr, unsigned int size, unsigned int flags);
    int ret = syscall(SYS_sched_getattr, 0, &attr, sizeof(attr), 0);
    if (ret == 0) return static_cast<int>(attr.sched_policy);
    return -1;
}

inline void check_sched_policy(std::string thread_name = "") {
    int policy = -1;

    // Prefer kernel syscall to reliably detect SCHED_DEADLINE
    policy = query_current_policy_kernel();

    // Fallback to pthread if kernel query failed
    if (policy < 0) {
        struct sched_param param{};
        if (pthread_getschedparam(pthread_self(), &policy, &param) != 0) {
            perror("pthread_getschedparam failed");
            policy = -1;
        }
    }

    std::string policy_str;
    switch (policy) {
        case SCHED_OTHER: policy_str = "SCHED_OTHER"; break;
        case SCHED_FIFO:  policy_str = "SCHED_FIFO";  break;
        case SCHED_RR:    policy_str = "SCHED_RR";    break;
#ifdef SCHED_BATCH
        case SCHED_BATCH: policy_str = "SCHED_BATCH"; break;
#endif
#ifdef SCHED_IDLE
        case SCHED_IDLE:  policy_str = "SCHED_IDLE";  break;
#endif
        case SCHED_DEADLINE: policy_str = "SCHED_DEADLINE"; break;
        default:
            policy_str = "Unknown(" + std::to_string(policy) + ")";
            break;
    }

    std::string policy_msg = thread_name + " scheduling policy: " + policy_str + "\n";
    std::cout << policy_msg;
    std::cout.flush();
}

#endif // SCHED_DEADLINE_HPP
