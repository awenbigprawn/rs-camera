#!/usr/bin/env python3
"""Discover RealSense SDK identities and Pi USB/IRQ topology; save local config."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
from itertools import combinations
import json
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO/'tools'))
from realsense_bench_common.realsense_devices import discover_realsense_devices
from realsense_bench_common.cpu_isolation import CpuIsolation, CpuIsolationConfig


def inventory(sdk, usb, irqs):
    """Join by physical ancestry, never by the different USB descriptor serial."""
    devices, issues = [], []
    for device in sorted(sdk, key=lambda item: item['serial']):
        item = dict(device)
        match = re.search(r'\b(D435|D455F|D455)\b', device['name'])
        item['model'] = match.group(1) if match else 'unsupported'
        physical = Path(device['physical_port']).resolve()
        candidates = [u for u in usb if Path(u['resolved_sysfs_path']) in (physical, *physical.parents)]
        if len(candidates) != 1:
            issues.append(f"{item['serial']}: SDK physical port does not identify exactly one USB device")
        else:
            item['usb'] = candidates[0]
            mappings = [irq for irq in irqs if candidates[0]['usb_device'] in irq['usb_devices']]
            if len(mappings) == 1:
                item.update(xhci_irq=mappings[0]['irq'], xhci_label=mappings[0]['action'],
                            xhci_controller=mappings[0]['controller'])
            else:
                issues.append(f"{item['serial']}: cannot identify exactly one xHCI IRQ")
        devices.append(item)
    seen = [d['serial'] for d in devices]
    if len(seen) != len(set(seen)):
        issues.append('SDK returned duplicate serials')
    mapped = {d.get('usb', {}).get('usb_device') for d in devices}
    for u in usb:
        if u['usb_device'] not in mapped:
            issues.append(f"USB camera {u['usb_device']} is not visible to the SDK; check udev permissions")
    return devices, issues


def make_config(devices, previous=None):
    usable = [d for d in devices if d['model'] in ('D435', 'D455', 'D455F')]
    for d in usable:
        if not d['serial'].isdigit() or not d.get('xhci_irq'):
            raise ValueError(f"{d['serial']}: missing SDK serial or IRQ mapping")
        if float(d.get('usb', {}).get('speed_mbps') or 0) < 5000:
            raise ValueError(f"{d['serial']}: requires USB SuperSpeed; check cable/hub/port")
    # Keep reviewer-selected aliases and roles when rediscovering the same cameras.
    aliases = {c['serial']: name for name, c in (previous or {}).get('cameras', {}).items()}
    cameras = {}
    for d in usable:
        name = aliases.get(d['serial'], d['model'].lower()+'_'+d['serial'])
        if name in cameras: raise ValueError(f'Duplicate camera alias: {name}')
        cameras[name] = {key: d[key] for key in ('model','serial','xhci_irq','xhci_label')}
    if previous:
        selection = previous['selection']
        selected = selection['d435_pair'] + [selection[k] for k in ('single_d435','single_d455','startup_d435')]
        if any(name not in cameras for name in selected):
            raise ValueError('A previously selected camera is missing; reconnect it or use --new-selection')
    else:
        d435 = sorted(k for k,c in cameras.items() if c['model']=='D435')
        pairs = [(a,b) for a,b in combinations(d435,2) if cameras[a]['xhci_irq']!=cameras[b]['xhci_irq']]
        d455 = sorted(k for k,c in cameras.items() if c['model'] in ('D455','D455F'))
        if not pairs or not d455:
            raise ValueError('Full reproduction needs two D435 cameras on separate controllers and one D455/D455F')
        pair = list(pairs[0])
        selection = dict(d435_pair=pair, single_d435=pair[1], single_d455=d455[0], startup_d435=pair[0])
    a,b = selection['d435_pair']
    if a == b or any(cameras[k]['model']!='D435' for k in (a,b,selection['single_d435'],selection['startup_d435'])):
        raise ValueError('D435 roles must select the appropriate distinct cameras')
    if cameras[selection['single_d455']]['model'] not in ('D455','D455F'):
        raise ValueError('The D455 role must select a D455/D455F')
    if cameras[a]['xhci_irq']==cameras[b]['xhci_irq']:
        raise ValueError('The selected D435 pair shares a controller; move one to the other Pi USB3 port')
    return dict(cameras=cameras, selection=selection)


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value, indent=2)+'\n')
    temporary.replace(path)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--probe', type=Path, default=REPO/'build-realsense-steady/d435_sensor_probe')
    ap.add_argument('--output', type=Path, default=HERE/'config.json')
    ap.add_argument('--inventory', type=Path, default=HERE/'inventory.json')
    ap.add_argument('--check', action='store_true', help='Compare live devices with saved config; do not write files')
    ap.add_argument('--new-selection', action='store_true', help='Select roles again instead of retaining saved selections')
    args = ap.parse_args()
    if args.check and args.new_selection: ap.error('--check and --new-selection cannot be combined')
    if args.output.resolve()==args.inventory.resolve(): ap.error('Config and inventory paths must differ')
    try:
        result = subprocess.run([str(args.probe.resolve()), '--list-devices'], check=True,
                                capture_output=True, text=True, timeout=60)
        sdk = json.loads(result.stdout)['devices']
        usb = discover_realsense_devices(Path('/sys/bus/usb/devices'))
        control = CpuIsolation(CpuIsolationConfig(enabled=False, housekeeping_cpus='0',
                               benchmark_cpus='1-3', use_sudo=False, repo_root=REPO))
        issues = []
        try: irqs = control.discover_camera_xhci_irqs()
        except RuntimeError as error: irqs = []; issues.append(str(error))
        devices, mapping_issues = inventory(sdk, usb, irqs)
        issues.extend(mapping_issues)
        previous = json.loads(args.output.read_text()) if args.output.exists() else None
        config = None
        try: config = make_config(devices, None if args.new_selection else previous)
        except (ValueError, KeyError) as error: issues.append(str(error))
        if not args.check:
            save_json(args.inventory, dict(observed_at=datetime.now(timezone.utc).isoformat(),
                kernel=platform.release(), devices=devices, issues=issues, ready=not issues))
        for d in devices:
            print(f"{d['model']:11s} SDK={d['serial']} USB={d.get('usb',{}).get('usb_device','?')} "
                  f"IRQ={d.get('xhci_irq','?')} firmware={d.get('firmware','?')}")
        if issues: raise ValueError('; '.join(issues))
        if args.check:
            if config != previous: raise ValueError('Live camera/IRQ configuration differs from saved config')
            print('Camera configuration matches live SDK/USB/IRQ discovery.'); return
        if previous and previous != config:
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            backup = args.output.with_name(args.output.stem+'.previous-'+stamp+'.json')
            shutil.copy2(args.output, backup)
            print(f'Previous config saved: {backup}')
        if previous != config: save_json(args.output, config)
        print(f'Configuration: {args.output}\nInventory and supported profiles: {args.inventory}')
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f'Discovery failed: {error}', file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError): print(error.stderr, file=sys.stderr)
        print('Existing config was not changed. Build the probe with setup/install.sh and check camera connections.',file=sys.stderr)
        return 1
    return 0

if __name__ == '__main__': sys.exit(main())
