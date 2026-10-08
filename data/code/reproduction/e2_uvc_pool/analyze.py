"""Experiment analysis; reuse the benchmark parsers and acceptance checks."""
from analysis_common import *

def analyze(repo, roots, out, artifacts, helper, archive, sections):
    tool=repo/"tools/realsense_steady_bench"
    if 'diagnosis' in sections:
        paths=sorted(roots['diagnosis'].rglob('freshness_path_correlations.csv'))
        if not paths: raise ValueError('No receive-path diagnostic correlation artifact')
        data=[]
        for path in paths:
            artifacts.add(path)
            data.extend({'source':str(path),**r} for r in rows(path))
        if data: write(out/'diagnosis_correlations.csv',data)
        (out/'diagnosis.json').write_text(json.dumps({'correlation_files':len(paths),'events':len(data)},indent=2)+'\n')

    if 'e2' in sections:
        helper.OUTPUT=out
        helper.UVC_POOL_CELLS={5:roots['e2']/'formal/d1-uvc5-standard-hardirq',16:roots['e2']/'formal/d1-h1-uvc16-standard-hardirq'}
        helper.prepare_uvc(artifacts)
