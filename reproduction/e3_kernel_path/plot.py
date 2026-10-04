"""E3 panel: paper colors and log scale, with pooled freshness counts."""
from analysis_common import rows

def plot(data, repo):
    values=rows(data/'e3_summary.csv')
    treatments=['other','fifo90','fifo90_uvc89']
    actual=sorted({r['treatment'] for r in values})
    # Campaign names, ordered from no protection to protection of both stages.
    def order(name):
        if 'uvc-fifo89' in name: return 2
        if 'fifo' in name: return 1
        return 0
    treatments=sorted(actual,key=order)
    if len(treatments)!=3 or len({order(t) for t in treatments})!=3:
        raise ValueError(f'Unknown E3 treatments: {actual}')
    text=[r'\definecolor{d435line}{HTML}{0072B2}',r'\definecolor{d455line}{HTML}{D55E00}',
          r'\begin{tikzpicture}[font=\scriptsize]',
          r'\begin{axis}[width=8cm,height=4.8cm,ymode=log,ymin=0.01,ymax=200,log origin=infty,',
          r'ybar,bar width=9pt,ymajorgrids,grid style={gray!45,dashed},axis lines=left,',
          r'ylabel={Non-fresh framesets (\%)},xlabel={xHCI / UVC copy policy},',
          r'xtick={1,2,3},xticklabels={OTHER/OTHER,FIFO 90/OTHER,FIFO 90/FIFO 89},',
          r'legend style={draw=none,at={(0.5,1)},anchor=north},legend columns=2,',
          r'nodes near coords,point meta=explicit symbolic]']
    for camera,color in [('d435','d435line'),('d455','d455line')]:
        coords=[]
        for i,t in enumerate(treatments,1):
            r=next(r for r in values if r['camera']==camera and r['treatment']==t)
            v=float(r['nonfresh_percent'])
            # Zero cannot be drawn on a log axis; label it explicitly at the floor.
            coords.append(f'({i},{max(v,0.01):.8g}) [{v:.3g}]')
        text += [r'\addplot[fill='+color+',draw='+color+'] coordinates {'+' '.join(coords)+'};',
                 r'\addlegendentry{'+camera.upper()+'}']
    text += [r'\end{axis}',r'\end{tikzpicture}']
    (data/'e3-kernel-path.tex').write_text('\n'.join(text)+'\n')
