#!/usr/bin/env python3
"""Prepare and run the existing RTNS campaigns on an already configured Pi 5.

No boot files or services are changed. Switch kernels explicitly between stages.
The generated adapters retain the experiment commands from the original scripts.
"""
from __future__ import annotations
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
DEFAULT_REPO = HERE.parent
STAGES = {
    'startup': ('standard-hardirq16', 20, 'Lifecycle discovery; OTHER'),
    'ablation': ('standard-threaded16', 6, 'Depth / Depth+Color worker attribution'),
    'e1': ('rt-threaded16', 12, 'D435/D455 x R/S x 3; full-path traces'),
    'e2-calibrate': ('rt-threaded16', 2, 'Single-D435 scheduling calibration'),
    'e2-urb5': ('standard-hardirq5', 6, 'UVC pool=5; R/S x 3'),
    'e2-urb16': ('standard-hardirq16', 6, 'UVC pool=16; R/S x 3'),
    'e3': ('standard-threaded16', 18, 'D435/D455 x 3 kernel treatments x 3'),
    'e4-calibrate': ('standard-threaded16', 6, 'Two-D435 scheduling calibration'),
    'e4-standard-hardirq': ('standard-hardirq16', 24, 'E4 OTHER; R/S x 4 noise cells x 3'),
    'e4-standard-threaded': ('standard-threaded16', 72, 'E4 RR/FIFO/Deadline; R/S x 4 x 3'),
    'e4-rt-threaded': ('rt-threaded16', 96, 'E4 four policies; R/S x 4 x 3'),
    'overhead': ('standard-hardirq16', 18, 'Supplement: R/S x off/build-only/full x 3'),
    'diagnosis': ('standard-hardirq16', 1, '600-s receive-path diagnosis; events are stochastic'),
}
KERNELS = {
    'standard-hardirq5': ('6.12.96-rpi5-standard-btf+', False),
    'standard-hardirq16': ('6.12.96-rpi5-standard-btf-uvc16+', False),
    'standard-threaded16': ('6.12.96-rpi5-standard-btf-uvc16+', True),
    'rt-threaded16': ('6.12.96-rpi5-rt-btf-uvc16+', True),
}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError(f'Source changed: expected one occurrence of {old!r}')
    return text.replace(old, new, 1)


def before(text, marker):
    if text.count(marker) != 1:
        raise ValueError(f'Source changed: expected unique boundary {marker!r}')
    return text.split(marker)[0]


def q(value):
    return shlex.quote(str(value))


def load_config(path):
    cfg = json.loads(path.read_text())
    cameras = cfg['cameras']
    selection = cfg['selection']
    serials = [camera['serial'] for camera in cameras.values()]
    if len(set(serials)) != len(serials):
        raise ValueError('Camera SDK serials must be distinct')
    if not all(re.fullmatch(r'[0-9]+', s) for s in serials):
        raise ValueError('Replace serial placeholders with the actual numeric serials')
    for camera in cameras.values():
        if not isinstance(camera['xhci_irq'], int) or camera['xhci_irq'] <= 0:
            raise ValueError('Set the E3 IRQ numbers from this Pi, not from the paper')
        if not re.fullmatch(r'xhci-hcd:usb[0-9]+', camera['xhci_label']):
            raise ValueError('Set the matching E3 xHCI IRQ labels')
    pair = selection['d435_pair']
    if len(pair) != 2 or len(set(pair)) != 2:
        raise ValueError('Select two different D435 cameras for E4')
    for name in pair + [selection['single_d435'], selection['startup_d435']]:
        if cameras[name]['model'] != 'D435': raise ValueError(f'{name} is not a D435')
    d455 = cameras[selection['single_d455']]
    if d455['model'] not in ('D455', 'D455F'): raise ValueError('Select a D455/D455F')
    cfg['d435_serials'] = [cameras[name]['serial'] for name in pair]
    cfg['single_d435_serial'] = cameras[selection['single_d435']]['serial']
    cfg['startup_serial'] = cameras[selection['startup_d435']]['serial']
    cfg['d455_serial'] = d455['serial']
    cfg['e3_irqs'] = {
        model: {'number': cameras[selection[role]]['xhci_irq'],
                'label': cameras[selection[role]]['xhci_label']}
        for model, role in [('d435', 'single_d435'), ('d455', 'single_d455')]
    }
    return cfg


