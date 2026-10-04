#!/usr/bin/env python3
"""Install existing matched kernel artifacts or select a kernel for next boot.

Dry-run by default. Does not reboot. Run --apply with sudo on the Pi only.
"""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import os
import re
import shutil
import subprocess

HERE=Path(__file__).resolve().parent
REPO=HERE.parents[1]
VARIANTS={
    'standard-urb5': ('6.12.96-rpi5-standard-btf+', 'standard-6.12-btf', 'artifacts/standard-boot', 'artifacts/standard-root-stripped'),
    'standard-urb16': ('6.12.96-rpi5-standard-btf-uvc16+', 'standard-6.12-btf-uvc16', 'artifacts/standard-uvc16-boot', 'stage-standard-btf-uvc16'),
    'rt-urb16': ('6.12.96-rpi5-rt-btf-uvc16+', 'rt-6.12-btf-uvc16', 'artifacts/rt-uvc16-boot', 'stage-rt-btf-uvc16'),
}

def config_settings(text):
    # Generated comments and the compiler's executable/branding string can
    # differ between native and cross builds. Configuration values must match.
    return {line.split('=',1)[0]: line.split('=',1)[1]
            for line in text.splitlines()
            if line.startswith('CONFIG_') and '=' in line
            and not line.startswith('CONFIG_CC_VERSION_TEXT=')}


def boot_selection(config, cmdline, prefix, threaded):
    count=len(re.findall(r'^os_prefix=.*$',config,re.M))
    if count > 1:
        raise ValueError('Multiple os_prefix entries; inspect boot configuration')
    if count:
        config=re.sub(r'^os_prefix=.*$',f'os_prefix={prefix}/',config,flags=re.M)
    else:
        config=config.rstrip()+f'\n\n[all]\nos_prefix={prefix}/\n'
    words=[w for w in cmdline.split() if w!='threadirqs']
    if not any(w.startswith('root=') for w in words): raise ValueError('Missing root= boot argument')
    if threaded: words.append('threadirqs')
    return config,' '.join(words)+'\n'

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('action',choices=['install','select'])
    ap.add_argument('variant',choices=VARIANTS)
    ap.add_argument('--threadirqs',action='store_true',help='For non-RT threaded stages')
    ap.add_argument('--artifacts',type=Path,default=REPO/'build-rpi-kernel')
    ap.add_argument('--boot',type=Path,default=Path('/boot/firmware'))
    ap.add_argument('--root',type=Path,default=Path('/'))
    ap.add_argument('--apply',action='store_true')
    args=ap.parse_args()
    release,prefix,boot_source,modules_source=VARIANTS[args.variant]
    destination=args.boot/prefix
    if args.action=='install':
        src=args.artifacts/boot_source; modules=args.artifacts/modules_source/'lib/modules'/release
        for p in [src/'vmlinuz',src/f'config-{release}',src/'bcm2712-rpi-5-b.dtb',src/'overlays',modules]:
            if not p.exists(): raise ValueError(f'Missing kernel artifact: {p}')
        if config_settings((src/f'config-{release}').read_text()) != config_settings((HERE/'kernel-configs'/f'{args.variant}.config').read_text()):
            raise ValueError('Artifact kernel configuration differs from the preserved configuration')
        if destination.exists() or (args.root/'lib/modules'/release).exists():
            raise ValueError('Kernel already installed; refusing to overwrite its boot files/modules')
        print(f'Install {src} -> {destination}; modules {modules}; generate initrd for {release}')
    else:
        for name in ['vmlinuz','initrd.img','cmdline.txt']:
            if not (destination/name).is_file(): raise ValueError(f'Missing {destination/name}')
        config,cmdline=boot_selection((args.boot/'config.txt').read_text(),(destination/'cmdline.txt').read_text(),prefix,args.threadirqs)
        print(f'Next boot: {release}; threadirqs={args.threadirqs}; prefix={prefix}')
        print(cmdline.strip())
    if not args.apply: print('Dry run. Add --apply on the Pi to write.'); return
    if os.geteuid()!=0: raise ValueError('--apply requires root')
    if args.root!=Path('/') or args.boot!=Path('/boot/firmware'):
        raise ValueError('--apply supports only the live Pi paths')
    if 'Raspberry Pi 5' not in Path('/proc/device-tree/model').read_text(): raise ValueError('Pi 5 required')
    if args.action=='install':
        shutil.copytree(src,destination)
        shutil.copytree(modules,args.root/'lib/modules'/release,symlinks=True)
        shutil.copy2(args.boot/'cmdline.txt',destination/'cmdline.txt')
        subprocess.run(['depmod','-a',release],check=True)
        subprocess.run(['mkinitramfs','-o',str(destination/'initrd.img'),release],check=True)
    else:
        stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        backup=args.boot/('reproduction-backup-'+stamp); backup.mkdir()
        shutil.copy2(args.boot/'config.txt',backup/'config.txt')
        shutil.copy2(destination/'cmdline.txt',backup/'cmdline.txt')
        (backup/'prefix.txt').write_text(prefix+'\n')
        (destination/'cmdline.txt').write_text(cmdline)
        (args.boot/'config.txt').write_text(config)
        print(f'Boot backup: {backup}. Reboot manually, then run reproduce.py check STAGE.')
    os.sync()

if __name__=='__main__': main()
