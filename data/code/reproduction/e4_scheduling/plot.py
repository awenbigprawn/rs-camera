"""Use the paper's native TikZ renderer for both p99 and maximum latency."""
from analysis_common import module
import sys

def plot(data, repo, repetitions=3):
    figure = module('paper_latency_figure', repo/'reproduction/e4_scheduling/tikz.py')
    figure.DATA = data
    original = sys.argv
    try:
        for metric in ('p99', 'max'):
            sys.argv = ['plot', '--metric', metric, '--output', str(data/f'e4-{metric}.tex')]
            figure.main(repetitions=repetitions)
    finally:
        sys.argv = original
