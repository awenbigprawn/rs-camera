#!/usr/bin/env python3
"""Compile generated paper tables/figures into standalone PDFs (optional TeX)."""
import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile


def check_dependencies():
    engine=shutil.which('pdflatex')
    finder=shutil.which('kpsewhich')
    if not engine or not finder:
        raise RuntimeError('PDF rendering needs pdflatex and kpsewhich; install texlive-latex-extra texlive-pictures, or use --no-pdf')
    for package in ('standalone.cls','booktabs.sty','array.sty','pgfplots.sty'):
        found=subprocess.run([finder,package],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        if found.returncode or not found.stdout.strip():
            raise RuntimeError(f'Missing TeX package: {package}; install texlive-latex-extra texlive-pictures')
    return engine


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('directory',type=Path)
    args=ap.parse_args()
    engine=check_dependencies()
    sources=sorted(args.directory.glob('e*/*.tex'))
    if not sources: ap.error('No experiment .tex files found; pass the analysis output directory')
    for source in sources:
        with tempfile.TemporaryDirectory(prefix='reproduction-tex-') as temp:
            work=Path(temp)
            shutil.copy2(source,work/'figure.tex')
            (work/'main.tex').write_text(
                '\\documentclass[border=4pt]{standalone}\n'
                '\\usepackage{booktabs,array,pgfplots}\n'
                '\\pgfplotsset{compat=1.18}\n'
                '\\begin{document}\n'
                '\\begin{minipage}{18cm}\\centering\n'
                '\\input{figure.tex}\n'
                '\\end{minipage}\n\\end{document}\n')
            result=subprocess.run([engine,'-halt-on-error','-interaction=nonstopmode','main.tex'],cwd=work,
                                  text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
            if result.returncode:
                source.with_suffix('.render.log').write_text(result.stdout)
                raise RuntimeError(f'TeX failed; see {source.with_suffix(".render.log")}')
            shutil.copy2(work/'main.pdf',source.with_suffix('.pdf'))
            print(source.with_suffix('.pdf'))

if __name__=='__main__': main()