def camera_identity(cfg):
    """Compare physical selections while allowing boot-dependent IRQ numbering."""
    identity = json.loads(json.dumps(cfg))
    identity.pop('e3_irqs', None)
    for camera in identity['cameras'].values():
        camera.pop('xhci_irq', None)
        camera.pop('xhci_label', None)
    return identity


def prepare(repo, root, cfg, *, allow_irq_refresh=False):
    """Generate inspectable adapters; do not execute a campaign or patch the repo."""
    tool = repo / 'tools/realsense_steady_bench'
    if (root / 'prepared.json').exists():
        prior = json.loads((root / 'prepared.json').read_text())
        same = prior['config'] == cfg
        if allow_irq_refresh:
            same = camera_identity(prior['config']) == camera_identity(cfg)
        if not same or prior['repo'] != str(repo):
            raise ValueError('Use a new output directory for a different configuration/repository')
    entry = root / 'entrypoints'
    entry.mkdir(parents=True, exist_ok=True)
    sources = {}
    single_d435 = cfg.get('single_d435_serial', cfg['d435_serials'][1])
    startup_serial = cfg.get('startup_serial', cfg['d435_serials'][0])

    def assignment(text, name, value):
        result, count = re.subn(r'(?m)^(\s*)' + re.escape(name) + r'=[^\n]*$',
                                lambda match: match[1] + name + '=' + q(value), text)
        if count != 1: raise ValueError(f'Expected one assignment for {name}, found {count}')
        return result

    def read(name):
        path = tool / name
        sources[str(path.relative_to(repo))] = digest(path)
        text = path.read_text()
        text = text.replace('repo=/home/safebot/program/rs-camera', 'repo=' + q(repo))
        text = text.replace('/home/safebot/.local/state/', str(root / 'state') + '/')
        return text

    def write(name, text):
        path = entry / name
        path.write_text(text)
        path.chmod(0o755)
        if path.suffix == '.sh':
            subprocess.run(['sh', '-n', str(path)], check=True)
        return path

    # Explicit serial lists; strip unused two-D455 cases from the E4 config.
    configs = {}
    for name in ('.d1_h1_single_d435_20260819.json', '.h1_single_d455_20260819.json',
                 'p1_v2_cases_20260820.json', '.model_stream_ablation_cases_20260812.json'):
        data = json.loads(read(name))
        if name.startswith('p1_'):
            data['cases'] = [c for c in data['cases'] if 'd435' in c['case_id']]
            serials = cfg['d435_serials']
        elif 'ablation' in name:
            serials = [startup_serial]
        elif 'd455' in name:
            serials = [cfg['d455_serial']]
        else:
            serials = [single_d435]
        for case in data['cases']:
            case['probe']['serials'] = serials
        if 'notes' in data:
            data['notes'] = ['Camera selection is generated from config.json.']
        dest = write(name.lstrip('.'), json.dumps(data, indent=2) + '\n')
        configs[name] = dest

    def config_paths(text):
        for name, path in configs.items():
            text = text.replace(f'"$tool_dir/{name}"', q(path))
        return text

    # C1 already has its own twelve-run capture/parse/selection implementation.
    text = read('.c1_full_receive_path_20260819.sh')
    text = assignment(text, 'd435_serial', single_d435)
    text = assignment(text, 'd455_serial', cfg['d455_serial'])
    text = replace_once(text, 'repo=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)',
                        'repo=' + q(repo))
    text = replace_once(text, 'output=${1:-$repo/tools/realsense_steady_bench/results/c1_full_receive_path_20260819}',
                        'output=' + q(root / 'e1'))
    # An analyzer failure must not be marked as a successful reproduced run.
    text = replace_once(text, 'python3 "$path_analyzer" "$attempt_dir" || true',
                        'python3 "$path_analyzer" "$attempt_dir"')
    write('e1.sh', text)

    text = config_paths(read('.d1_h1_single_d435_20260819.sh'))
    text = assignment(text, 'serial', single_d435)
    text = before(text, 'state=$(cat "$state_file" 2>/dev/null || printf \'%s\' prepare)')
    text = replace_once(text, 'result_root="$tool_dir/results/d1_h1_single_d435_20260819"',
                        'result_root=' + q(root / 'e2'))
    for stage, suffix in {
        'e2-calibrate': 'calibrate_profiles\n',
        'e2-urb5': 'run_cell d1-uvc5-standard-hardirq 5 hardirq\n',
        'e2-urb16': 'run_cell d1-h1-uvc16-standard-hardirq 16 hardirq\n',
    }.items():
        write(stage + '.sh', text + '\nsudo -n true\n' + suffix)

    one = config_paths(read('.run_h1_replacement_one_20260819.sh'))
    one = one.replace('"$tool_dir/results/d1_h1_single_d435_20260819/profiles/stress.csv"',
                      q(root / 'e2/profiles/stress.csv'))
    one = one.replace('"$tool_dir/results/h1_replacement_profiles/d455_stress60_fullpath.csv"',
                      q(root / 'e1/profiles/d455_stress60.csv'))
    for model in ('d435', 'd455'):
        irq = cfg['e3_irqs'][model]
        pattern = rf'(?ms)(^    {model}\)\n)(.*?)(^        ;;\n)'
        def configure_camera(match):
            block = assignment(match[2], 'serial', single_d435 if model == 'd435' else cfg['d455_serial'])
            block = assignment(block, 'xhci_irq', irq['number'])
            block = assignment(block, 'xhci_label', irq['label'])
            return match[1] + block + match[3]
        one, count = re.subn(pattern, configure_camera, one)
        if count != 1: raise ValueError(f'Expected one {model} case in E3 adapter')
    one = replace_once(one, 'sudo -n true\n', 'sudo -n true\n"$tool_dir/build_uvc_worker_fifo_monitor.sh" "$monitor_build"\n')
    write('e3-one.sh', one)
    text = read('.run_h1_replacement_matrix_20260819.sh')
    text = replace_once(text, 'one="$tool_dir/.run_h1_replacement_one_20260819.sh"',
                        'one=' + q(entry / 'e3-one.sh'))
    text = replace_once(text, 'root="$tool_dir/results/h1_replacement_targeted_starvation_20260819"',
                        'root=' + q(root / 'e3'))
    write('e3.sh', text)

    text = config_paths(read('run_p1_v2_calibration_20260820.sh'))
    text = replace_once(text, 'result_root="$tool_dir/results/p1_cross_layer_v2_20260820/calibration"',
                        'result_root=' + q(root / 'e4-calibration'))
    text = replace_once(text, 'profiles="$tool_dir/results/p1_cross_layer_v2_20260820/profiles"',
                        'profiles=' + q(root / 'e4-profiles'))
    text = replace_once(text, 'sudo -n true\n',
                        'sudo -n true\n"$tool_dir/build_uvc_worker_fifo_monitor.sh" "$monitor_build"\n')
    write('e4-calibrate.sh', text)

    text = config_paths(read('run_p1_cross_layer_v2_20260820.sh'))
    text = before(text, 'sudo -n true\nstate=$(cat "$state_file" 2>/dev/null || printf \'%s\' prepare)')
    text = replace_once(text, 'result_root=${RS_P1_RESULT_ROOT:-$tool_dir/results/p1_cross_layer_v2_20260820}',
                        'result_root=' + q(root / 'e4'))
    text = replace_once(text, '    disable_wifi\n    disable_autosuspend', '    disable_autosuspend')
    # Manual phase selection excludes the old boot mutation / automatic reboot state machine.
    for phase, policies, kernel, threading in (
        ('standard-hardirq', 'other', KERNELS['standard-hardirq16'][0], 'no'),
        ('standard-threaded', 'rr-rm fifo-rm deadline', KERNELS['standard-threaded16'][0], 'yes'),
        ('rt-threaded', 'other rr-rm fifo-rm deadline', KERNELS['rt-threaded16'][0], 'yes')):
        suffix = '\nsudo -n true\n'
        for scope in ('representative', 'stress'):
            suffix += '\n'.join([
                'result_root=' + q(root / ('e4-' + scope)),
                'profiles=' + q(root / 'e4-profiles'),
                'p1_trace_mode=host-latency', 'p1_workload_scope=' + scope,
                'mkdir -p "$result_root"',
                f'run_phase {phase} {q(policies)} {kernel} {threading}', ''])
        write('e4-' + phase + '.sh', text + suffix)

    text = config_paths(read('.model_stream_ablation_20260812.sh'))
    text = before(text, 'state=$(cat "$state_file" 2>/dev/null || printf \'%s\' prepare)')
    text = replace_once(text, 'result_root="$tool_dir/results/model_stream_ablation_20260812"',
                        'result_root=' + q(root / 'ablation'))
    text = replace_once(text, '    sudo -n systemctl stop wpa_supplicant.service 2>/dev/null || true\n    sudo -n ip link set wlan0 down 2>/dev/null || true\n', '')
    write('ablation.sh', text + '\nsudo -n true\nrun_kernel standard\n')

    python = repo / '.venv/bin/python'
    startup = [str(python), str(repo / 'tools/realsense_startup_bench/run_startup_campaign.py'),
        '--policies','other','--recover-on-failure','full-reset','--max-attempts-per-run','3',
        '--recovery-settle-seconds','0','--recovery-wait-seconds','1.2','--recovery-reset-timeout-ms','5000',
        '--frame-timeout-ms','1500','--join-timeout-ms','10','--cycles','1','--frames','60','--nb-runs','20',
        '--serial',startup_serial,'--build-dir',str(repo/'build-realsense-thread-trace'),
        '--results-dir',str(root/'startup')]
    overhead = [str(python),str(tool/'run_diagnostic_overhead.py'),'--serial',single_d435,
                '--results-dir',str(root/'overhead'),'--build-jobs','3']
    diagnosis = [str(python),str(tool/'run_steady_campaign.py'),'--config',str(tool/'configs/timing_trace_60s.json'),
        '--case','representative_depth_color_30fps_60s_trace','--policies','other',
        '--freshness-kernel-trace','--measurement-duration-seconds','600','--nb-runs','1',
        '--serial',cfg['d435_serials'][0],'--serial',cfg['d435_serials'][1],
        '--recover-on-failure','full-reset','--max-attempts-per-run','3','--results-dir',str(root/'diagnosis')]
    for name, command in [('startup',startup),('overhead',overhead),('diagnosis',diagnosis)]:
        write(name+'.sh', '#!/bin/sh\nset -eu\ncd '+q(repo)+'\nexec '+shlex.join(command)+'\n')
    manifest = {'repo':str(repo),'config':cfg,'adapter_sources':sources,
                'entrypoints':{p.name:digest(p) for p in sorted(entry.iterdir()) if p.is_file()}}
    (root/'prepared.json').write_text(json.dumps(manifest, indent=2)+'\n')
    return manifest


