"""Export E1's family table with the paper's R/S panels and booktabs style."""
from analysis_common import rows

def span(group, key, digits):
    values=[float(r[key]) for r in group]
    a,b=(f'{v:.{digits}f}' for v in (min(values),max(values)))
    return a if a==b else a+'--'+b

def plot(data, repo):
    values=rows(data/'e1_families.csv')
    families=[('Depth/IR capture','Frame'),('Color capture','Frame'),('Application wait','Frame'),
              ('udev watcher','Timer'),('Time-difference keeper','Timer'),('Thermal monitor','Timer'),
              ('Firmware-error poller','Timer'),('Notification/pipeline dispatchers','Event/timeout')]
    text=[r'\begingroup\scriptsize',r'\setlength{\tabcolsep}{2pt}',r'\renewcommand{\arraystretch}{1.05}']
    for workload, label in [('representative30','R'),('stress60','S')]:
        text += [r'\begin{tabular}{@{}llrrrr@{}}', r'\toprule',
                 r'Worker family & Activation & $n_'+label+r'$ & $\widehat C$ ($\mu$s) & $\widehat T/\widehat\Delta$ (ms) & $U^{\mathrm{obs}}$ (\%) \\',r'\midrule']
        for family,activation in families:
            group=[r for r in values if r['workload']==workload and (r['family']==family or
                   (family.startswith('Notification/') and r['family'] in ('Notification dispatcher','Pipeline dispatcher')))]
            if not group: raise ValueError(f'Missing family {family}')
            count='5' if family.startswith('Notification/') else span(group,'instances',0)
            name=family + (' (D455 only)' if family=='Thermal monitor' else '')
            text.append(' & '.join([name,activation,count,span(group,'execution_max_us',0),
                        span(group,'interval_min_ms',2),span(group,'utilization_percent',5)])+r' \\')
        text += [r'\bottomrule',r'\end{tabular}',r'\par\smallskip']
    text += [r'\endgroup']
    (data/'e1-families.tex').write_text('\n'.join(text)+'\n')
