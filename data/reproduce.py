#!/usr/bin/env python3
"""Verify the artifact, unpack selected measurement metadata, and regenerate paper outputs."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tarfile

HERE = Path(__file__).resolve().parent


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def child(root, name):
    relative = PurePosixPath(name)
    if relative.is_absolute() or '..' in relative.parts or '\\' in name:
        raise ValueError(f'Unsafe artifact path: {name}')
    target = root.joinpath(*relative.parts)
    if not target.resolve().is_relative_to(root.resolve()) or target.is_symlink():
        raise ValueError(f'Artifact path escapes its root: {name}')
    return target


def verify(root, checksum_file, strict=False):
    count = 0
    expected_paths = set()
    for line in checksum_file.read_text().splitlines():
        expected, name = line.split('  ', 1)
        name = PurePosixPath(name).as_posix()
        if name in expected_paths:
            raise ValueError(f'Duplicate checksum entry: {name}')
        expected_paths.add(name)
        if len(expected) != 64 or digest(child(root, name)) != expected:
            raise ValueError(f'Checksum mismatch: {name}')
        count += 1
    if strict:
        actual = {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()}
        extras = actual - expected_paths - {checksum_file.name, '.archive-verified.json'}
        if extras:
            raise ValueError(f'Unexpected files in metadata package: {sorted(extras)[:5]}')
    print(f'Verified {count} files against {checksum_file.name}', flush=True)


class Parts(io.RawIOBase):
    """Read archive parts in manifest order without assembling a second archive."""
    def __init__(self, paths):
        self.paths = iter(paths)
        self.current = None

    def readable(self):
        return True

    def readinto(self, buffer):
        while True:
            if self.current is None:
                path = next(self.paths, None)
                if path is None:
                    return 0
                self.current = path.open('rb')
            size = self.current.readinto(buffer)
            if size:
                return size
            self.current.close()
            self.current = None

    def close(self):
        if self.current is not None:
            self.current.close()
        super().close()


def unpack(target):
    metadata = json.loads((HERE / 'metadata/ARCHIVE.json').read_text())
    marker = target / '.archive-verified.json'
    if target.exists() and any(target.iterdir()):
        if not marker.is_file() or json.loads(marker.read_text()) != metadata:
            raise ValueError(f'Use an empty extraction directory: {target}')
        verify(target, target / 'SHA256SUMS', strict=True)
        return
    target.mkdir(parents=True, exist_ok=True)
    parts = []
    for part in metadata['parts']:
        path = child(HERE / 'metadata', part['name'])
        if path.stat().st_size != part['bytes'] or digest(path) != part['sha256']:
            raise ValueError(f'Invalid archive part: {path.name}')
        parts.append(path)
    count = size = 0
    with Parts(parts) as raw, io.BufferedReader(raw) as stream:
        with tarfile.open(fileobj=stream, mode='r|gz') as archive:
            for member in archive:
                if not member.isfile():
                    raise ValueError(f'Unexpected archive entry: {member.name}')
                destination = child(target, member.name)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, destination.open('xb') as output:
                    while block := source.read(1024 * 1024):
                        output.write(block)
                if destination.stat().st_size != member.size:
                    raise ValueError(f'Truncated archive entry: {member.name}')
                count += 1
                size += member.size
    if (count, size) != (metadata['files'], metadata['uncompressed_file_bytes']):
        raise ValueError('Archive inventory does not match ARCHIVE.json')
    if digest(target / 'SHA256SUMS') != metadata['source_sha256sums']:
        raise ValueError('Original checksum manifest differs')
    verify(target, target / 'SHA256SUMS', strict=True)
    marker.write_text(json.dumps(metadata, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify-only', action='store_true')
    parser.add_argument('--extract-only', action='store_true')
    parser.add_argument('--input', type=Path, help='Existing extracted paper metadata package')
    parser.add_argument('--output', type=Path, default=HERE / 'generated')
    parser.add_argument('--pdf', action='store_true', help='Also render the 11 paper objects; requires TeX Live')
    args = parser.parse_args()
    if sys.version_info < (3, 12):
        parser.error('Use Python 3.12 or newer; validated with Python 3.12')
    verify(HERE, HERE / 'SHA256SUMS')
    if args.verify_only:
        return
    source = args.input.resolve() if args.input else HERE / 'metadata-unpacked'
    if args.input:
        metadata = json.loads((HERE / 'metadata/ARCHIVE.json').read_text())
        if digest(source / 'SHA256SUMS') != metadata['source_sha256sums']:
            raise ValueError('Input is not the checksummed paper metadata selection')
        verify(source, source / 'SHA256SUMS', strict=True)
    else:
        unpack(source)
    if args.extract_only:
        return
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        parser.error('Use a new or empty output directory')
    command = [sys.executable, str(HERE / 'code/reproduction/analyze.py'),
               '--repo', str(HERE / 'code'), '--archive', '--input', str(source),
               '--output', str(output / 'derived')]
    subprocess.run(command, check=True)
    command = [sys.executable, str(HERE / 'code/paper_outputs.py'),
               '--derived', str(output / 'derived'), '--raw', str(source),
               '--output', str(output / 'paper')]
    if args.pdf:
        command.append('--pdf')
    subprocess.run(command, check=True)
    print(f'Completed: {output}', flush=True)


if __name__ == '__main__':
    main()