def check(repo, root, stage, cfg):
    errors = []
    mode = STAGES[stage][0]
    kernel, threaded = KERNELS[mode]
    if platform.machine() not in ('aarch64','arm64'):
        errors.append('Acquisition requires the AArch64 Raspberry Pi 5, not this host')
    model = Path('/proc/device-tree/model')
    if not model.is_file() or 'Raspberry Pi 5' not in model.read_text():
        errors.append('Paper platform is Raspberry Pi 5')
    wifi = Path('/sys/class/net/wlan0/operstate')
    if wifi.exists() and wifi.read_text().strip() == 'up':
        errors.append('Paper runs disable Wi-Fi; use wired/local access and disable it before acquisition')
    if platform.release() != kernel:
        errors.append(f'Boot kernel {kernel}; found {platform.release()}')
    cmdline = Path('/proc/cmdline').read_text()
    if not mode.startswith('rt-') and ('threadirqs' in cmdline.split()) != threaded:
        errors.append(f'{mode}: threadirqs must be {threaded}')
    needed = [repo/'.venv/bin/python', repo/'deps/lime-rtw/target/release/lime-rtw',
              repo/'build-realsense-steady/realsense_steady_probe']
    for path in needed:
        if not path.is_file(): errors.append(f'Missing {path}')
    probe = repo/'build-realsense-steady/d435_sensor_probe'
    if platform.machine() in ('aarch64', 'arm64') and probe.is_file():
        for serial in sorted(set(cfg['d435_serials'] + [cfg['d455_serial'], cfg['single_d435_serial'], cfg['startup_serial']])):
            try:
                result = subprocess.run([str(probe), '--serial', serial, '--device-present'],
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=15)
                if result.returncode:
                    errors.append(f'SDK cannot find camera {serial}; use the librealsense serial, not the USB sysfs serial: {result.stdout.strip()}')
            except (OSError, subprocess.TimeoutExpired) as error:
                errors.append(f'SDK device check failed for {serial}: {error}')
    for executable in ('trace-cmd', 'chrt', 'taskset', 'lsusb', 'gcc', 'g++'):
        if shutil.which(executable) is None: errors.append(f'Missing command: {executable}')
    for compiler in ('gcc', 'g++'):
        if shutil.which(compiler):
            version = subprocess.check_output([compiler, '-dumpfullversion'], text=True).strip()
            if version != '13.3.0': errors.append(f'Paper uses {compiler} 13.3.0; found {version}')
    cache = repo/'build-realsense-steady/CMakeCache.txt'
    if not cache.is_file() or 'CMAKE_BUILD_TYPE:STRING=RelWithDebInfo' not in cache.read_text():
        errors.append('Benchmark must be configured as RelWithDebInfo')
    analyzer = repo/'tools/realsense_steady_bench/analyze_full_receive_path.py'
    if 'latency_s > 0.250' in analyzer.read_text():
        errors.append('Latency analyzer still discards >250 ms; run fix-latency first')
    if stage.startswith('e2-urb'):
        needed_profiles = [root/'e2/profiles'/f'{w}.csv' for w in ('representative','stress')]
    elif stage.startswith('e4-') and stage != 'e4-calibrate':
        needed_profiles = [root/'e4-profiles'/f'{w}.csv' for w in ('representative','stress')]
    elif stage == 'e3':
        needed_profiles = [root/'e2/profiles/stress.csv',root/'e1/profiles/d455_stress60.csv']
    else: needed_profiles = []
    for path in needed_profiles:
        if not path.is_file(): errors.append(f'Missing prerequisite calibration: {path}')
    if stage == 'e3':
        interrupts = Path('/proc/interrupts').read_text()
        for item in cfg['e3_irqs'].values():
            if not re.search(r'^\s*'+str(item['number'])+r':.*'+re.escape(item['label']), interrupts, re.M):
                errors.append(f'IRQ mapping does not exist: {item}')
    return errors


