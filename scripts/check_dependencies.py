#!/usr/bin/env python3
"""Verify pinned sources, Python libraries, Rust, or the reference Pi packages."""
import argparse
from importlib import metadata
import json
from pathlib import Path
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def lines(path):
    return [s.strip() for s in path.read_text().splitlines()
            if s.strip() and not s.lstrip().startswith('#')]


def output(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT).strip()


def check_sources(root=ROOT):
    errors = []
    for name, expected in json.loads((root/'dependencies/sources.json').read_text()).items():
        path = root/name
        try:
            if not (path/'.git').exists():
                raise ValueError('submodule is not initialized')
            actual = output('git', '-C', str(path), 'rev-parse', 'HEAD')
            if actual != expected:
                errors.append(f'{name}: expected {expected}, found {actual}')
            if output('git', '-C', str(path), 'status', '--porcelain', '--untracked-files=normal'):
                errors.append(f'{name}: has local changes; restore a clean pinned checkout')
            entry = output('git', '-C', str(root), 'ls-tree', 'HEAD', '--', name).split()
            if len(entry) < 3 or entry[0] != '160000' or entry[2] != expected:
                errors.append(f'{name}: lock differs from the superproject gitlink')
        except (subprocess.CalledProcessError, ValueError) as error:
            errors.append(f'{name}: {error}')
    return errors


def check_python(build_tools=False):
    errors = []
    if sys.version_info[:2] != (3, 12):
        errors.append(f'Python: expected 3.12, found {sys.version.split()[0]}')
    pins = lines(ROOT/'requirements.txt') + ['pybenchkit==0.0.2']
    if build_tools:
        pins += lines(ROOT/'dependencies/python-build.txt')
    for pin in pins:
        name, expected = pin.split('==')
        try:
            actual = metadata.version(name)
        except metadata.PackageNotFoundError:
            actual = 'missing'
        if actual != expected:
            errors.append(f'{name}: expected {expected}, found {actual}')
    try:
        direct = json.loads(metadata.distribution('pybenchkit').read_text('direct_url.json') or '{}')
        if direct.get('url') != (ROOT/'deps/benchkit').as_uri() or not direct.get('dir_info', {}).get('editable'):
            errors.append('pybenchkit: must be installed editable from this checkout/deps/benchkit')
    except metadata.PackageNotFoundError:
        pass
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources', action='store_true')
    parser.add_argument('--python', action='store_true')
    parser.add_argument('--build-tools', action='store_true')
    parser.add_argument('--rust', action='store_true')
    parser.add_argument('--system', action='store_true', help='Reference Ubuntu 24.04 ARM64 only')
    args = parser.parse_args()
    if not any(vars(args).values()):
        args.sources = args.python = True
    errors = check_sources() if args.sources else []
    if args.python or args.build_tools:
        errors.extend(check_python(args.build_tools))
    if args.rust:
        version = tomllib.loads((ROOT/'rust-toolchain.toml').read_text())['toolchain']['channel']
        try:
            actual = output('rustup', 'run', version, 'rustc', '--version').split()[1]
            if actual != version:
                errors.append(f'Rust: expected {version}, found {actual}')
        except (subprocess.CalledProcessError, FileNotFoundError) as error:
            errors.append(f'Rust {version}: {error}')
    if args.system:
        os_release = dict(s.split('=', 1) for s in Path('/etc/os-release').read_text().splitlines() if '=' in s)
        if os_release.get('ID', '').strip('"') != 'ubuntu' or os_release.get('VERSION_ID', '').strip('"') != '24.04' or output('dpkg', '--print-architecture') != 'arm64':
            errors.append('System lock requires Ubuntu 24.04 ARM64')
        else:
            for pin in lines(ROOT/'dependencies/ubuntu-24.04-arm64.txt'):
                name, expected = pin.split('=', 1)
                try:
                    actual = output('dpkg-query', '-W', '-f=${Version}', name)
                except subprocess.CalledProcessError:
                    actual = 'missing'
                if actual != expected:
                    errors.append(f'{name}: expected {expected}, found {actual}')
    if errors:
        print('\n'.join(errors), file=sys.stderr)
        return 1
    print('Selected dependency locks verified.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
