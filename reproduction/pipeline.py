#!/usr/bin/env python3
"""Two-level reproduction: archived data, or all captures across kernel reboots."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

import reproduce
from setup.kernel import VARIANTS

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SERVICE = 'rs-camera-reproduction.service'
UNIT = Path('/etc/systemd/system') / SERVICE
SIGNATURE = '# Managed by rs-camera reproduction/pipeline.py'
# Calibration dependencies require this ordering, including the second RT boot.
PHASES = [
    ('rt-urb16', False, ['e1', 'e2-calibrate']),
    ('standard-urb16', True, ['ablation', 'e3', 'e4-calibrate', 'e4-standard-threaded']),
    ('standard-urb5', False, ['e2-urb5']),
    ('standard-urb16', False, ['startup', 'e2-urb16', 'e4-standard-hardirq', 'overhead', 'diagnosis']),
    ('rt-urb16', False, ['e4-rt-threaded']),
]


def now(): return datetime.now(timezone.utc).isoformat()
def read(path): return json.loads(path.read_text())
def save(path, data):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, indent=2) + '\n')
    temporary.replace(path)


def call(command, log=None, **kwargs):
    print('+', ' '.join(map(str, command)), flush=True)
    if log:
        with log.open('a') as stream:
            subprocess.run(list(map(str, command)), check=True, stdout=stream,
                           stderr=subprocess.STDOUT, **kwargs)
    else: subprocess.run(list(map(str, command)), check=True, **kwargs)


def py(script, *args, **kwargs):
    call([sys.executable, HERE / script, *args], **kwargs)


def require_analysis(pdf):
    call([sys.executable, REPO/'scripts/check_dependencies.py', '--sources', '--python'])
    with tempfile.TemporaryDirectory(prefix='rs-camera-analysis-check-') as temp:
        call([sys.executable, '-c', 'import numpy; import benchkit'], env={**os.environ, 'TMPDIR':temp})
    for name in ('paper_metrics.py', 'e4_scheduling/metrics.py', 'e4_scheduling/tikz.py'):
        if not (HERE/name).is_file():
            raise ValueError(f'Missing tracked reproduction helper: {name}')
    if pdf:
        from render import check_dependencies
        check_dependencies()


def require_empty(root):
    if root.exists() and any(root.iterdir()):
        raise ValueError(f'Output must be new or empty: {root}')


def analyze(source, output, archive, pdf, log):
    command = ['--input', source, '--output', output]
    if archive: command.append('--archive')
    elif (source/'prepared.json').exists() and read(source/'prepared.json').get('smoke'):
        command += ['--smoke', '--sections', 'e1', 'e2', 'e3', 'e4']
    py('analyze.py', *command, log=log)
    if pdf: py('render.py', output, log=log)


def raw(args):
    source, root = args.input.resolve(), args.output.resolve()
    if not source.is_dir(): raise ValueError(f'Missing raw-data package: {source}')
    if root == source or root.is_relative_to(source):
        raise ValueError('Keep generated output outside the raw-data package')
    require_empty(root)
    require_analysis(not args.no_pdf)
    root.mkdir(parents=True, exist_ok=True)
    state = dict(level='raw', status='running', source=str(source), started_at=now(), pdf=not args.no_pdf)
    save(root/'pipeline.json', state)
    try:
        sums = source/'SHA256SUMS'
        if sums.is_file():
            call(['sha256sum', '--check', '--quiet', sums.name], cwd=source, log=root/'checksums.log')
            state['checksums'] = 'verified'
        else: state['checksums'] = 'no SHA256SUMS supplied; analysis manifest records consumed input hashes'
        save(root/'pipeline.json', state)
        analyze(source, root/'derived', True, not args.no_pdf, root/'analysis.log')
        state.update(status='complete', completed_at=now())
        save(root/'pipeline.json', state)
        print(f'Complete: {root / "derived"}')
    except BaseException as error:
        state.update(status='failed', error=str(error)); save(root/'pipeline.json', state)
        raise


def boot_matches(variant, threaded, release=None, cmdline=None):
    release = platform.release() if release is None else release
    cmdline = Path('/proc/cmdline').read_text() if cmdline is None else cmdline
    return release == VARIANTS[variant][0] and (
        variant.startswith('rt-') or ('threadirqs' in cmdline.split()) == threaded)


def complete(root, stage):
    marker = root/'completed'/f'{stage}.json'
    if not marker.exists(): return False
    record = read(marker)
    script = root/'entrypoints'/f'{stage}.sh'
    if record.get('stage') != stage or record.get('adapter_sha256') != reproduce.digest(script):
        raise ValueError(f'Invalid completion marker: {marker}')
    return True


def phases(root, smoke=None):
    if smoke is None:
        state = read(root/'pipeline.json') if (root/'pipeline.json').exists() else {}
        smoke = state.get('smoke', False)
    if not smoke: return PHASES
    supplements = {'startup', 'ablation', 'overhead', 'diagnosis'}
    return [(variant, threaded, [s for s in stages if s not in supplements])
            for variant, threaded, stages in PHASES]


def next_phase(root):
    for variant, threaded, stages in phases(root):
        pending = [stage for stage in stages if not complete(root, stage)]
        if pending: return variant, threaded, pending
    return None


def require_pi():
    model = Path('/proc/device-tree/model')
    if not model.is_file() or 'Raspberry Pi 5' not in model.read_text():
        raise ValueError('Full capture requires a configured Raspberry Pi 5; use raw or plan on a laptop')


def root_access():
    if os.geteuid() != 0:
        # Retain the SSH route guard when sudo starts the privileged controller.
        call(['sudo', '--preserve-env=SSH_CONNECTION', sys.executable, HERE/'pipeline.py', *sys.argv[1:]])
        raise SystemExit(0)


def systemd_path(path):
    # Avoid systemd specifier/environment expansion in installed unit arguments.
    value = str(path)
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+', value):
        raise ValueError('Full-mode repository/output/Python paths must be absolute without spaces or shell metacharacters')
    return value


def service_text(root):
    executable, script, destination = map(systemd_path, (sys.executable, HERE/'pipeline.py', root))
    return f'''{SIGNATURE}
[Unit]
Description=rs-camera complete paper reproduction
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
WorkingDirectory={systemd_path(REPO)}
Environment=SUDO_UID={REPO.stat().st_uid} SUDO_GID={REPO.stat().st_gid}
ExecStartPre=/usr/bin/udevadm settle --timeout=30
ExecStart={executable} {script} _worker --output {destination}
TimeoutStartSec=infinity
TimeoutStopSec=120
KillMode=control-group
Restart=no
UMask=0022
[Install]
WantedBy=multi-user.target
'''


def check_unit_available(root, resuming=False):
    if UNIT.exists():
        if not UNIT.read_text().startswith(SIGNATURE):
            raise ValueError(f'Refusing to replace an unrelated service: {UNIT}')
        active = subprocess.check_output(['systemctl', 'show', '--property=ActiveState', '--value', SERVICE], text=True).strip() in {'active','activating','deactivating','reloading'}
        if active: raise ValueError('A full reproduction is already running; inspect status first')
        if resuming and UNIT.read_text() != service_text(root):
            raise ValueError('Installed service belongs to a different output directory')
        enabled = subprocess.run(['systemctl', 'is-enabled', '--quiet', SERVICE], stdout=subprocess.DEVNULL).returncode == 0
        if enabled and not resuming: raise ValueError('An unfinished service is enabled; use status/resume/stop')


def verify_sources(root):
    with tarfile.open(root/'source-snapshot.tar.gz') as bundle:
        manifest = json.load(bundle.extractfile('SOURCE_MANIFEST.json'))
    for relative, expected in manifest['source_sha256'].items():
        if reproduce.digest(REPO/relative) != expected:
            raise ValueError(f'Frozen source changed: {relative}; preserve this campaign and use a new output')


def snapshot_system(root):
    boot = Path('/boot/firmware')
    paths = [boot/'config.txt'] + [boot/value[1]/'cmdline.txt' for value in VARIANTS.values()]
    originals = {str(path): path.read_text() for path in paths}
    wifi = Path('/sys/class/net/wlan0/operstate')
    save(root/'system-before.json', dict(boot_files=originals, wifi_up=wifi.exists() and wifi.read_text().strip()=='up'))


def restore_system(root):
    before = read(root/'system-before.json')
    for name, text in before['boot_files'].items(): Path(name).write_text(text)
    if before['wifi_up']: call(['ip', 'link', 'set', 'dev', 'wlan0', 'up'])
    os.sync()


def check_wired(wait_seconds=0):
    eth = Path('/sys/class/net/eth0/operstate')
    deadline = time.monotonic() + wait_seconds
    while not eth.exists() or eth.read_text().strip() != 'up':
        if time.monotonic() >= deadline:
            raise ValueError('Full mode requires an active eth0 connection before disabling Wi-Fi')
        time.sleep(1)
    peer = os.environ.get('SSH_CONNECTION', '').split()
    if peer:
        route = subprocess.check_output(['ip', 'route', 'get', peer[0]], text=True)
        if 'dev eth0' not in route:
            raise ValueError('Reconnect SSH through Ethernet before starting full mode')


def full(args):
    require_pi()
    # rustup belongs to the setup user. sudo changes HOME; the root runtime
    # does not need its own copy of the build-only Rust toolchain.
    if os.geteuid() != 0:
        call([sys.executable, REPO/'scripts/check_dependencies.py', '--rust'])
    root_access()
    root = args.output.resolve()
    require_empty(root)
    unit = service_text(root)
    check_unit_available(root)
    require_analysis(not args.no_pdf)
    check_wired()
    call([sys.executable, REPO/'scripts/check_dependencies.py', '--system'])
    for variant in VARIANTS: py('setup/kernel.py', 'select', variant)  # dry-run, all boot artifacts must exist
    # Check common build prerequisites using one stage while deferring its boot/Wi-Fi requirements.
    root.mkdir(parents=True, exist_ok=True)
    config = root/'camera-config.json'
    if args.config.exists(): shutil.copy2(args.config, config)
    py('setup/discover.py', '--output', config, '--inventory', root/'camera-inventory.json')
    cfg = reproduce.load_config(config)
    errors = [e for e in reproduce.check(REPO, root, 'e1', cfg)
              if not e.startswith(('Boot kernel ', 'Paper runs disable Wi-Fi'))]
    if errors: raise ValueError('\n'.join(errors))
    reproduce.prepare(REPO, root, cfg, smoke=args.smoke, calibration_seconds=args.calibration_seconds)
    py('bundle_sources.py', '--output', root/'source-snapshot.tar.gz', '--exclude', root)
    snapshot_system(root)
    save(root/'pipeline.json', dict(level='full', status='queued', repo=str(REPO), started_at=now(),
         pdf=not args.no_pdf, smoke=args.smoke, calibration_seconds=args.calibration_seconds, active_stage=None, pending_boot=None))
    UNIT.write_text(unit)
    call(['systemctl', 'daemon-reload'])
    call(['systemctl', 'enable', SERVICE])
    call(['systemctl', 'start', '--no-block', SERVICE])
    print(f'Started {SERVICE}; automatic kernel switches/reboots are enabled.\nStatus: ./reproduce.sh status --output {root}')


def current_boot_id():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def refresh_boot_config(root, boot_id):
    """Re-enumerate IRQs after reboot without changing physical camera roles."""
    folder = root/'boot-inventories'/boot_id
    folder.mkdir(parents=True, exist_ok=True)
    config = folder/'config.json'
    shutil.copy2(root/'camera-config.json', config)
    py('setup/discover.py', '--output', config, '--inventory', folder/'inventory.json')
    old, new = (reproduce.load_config(p) for p in (root/'camera-config.json', config))
    if reproduce.camera_identity(old) != reproduce.camera_identity(new):
        raise ValueError('Camera identities/roles changed across boots; refusing to combine experiments')
    previous = read(root/'prepared.json')
    for name, digest in previous['entrypoints'].items():
        if reproduce.digest(root/'entrypoints'/name) != digest:
            raise ValueError(f'Prepared adapter was modified: {name}')
    if old != new:
        save(folder/'prepared-before-irq-refresh.json', previous)
        reproduce.prepare(REPO, root, new, allow_irq_refresh=True, smoke=previous.get('smoke', False), calibration_seconds=previous.get('calibration_seconds', 30))
        shutil.copy2(config, root/'camera-config.json')
    save(folder/'prepared.json', read(root/'prepared.json'))


def worker(root):
    require_pi()
    if os.geteuid() != 0: raise ValueError('Worker must run in the installed root service')
    temporary = root/'private-tmp'
    temporary.mkdir(mode=0o700, exist_ok=True)
    os.environ['TMPDIR'] = str(temporary)
    state = read(root/'pipeline.json')
    if state['repo'] != str(REPO): raise ValueError('Repository moved since preparation')
    lock = (root/'pipeline.lock').open('w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        lock.close()
        raise
    try:
        verify_sources(root)
        # network-online.target can precede Ethernet carrier after a Pi reboot.
        check_wired(wait_seconds=60)
        stage = state.get('active_stage')
        if stage and not complete(root, stage):
            raise ValueError(f'{stage} was interrupted without a completion marker; keep partial data and start a new campaign')
        boot_id = current_boot_id()
        pending = state.get('pending_boot')
        if pending:
            if pending['boot_id'] == boot_id or not boot_matches(pending['variant'], pending['threaded']):
                raise ValueError('Requested kernel boot did not complete; automatic reboot retry disabled')
            state['pending_boot'] = None
        state.update(status='running', active_stage=None)
        save(root/'pipeline.json', state)
        refresh_boot_config(root, boot_id)
        # Root service has durable privilege; no expiring sudo ticket or sudoers changes.
        if Path('/sys/class/net/wlan0').exists(): call(['ip', 'link', 'set', 'dev', 'wlan0', 'down'])
        while True:
            phase = next_phase(root)
            if phase is None: break
            variant, threaded, stages = phase
            if not boot_matches(variant, threaded):
                command = ['select', variant, '--apply']
                if threaded: command.append('--threadirqs')
                py('setup/kernel.py', *command)
                state.update(status='rebooting', pending_boot=dict(variant=variant, threaded=threaded, boot_id=boot_id))
                save(root/'pipeline.json', state)
                os.sync()
                call(['systemctl', 'reboot'])
                return
            for stage in stages:
                command = ['--output', root, '--config', root/'camera-config.json']
                py('reproduce.py', *command, 'check', stage)
                (root/'environment').mkdir(exist_ok=True)
                save(root/'environment'/f'{stage}-prepared.json', read(root/'prepared.json'))
                state['active_stage'] = stage; save(root/'pipeline.json', state)
                py('reproduce.py', *command, 'run', stage)
                if not complete(root, stage): raise ValueError(f'No valid completion marker for {stage}')
                state['active_stage'] = None; save(root/'pipeline.json', state)
        output = root/'derived'
        if not output.exists():
            pending_output = Path(tempfile.mkdtemp(prefix='analysis-attempt-', dir=root))
            analyze(root, pending_output, False, False, root/'analysis.log')
            pending_output.chmod(0o755)
            pending_output.rename(output)
        if not (output/'manifest.json').exists(): raise ValueError('Incomplete derived output; preserve it before retrying analysis')
        if state['pdf']: py('render.py', output, log=root/'analysis.log')
        restore_system(root)
        state.update(status='complete', completed_at=now(), derived=str(output))
        save(root/'pipeline.json', state)
        call(['systemctl', 'disable', SERVICE])
    except BaseException as error:
        state.update(status='failed', error=str(error), failed_at=now())
        save(root/'pipeline.json', state)
        try: restore_system(root)
        finally: call(['systemctl', 'disable', SERVICE])
        raise
    finally:
        lock.close()


def control(args):
    root = args.output.resolve()
    state = read(root/'pipeline.json')
    if args.action == 'status':
        print(json.dumps(state, indent=2))
        if state['level'] == 'full':
            print('Completed:', ', '.join(s for _,_,stages in phases(root) for s in stages if complete(root,s)))
            print(f'Log: journalctl -u {SERVICE} -f')
        return
    if state['level'] != 'full': raise ValueError('resume/stop apply only to full-mode campaigns')
    if state['status'] == 'complete':
        if args.action == 'stop':
            print('Campaign is already complete and its service is disabled.'); return
        raise ValueError('Campaign already complete')
    require_pi(); root_access()
    if not UNIT.exists() or UNIT.read_text() != service_text(root):
        raise ValueError('Output does not match the installed reproduction service')
    if args.action == 'stop':
        call(['systemctl', 'disable', '--now', SERVICE])
        state = read(root/'pipeline.json')
        restore_system(root)
        state.update(status='stopped', stopped_at=now()); save(root/'pipeline.json', state)
    else:
        if state['status']=='complete': raise ValueError('Campaign already complete')
        check_unit_available(root, resuming=True)
        stage = state.get('active_stage')
        if stage and not complete(root, stage): raise ValueError('Interrupted acquisition cannot be resumed safely; use a new output')
        verify_sources(root)
        check_wired()
        state.update(status='queued', pending_boot=None); save(root/'pipeline.json', state)
        call(['systemctl', 'enable', SERVICE]); call(['systemctl', 'start', '--no-block', SERVICE])


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('action', choices=['raw','full','plan','status','resume','stop','_worker'])
    ap.add_argument('--input', type=Path, default=REPO/'paperwriting/paper_used_raw_data', help='raw-data package (raw mode)')
    ap.add_argument('--output', type=Path, help='new output directory, or campaign to inspect/control')
    ap.add_argument('--config', type=Path, default=HERE/'setup/config.json', help='optional existing camera role selection (full mode)')
    ap.add_argument('--smoke', action='store_true', help='3-second measurements, one repetition per main-experiment cell; omit supplements')
    ap.add_argument('--calibration-seconds', type=int, choices=[3,30], default=30, help='smoke calibration duration; 3 seconds may not observe timer workers')
    ap.add_argument('--no-pdf', action='store_true', help='emit CSV/TeX without compiling PDF; default requires TeX')
    args = ap.parse_args()
    args.output = args.output or (HERE/'derived-raw' if args.action=='raw' else HERE/('runs/smoke' if args.smoke else 'runs/full'))
    if args.action=='plan':
        print('raw: verify supplied checksums -> analyze E1-E4 + supplements -> CSV/TeX/PDF')
        if args.smoke:
            print(f'full --smoke: 3-second measurements, one repetition; {args.calibration_seconds}-second calibrations; no supplements')
        for variant, threaded, stages in phases(args.output, args.smoke):
            print(f'full: {variant}, threadirqs={threaded}: '+', '.join(stages))
        print('full: analyze all -> CSV/TeX/PDF -> restore saved next-boot configuration and Wi-Fi link -> disable service')
        return
    try:
        if args.action=='raw': raw(args)
        elif args.action=='full': full(args)
        elif args.action=='_worker': worker(args.output.resolve())
        else: control(args)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        return 1
    return 0

if __name__=='__main__': sys.exit(main())
