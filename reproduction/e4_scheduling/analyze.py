"""Experiment analysis; reuse the benchmark parsers and acceptance checks."""
from analysis_common import *

def analyze(repo, roots, out, artifacts, helper, archive, sections):
    repetitions = getattr(helper, "REPETITIONS", 3)
    tool=repo/"tools/realsense_steady_bench"
    if 'e4' in sections:
        prep=module('paper_latency',HERE/'e4_scheduling/metrics.py')
        # Native parser checks success, phase, policy, noise, topology, and coverage.
        data=[]; grouped=defaultdict(list)
        for key in ('e4-representative','e4-stress'):
            root=roots[key]
            for done in sorted(root.glob('*/formal/round-*/*/noise-*/policy-*/DONE')):
                row,paths=prep.parse_run(done,root); artifacts.update(paths); artifacts.add(done)
                expected_phase = 'rt-threaded' if row['kernel']=='rt' else ('standard-hardirq' if row['policy']=='OTHER' else 'standard-threaded')
                if row['phase'] != expected_phase: raise ValueError(f'Unexpected E4 phase: {row}')
                data.append(row); grouped[tuple(row[k] for k in ['kernel','workload','noise','policy'])].append(row)
        if len(data)!=64*repetitions or len(grouped)!=64 or any(len(v)!=repetitions for v in grouped.values()):
            raise ValueError(f'Expected {64*repetitions} E4 runs, 64 cells, {repetitions} repetitions each')
        summary=[]
        for key,group in sorted(grouped.items()):
            matched=sum(r['matched_deliveries'] for r in group); total=sum(r['total_deliveries'] for r in group)
            summary.append({**dict(zip(['kernel','workload','noise','policy'],key)),'runs':repetitions,
                'p99_ms':statistics.mean(r['p99_ms'] for r in group),'max_ms':max(r['max_ms'] for r in group),
                'coverage':matched/total,'matched_deliveries':matched,'total_deliveries':total,
                **{k:sum(r[k] for r in group) for k in ['problem_framesets','duplicate_components','sequence_gaps']}})
        write(out/'main_matrix_host_latency_runs.csv',data);write(out/'main_matrix_host_latency_summary.csv',summary)
    if 'overhead' in sections:
        data=[]; grouped=defaultdict(list)
        for p in sorted(roots['overhead'].glob('*/*/*/resource_summary.json')):
            r=read(p); agg=r['aggregate']; events=p.parent/'frame_events.csv';artifacts.update([p,events])
            if not r.get('probe_success') or r['returncode'] != 0:
                raise ValueError(f'Unsuccessful overhead attempt: {p}')
            row={'workload':r['workload'],'mode':r['mode'],'rep':r['rep'],
                 'cpu_percent':r['cpu_utilization_percent_of_one_core'],'rss_max_mib':r['rss_max_kib']/1024,
                 **{f'interarrival_{k}_ms':agg['delivery_interarrival_ms'][k] for k in ['mean','p99','max']},
                 'trace_mib':r['trace_final_allocated_bytes']/1024**2,
                 **helper.frameset_latency_metrics(events),
                 **{k:agg[k] for k in ['duplicate_frames','sequence_gaps','timeouts']}}
            data.append(row);grouped[row['workload'],row['mode']].append(row)
        if len(data)!=18 or len(grouped)!=6 or any(len(g)!=3 for g in grouped.values()):
            raise ValueError('Expected 18 overhead runs in six cells')
        summary=[]
        for (workload,mode),group in sorted(grouped.items()):
            summary.append({'workload':workload,'mode':mode,'runs':repetitions,
                **{k:statistics.median(r[k] for r in group) for k in group[0] if k not in ['workload','mode','rep']}})
        write(out/'overhead_runs.csv',data);write(out/'overhead_summary.csv',summary)
