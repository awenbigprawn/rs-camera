#!/usr/bin/env python3
"""Freeze tracked and untracked experiment code, plus dependency commits/diffs.

Dependency source trees, built binaries, kernel images, and raw results are not
included. This bundle accompanies an already prepared rs-camera checkout.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile

HERE=Path(__file__).resolve().parent


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo',type=Path,default=HERE.parent)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--exclude',type=Path,action='append',default=[],help='Exclude a generated campaign directory')
    args=parser.parse_args();repo=args.repo.resolve();output=args.output.resolve()
    if output.exists(): raise ValueError('Refusing to replace an existing source bundle')
    excluded=[p.resolve() for p in args.exclude]
    files=set()
    extensions={'.py','.sh','.cpp','.c','.h','.hpp','.json','.txt','.md','.rst','.toml','.cmake','.patch','.config','.tex','.csv'}
    for directory in ['tools','scripts','src','include','reproduction','dependencies']:
        for path in (repo/directory).rglob('*'):
            if any(path.resolve().is_relative_to(p) for p in excluded): continue
            if any(part in {'results','runs','validation','__pycache__','.git'} or part.startswith('derived') for part in path.relative_to(repo).parts): continue
            if path.parent == repo/'reproduction/setup' and (path.name in {'config.json','inventory.json'} or path.name.startswith('config.previous-')): continue
            if path.is_file() and path.suffix in extensions: files.add(path)
    files.update(repo/name for name in ['CMakeLists.txt','.gitmodules','README.md','reproduce.sh','requirements.txt','rust-toolchain.toml'] if (repo/name).is_file())
    metadata={'source_sha256':{str(p.relative_to(repo)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)},'repositories':{}}
    output.parent.mkdir(parents=True,exist_ok=True)
    with tarfile.open(output,'w:gz') as tar:
        for path in sorted(files): tar.add(path,arcname=str(path.relative_to(repo)),recursive=False)
        for name in ['.','deps/librealsense','deps/lime-rtw','deps/benchkit','deps/ncnn']:
            path=repo/name
            if not path.is_dir(): raise ValueError(f'Missing dependency checkout {path}')
            commit=subprocess.check_output(['git','-C',str(path),'rev-parse','HEAD'],text=True).strip()
            status=subprocess.check_output(['git','-C',str(path),'status','--short'],text=True)
            metadata['repositories'][name]={'commit':commit,'status':status}
            if name!='.':
                diff=subprocess.check_output(['git','-C',str(path),'diff','--binary','HEAD'])
                info=tarfile.TarInfo('dependency-patches/'+name.replace('/','-')+'.patch');info.size=len(diff)
                tar.addfile(info,io.BytesIO(diff))
        data=(json.dumps(metadata,indent=2)+'\n').encode()
        info=tarfile.TarInfo('SOURCE_MANIFEST.json');info.size=len(data);tar.addfile(info,io.BytesIO(data))
    print(f'{output}: {len(files)} source files, SHA256 {hashlib.sha256(output.read_bytes()).hexdigest()}')

if __name__=='__main__': main()
