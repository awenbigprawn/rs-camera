#!/usr/bin/env python3
"""Export paper metrics from accepted runs, without overwriting source data.

Use --archive for the paper metadata package with experiment-named directories.
Without --archive, --input is a reproduce.py output directory.
"""
from analysis_common import *

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--repo',type=Path,default=HERE.parent)
    ap.add_argument('--input',type=Path,required=True)
    ap.add_argument('--archive',action='store_true')
    ap.add_argument('--smoke',action='store_true',help='Analyze one repetition per cell; never paper-performance evidence')
    ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--sections',nargs='+',choices=['startup','ablation','e1','e2','e3','e4','overhead','diagnosis'],default=['startup','ablation','e1','e2','e3','e4','overhead','diagnosis'])
    args=ap.parse_args(); repo=args.repo.resolve(); source=args.input.resolve(); out=args.output.resolve()
    if out.exists() and any(out.iterdir()): raise ValueError('Use a new, empty output directory')
    out.mkdir(parents=True,exist_ok=True)
    tool=repo/'tools/realsense_steady_bench'
    sys.path.insert(0,str(tool))
    helper=module('paper_helpers',HERE/'paper_metrics.py')
    helper.REPETITIONS = 1 if args.smoke else 3
    artifacts=set()
    if args.archive:
        results=source/'results' if (source/'results').is_dir() else source
        roots={'e1':results/'e1_worker_timing', 'e2':results/'e2_uvc_pool',
               'e3':results/'e3_kernel_path_protection',
               'e4-representative':results/'e4_scheduling_30fps',
               'e4-stress':results/'e4_scheduling_60fps',
               'overhead':results/'supp_instrumentation_overhead',
               'startup':results/'e1_startup_worker_map/standard',
               'ablation':results/'e1_stream_ablation',
               'diagnosis':results/'e2_incomplete_frame_diagnostics'}
    else: roots={key:source/key for key in ['startup','ablation','e1','e2','e3','e4-representative','e4-stress','overhead','diagnosis']}

    groups = {
        'e1_workers': ['startup', 'ablation', 'e1'],
        'e2_uvc_pool': ['diagnosis', 'e2'],
        'e3_kernel_path': ['e3'],
        'e4_scheduling': ['e4', 'overhead'],
    }
    for directory, sections in groups.items():
        selected = set(sections) & set(args.sections)
        if not selected: continue
        destination = out / directory
        destination.mkdir()
        analyzer = module(directory + '_analysis', HERE / directory / 'analyze.py')
        analyzer.analyze(repo, roots, destination, artifacts, helper, args.archive, selected)
        if directory.split('_')[0] in selected:
            plotter = module(directory + '_plot', HERE / directory / 'plot.py')
            options = {'repetitions': helper.REPETITIONS} if directory == 'e4_scheduling' else {}
            plotter.plot(destination, repo, **options)
            if args.smoke:
                for figure in destination.glob('*.tex'):
                    figure.write_text(r'\textbf{SMOKE TEST --- one repetition; not paper results}\par\medskip'+'\n'+figure.read_text())
    (out/'manifest.json').write_text(json.dumps({'sections':args.sections,'archive':args.archive,'smoke':args.smoke,
        'latency_source':'existing per-run summaries; no silent raw-trace reanalysis',
        'source_files':[{'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted(artifacts)]},indent=2)+'\n')
    print(f'Exported {args.sections} to {out}')

if __name__=='__main__': main()