def provenance(repo, root, stage):
    record = {'kernel': platform.release(), 'machine': platform.machine(),
              'cmdline': Path('/proc/cmdline').read_text(), 'stage': stage, 'files': {}}
    for name, command in {
        'commit': ['git','rev-parse','HEAD'],
        'working_tree': ['git','status','--short'],
        'sdk_commit': ['git','-C',str(repo/'deps/librealsense'),'rev-parse','HEAD'],
        'gcc': ['gcc','--version'], 'g++': ['g++','--version'],
        'usb_topology': ['lsusb','-t'],
    }.items():
        result = subprocess.run(command,cwd=repo,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        record[name] = {'returncode':result.returncode,'output':result.stdout}
    for path in sorted((repo/'build-realsense-steady').glob('**/flags.make')):
        if 'realsense' in str(path): record['files'][str(path.relative_to(repo))] = path.read_text()
    source_hashes = {}
    for directory in ('tools','scripts','src'):
        for path in sorted((repo/directory).rglob('*')):
            if path.is_file() and not any(part in {'results','__pycache__','.git'} for part in path.parts):
                if path.suffix in {'.py','.sh','.cpp','.c','.h','.hpp','.json','.txt'}:
                    source_hashes[str(path.relative_to(repo))] = digest(path)
    record['source_sha256'] = source_hashes
    destination = root/'environment'/f'{stage}.json'
    destination.parent.mkdir(parents=True,exist_ok=True)
    destination.write_text(json.dumps(record,indent=2)+'\n')


def patched_analyzer(text):
    """Keep invalid-negative rejection; retain every nonnegative matched latency."""
    return replace_once(text, 'if latency_s < 0.0 or latency_s > 0.250:', 'if latency_s < 0.0:')


def generate_profiles(repo, root):
    for camera in ('d435','d455'):
        for workload in ('representative30','stress60'):
            logical = [root/'e1'/camera/workload/f'run-{n}' for n in (1,2,3)]
            selected = [p / ('attempt-'+(p/'selected_attempt.txt').read_text().strip()) for p in logical]
            output = root/'e1/profiles'/f'{camera}_{workload}.csv'
            output.parent.mkdir(parents=True,exist_ok=True)
            command = [str(repo/'.venv/bin/python'),str(repo/'tools/realsense_steady_bench/generate_deadline_profile.py')]
            for p in selected: command += ['--trace-run',str(p)]
            subprocess.run(command+['--output',str(output)],cwd=repo,check=True)


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--repo',type=Path,default=DEFAULT_REPO)
    ap.add_argument('--output',type=Path,default=HERE/'runs')
    ap.add_argument('--config',type=Path,default=HERE/'setup/config.json')
    ap.add_argument('action',choices=['plan','prepare','check','run','fix-latency','profiles','analyze','bundle','discover'])
    ap.add_argument('stage',nargs='?',choices=STAGES)
    args=ap.parse_args()
    repo=args.repo.resolve(); root=args.output.resolve()
    if args.action=='discover':
        subprocess.run([sys.executable,str(HERE/'setup/discover.py'),'--output',str(args.config),
                        '--inventory',str(args.config.resolve().parent/'inventory.json'),
                        '--probe',str(repo/'build-realsense-steady/d435_sensor_probe')],check=True)
        return
    if args.action=='analyze':
        subprocess.run([sys.executable,str(HERE/'analyze.py'),'--repo',str(repo),
                        '--input',str(root),'--output',str(root/'derived')],check=True)
        return
    if args.action=='bundle':
        subprocess.run([sys.executable,str(HERE/'bundle_sources.py'),'--repo',str(repo),
                        '--output',str(root/'source-snapshot.tar.gz')],check=True)
        return
    if args.action=='plan':
        for name,(kernel,n,description) in STAGES.items():
            print(f'{name:24s} {kernel:24s} {n:3d} runs  {description}')
        return
    if args.action=='fix-latency':
        path=repo/'tools/realsense_steady_bench/analyze_full_receive_path.py'
        old=path.read_text()
        if 'latency_s > 0.250' not in old:
            if 'if latency_s < 0.0:' not in old:
                raise ValueError('Unknown latency-filter implementation; inspect manually')
            print('No 250 ms upper cutoff found'); return
        new=patched_analyzer(old); compile(new,str(path),'exec')
        backup=root/'source-before-fix'/('analyze_full_receive_path-'+digest(path)+'.py')
        backup.parent.mkdir(parents=True,exist_ok=True); backup.write_text(old)
        path.write_text(new)
        print(f'Removed upper cutoff; original saved in {backup}'); return
    if args.action=='profiles': generate_profiles(repo,root); return
    cfg=load_config(args.config)
    if args.action=='prepare':
        prepare(repo,root,cfg); print(f'Prepared adapters in {root / "entrypoints"}'); return
    if not args.stage: ap.error('check/run requires a stage')
    errors=check(repo,root,args.stage,cfg)
    if errors:
        for error in errors: print('BLOCKED:',error,file=sys.stderr)
        raise SystemExit(1)
    if args.action=='check': print('Preflight passed'); return
    manifest=json.loads((root/'prepared.json').read_text())
    if manifest['config'] != cfg or manifest['repo'] != str(repo):
        raise ValueError('Config/repo changed; prepare again in a fresh output directory')
    for relative,sha in manifest['adapter_sources'].items():
        if digest(repo/relative)!=sha: raise ValueError(f'Source changed since prepare: {relative}')
    script=root/'entrypoints'/(args.stage+'.sh')
    if digest(script)!=manifest['entrypoints'][script.name]:
        raise ValueError('Adapter changed since prepare')
    marker=root/'completed'/(args.stage+'.json')
    if marker.exists():
        print('Stage already complete; use a fresh --output for a new campaign'); return
    subprocess.run(['sudo','-n','true'],check=True)
    lock = (root/'campaign.lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    provenance(repo,root,args.stage)
    command=['sh',str(script)]
    if args.stage=='e1': command=['sudo','-n']+command
    log=root/(args.stage+'.log')
    print(f'Running {args.stage}; output: {log}',flush=True)
    with log.open('a') as handle:
        subprocess.run(command,cwd=repo,stdout=handle,stderr=subprocess.STDOUT,check=True)
    if args.stage=='e1': generate_profiles(repo,root)
    marker.parent.mkdir(parents=True,exist_ok=True)
    marker.write_text(json.dumps({'stage':args.stage,'kernel':platform.release(),
                                  'adapter_sha256':digest(script)},indent=2)+'\n')

EXPERIMENTS = {
    'e1_workers': (['startup', 'ablation', 'e1'], ['startup', 'ablation', 'e1']),
    'e2_uvc_pool': (['e2-calibrate', 'e2-urb5', 'e2-urb16', 'diagnosis'], ['e2', 'diagnosis']),
    'e3_kernel_path': (['e3'], ['e3']),
    'e4_scheduling': (['e4-calibrate', 'e4-standard-hardirq', 'e4-standard-threaded',
                       'e4-rt-threaded', 'overhead'], ['e4', 'overhead']),
}


def experiment_main(experiment):
    stages, sections = EXPERIMENTS[experiment]
    parser = argparse.ArgumentParser(description=f'{experiment}: capture, analyze and plot')
    parser.add_argument('action', choices=['plan', 'prepare', 'check', 'run', 'analyze'])
    parser.add_argument('stage', nargs='?', choices=stages)
    parser.add_argument('--config', type=Path, default=HERE/'setup/config.json')
    parser.add_argument('--output', type=Path, default=HERE/'runs')
    parser.add_argument('--input', type=Path, help='Saved campaign or archive (for analyze)')
    parser.add_argument('--archive', action='store_true')
    parser.add_argument('--with-supplements', action='store_true',
                        help='Also analyze startup/ablation, diagnosis, or overhead')
    args = parser.parse_args()
    if args.action == 'plan':
        for stage in stages: print(stage, *STAGES[stage])
        return
    if args.action == 'analyze':
        selected = sections if args.with_supplements else [experiment.split('_')[0]]
        source = args.input or HERE/'runs'
        destination = args.output if args.output != HERE/'runs' else HERE/experiment/'derived'
        command = [sys.executable, str(HERE/'analyze.py'), '--input', str(source),
                   '--output', str(destination), '--sections', *selected]
        if args.archive: command.append('--archive')
    else:
        if args.action in ('run', 'check') and not args.stage:
            parser.error('run/check requires a stage; use plan to list stages')
        command = [sys.executable, str(HERE/'reproduce.py'), '--config', str(args.config),
                   '--output', str(args.output), args.action]
        if args.stage: command.append(args.stage)
    subprocess.run(command, check=True)


if __name__=='__main__':
    main()
