# Scheduling-Policy Validation of the Steady-State Time Model

## Question

The steady-state worker model was reconstructed under `SCHED_OTHER`. This
experiment tests whether the same model describes role-aware `SCHED_RR`,
`SCHED_FIFO`, and `SCHED_DEADLINE` executions.

"The same model" means that:

1. the normalized creator-stack signatures and instance multiplicities are
   unchanged; and
2. identities that have a stable principal logical-job period under OTHER
   retain that period under the modeled policies.

The claim does not require equal execution time, ready delay, or response
time. Those quantities can change with preemption, cache state, and scheduling
interference.

## Experimental Controls

- Platform: Raspberry Pi 5 at a fixed 1500 MHz CPU frequency.
- Kernel: Linux `6.12.96-rpi5-rt-btf-uvc16+` with PREEMPT_RT and 16 UVC URBs.
- Backend: V4L2 plus `uvcvideo`.
- xHCI IRQ predecessors: `SCHED_FIFO`, priority 90, original affinities.
- Probe CPU placement: no affinity, taskset, cgroup partition, or isolation.
- Duration: 30 s after a 30-frame warm-up.
- Repetitions: three per cell.
- Policies: OTHER, RR with rate-monotonic priorities, FIFO with
  rate-monotonic priorities, and Deadline.
- Cases: one D435 and one D455 under representative 30-FPS and common-stress
  60-FPS workloads, plus two D455 cameras under the representative workload.

Each workload-specific scheduler profile was generated from the three
contemporaneous OTHER traces after the xHCI IRQ policy had been fixed. The
profile generator merged activation bursts into logical jobs. It used 1.20
times the maximum observed logical-job execution as the Deadline runtime and
0.91 times the minimum observed stable logical period as both deadline and
period. These are empirical margins, not worst-case bounds.

## Validation Method

A worker identity is the pair `(normalized creator signature, instance)`. The
structural check compares the full identity set in every run. The temporal
check uses the same burst reconstruction as the scheduler-profile generator,
but compares the median, or principal, logical period rather than the minimum
period used to make a conservative reservation.

An identity is classified as stable only when all three OTHER principal-period
estimates are within 5% of their median. The policy observations are compared
with that median using the same declared 5% descriptive tolerance.
Event-driven and self-suspending identities remain part of the structural
check but do not require a periodic model.

## Results

All 20 cells and all 60 runs completed successfully. Every run contained the
same worker identity set as its corresponding OTHER baseline. The table
reports the largest absolute principal-period deviation observed across all
three repetitions and all stable identities.

| Camera and workload | Stable identities | RR-RM max. delta | FIFO-RM max. delta | Deadline max. delta | Deadline duplicate / partial-stale framesets |
|---|---:|---:|---:|---:|---:|
| D435, common stress 60 FPS | 11 | 0.144% | 0.145% | 0.141% | 0 / 0 |
| D435, representative 30 FPS | 11 | 0.106% | 0.104% | 0.108% | 4 / 4 |
| D455, common stress 60 FPS | 12 | 0.143% | 0.141% | 0.141% | 0 / 0 |
| D455, representative 30 FPS | 12 | 0.104% | 0.103% | 3.293% | 2 / 2 |
| Two D455, representative 30 FPS | 22 | 3.172% | 3.172% | 3.172% | 0 / 0 |

The median thread-level ready-delay p99 was 0.021--0.066 ms under OTHER and
0.004--0.005 ms under the modeled policies. Execution-time p99 also changed,
as expected, but the worker structure and principal periods stayed within the
declared tolerance.

The generated Deadline reservations were:

| Case | Profile entries | Reserved CPU utilization |
|---|---:|---:|
| D435, common stress 60 FPS | 11 | 0.335 cores |
| D435, representative 30 FPS | 11 | 0.100 cores |
| D455, common stress 60 FPS | 12 | 0.302 cores |
| D455, representative 30 FPS | 12 | 0.099 cores |
| Two D455, representative 30 FPS | 23 | 0.305 cores |

Three Deadline runs returned non-advancing stream frame numbers. D435 runs 1
and 2 and D455 run 3 each contained one repeated color frame and one repeated
depth frame. These events produced six partially stale framesets in total,
without a sequence gap or timeout. This finding does not invalidate the
activation model, but it shows that model preservation and frame freshness
are separate properties.

## Conclusion and Scope

Within the tested camera counts, workloads, and 30-s windows, the
`SCHED_OTHER` worker-family and principal-period model also describes
RR-RM, FIFO-RM, and Deadline execution. The scheduler changes service delay and
execution distributions rather than the workload's source-level worker graph
or principal release pattern. The result is an empirical model validation; it
is not a WCET measurement or a schedulability guarantee.

Machine-readable summaries are stored under
`results/rpi5/policy_time_model_validation_20260818/analysis/` in
`policy_time_model.csv` and `policy_time_model_identities.csv`.
