"""Experiment analysis; reuse the benchmark parsers and acceptance checks."""
from analysis_common import *

def analyze(repo, roots, out, artifacts, helper, archive, sections):
    tool=repo/"tools/realsense_steady_bench"
    if 'e3' in sections:
        root=roots['e3']; selected=[]
        if archive:
            path=root/'h1_replacement_runs.csv'; artifacts.add(path)
            for r in rows(path): selected.append((r['camera'],r['treatment'],one((root/r['selected_path']).rglob('frame_events.csv')).parent))
        else:
            for marker in sorted(root.glob('camera-*/treatment-*/rep-*/SELECTED')):
                camera,treatment,_rep,_=marker.relative_to(root).parts
                artifacts.add(marker)
                selected.append((camera.removeprefix('camera-'),treatment.removeprefix('treatment-'),
                                 one(Path(marker.read_text().strip()).rglob('frame_events.csv')).parent))
        if len(selected)!=18: raise ValueError(f'Expected 18 selected E3 runs, found {len(selected)}')
        data=[]; grouped=defaultdict(list)
        for camera,treatment,p in selected:
            events=p/'frame_events.csv'; latency=p/'full_receive_path_summary.json'; artifacts.update([events,latency])
            status=p/'steady_summary.json'; artifacts.add(status)
            if not read(status).get('success'): raise ValueError(f'Unsuccessful E3 attempt: {p}')
            lat=read(latency)['host_receive_to_frameset_latency']
            r={'camera':camera,'treatment':treatment,'source':str(p),**helper.frameset_freshness_metrics(events),
               'p99_ms':lat['p99_us']/1000,'max_ms':lat['max_us']/1000,'latency_coverage':lat['coverage']}
            data.append(r); grouped[camera,treatment].append(r)
        summary=[]
        for (camera,treatment),group in sorted(grouped.items()):
            if len(group)!=3: raise ValueError('E3 cell must have three accepted repetitions')
            totals={k:sum(r[k] for r in group) for k in ['deliveries','stale_framesets','gap_bearing_framesets','nonfresh_framesets','duplicate_components','sequence_gaps']}
            summary.append({'camera':camera,'treatment':treatment,'runs':3,**totals,
                'nonfresh_percent':100*totals['nonfresh_framesets']/totals['deliveries'],
                'p99_ms':statistics.median(r['p99_ms'] for r in group),'max_ms':max(r['max_ms'] for r in group)})
        if len(summary)!=6: raise ValueError('Expected six E3 cells')
        write(out/'e3_runs.csv',data); write(out/'e3_summary.csv',summary)
