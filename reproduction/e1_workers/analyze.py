"""Experiment analysis; reuse the benchmark parsers and acceptance checks."""
from analysis_common import *

def analyze(repo, roots, out, artifacts, helper, archive, sections):
    tool=repo/"tools/realsense_steady_bench"
    if 'startup' in sections:
        builder=module('startup_builder',repo/'tools/realsense_startup_bench/build_startup_model.py')
        campaign_csv=one(roots['startup'].glob('benchmark_*.csv'))
        campaign=campaign_csv.with_suffix(''); artifacts.add(campaign_csv)
        meta=builder.build_model(campaign,out/'startup',policy='other',cycle=1,min_periods=6,periodic_presence=0.8)
        artifacts.update(campaign.glob('policy-other/**/thread_timing.csv'))
        artifacts.update(campaign.glob('policy-other/**/thread_intervals.csv'))
        if int(meta['logical_runs_recorded']) != 20: raise ValueError('Expected 20 OTHER startup runs')

    if 'ablation' in sections:
        data=[]
        for case in ['model_ablation_d435_n1_depth60','model_ablation_d435_n1_depth_color60']:
            cell=roots['ablation']/'standard'/case
            marker=cell/'selected-attempt.txt'; artifacts.add(marker)
            selected=cell/('campaign-attempt-'+marker.read_text().strip())/'results'
            logical=sorted(selected.glob('**/run-*/selected_attempt.txt'))
            if len(logical)!=3: raise ValueError('Expected three selected stream-ablation runs')
            for marker in logical:
                p=attempt(marker.parent); summary=p/'thread_steady_summary.json';artifacts.add(summary)
                value=read(summary)
                for thread in value['threads']:
                    data.append({'case':case,'source':str(p),'signature':thread['signature'],
                        'tid':thread['tid'],'name':thread['name'],'running_ms':thread['running_ms'],
                        'period_median_ms':thread['period_ms'].get('p50'),
                        'execution_max_ms':thread['execution_ms'].get('max')})
        write(out/'ablation_threads.csv',data)

    if 'e1' in sections:
        generator=module('generate_profile',tool/'generate_deadline_profile.py')
        from parse_startup_trace import thread_signature
        table=[]; instances=[]
        for camera in ('d435','d455'):
            for workload in ('representative30','stress60'):
                paths=[attempt(roots['e1']/camera/workload/f'run-{n}') for n in (1,2,3)]
                for p in paths:
                    status=p/'steady_summary.json'; artifacts.add(status)
                    if not read(status).get('success'): raise ValueError(f'Unsuccessful E1 attempt: {p}')
                meta=generator.generate_profile(paths,out/'profiles'/f'{camera}_{workload}.csv',1.20,0.91,100,4194304)
                names={}; usage=defaultdict(list)
                for p in paths:
                    life=p/'thread_lifecycle.jsonl'; steady=p/'thread_steady_summary.json'
                    artifacts.update([life,steady,p/'thread_steady_activations.csv'])
                    for line in life.open():
                        event=json.loads(line)
                        if event.get('event')=='pthread_create':
                            sig=thread_signature(event)
                            symbols=' '.join(f.get('symbol','') for f in event.get('stack',[]))
                            names[sig]=symbols
                    summary=read(steady); totals=defaultdict(float)
                    for t in summary['threads']:
                        if t['name']=='main': continue
                        totals[t['signature']]+=float(t['running_ms'])/summary['duration_ms']*100
                    for sig,value in totals.items(): usage[sig].append(value)
                seen=set()
                for t in meta['threads']:
                    sig=t['signature']; label=role(names.get(sig,''),sig)
                    instances.append({'camera':camera,'workload':workload,'family':label,**{k:t[k] for k in
                        ['signature','instance','observed_execution_max_ns','observed_logical_period_min_ns']}})
                    if sig in seen: continue
                    seen.add(sig)
                    if len(usage[sig])!=3: raise ValueError('Worker missing from a repetition')
                    table.append({'camera':camera,'workload':workload,'family':label,'signature':sig,
                        'instances':t['role_instance_count'],
                        'execution_max_us':t['role_observed_execution_max_ns']/1000,
                        'interval_min_ms':t['role_observed_logical_period_min_ns']/1e6,
                        'utilization_percent':statistics.median(usage[sig])})
        write(out/'e1_families.csv',table); write(out/'e1_instances.csv',instances)
