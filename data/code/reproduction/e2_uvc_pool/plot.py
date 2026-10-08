"""E2 panel: paper colors and categories, using newly measured counts."""
from analysis_common import rows
import math

def plot(data, repo):
    values = {(r['fps'], int(r['urbs'])): r for r in rows(data/'uvc_pool.csv')}
    groups = [(fps, field) for fps in ('30 FPS', '60 FPS')
              for field in ('partially_stale', 'framesets_with_gaps')]
    maximum = max(int(values[fps, u][field]) for fps, field in groups for u in (5,16))
    top = max(14, math.ceil(maximum/2)*2)
    text = [r'\definecolor{uvcfive}{HTML}{999999}', r'\definecolor{uvcsixteen}{HTML}{0072B2}',
            r'\begin{tikzpicture}[font=\scriptsize]',
            r'\begin{axis}[width=8cm,height=4.8cm,ymin=0,ymax='+str(top*1.25)+r',',
            r'ybar,bar width=7pt,ymajorgrids,grid style={gray!45,dashed},',
            r'axis lines=left,ylabel={Problematic non-fresh framesets},',
            r'xtick={1,2,3,4},xticklabels={30 FPS Stale,30 FPS Gap,60 FPS Stale,60 FPS Gap},',
            r'x tick label style={rotate=20,anchor=east},legend style={draw=none,at={(0.5,1)},anchor=north},legend columns=2]']
    for u, color in ((5,'uvcfive'),(16,'uvcsixteen')):
        coords=' '.join(f'({i},{values[fps,u][field]})' for i,(fps,field) in enumerate(groups,1))
        text += [r'\addplot[fill='+color+',draw='+color+r'] coordinates {'+coords+'};', r'\addlegendentry{'+str(u)+' URBs}']
    text += [r'\end{axis}',r'\end{tikzpicture}']
    (data/'e2-uvc-pool.tex').write_text('\n'.join(text)+'\n')
