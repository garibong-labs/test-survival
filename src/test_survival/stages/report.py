from ..report import tables,figures,consistency
from ..io import atomic_text


def run(exp,config):
    tables.build(exp,config);figures.build(exp,config);png=figures.png(exp,config);summary=consistency.build(exp,config)
    path=exp/'README.md';text=path.read_text() if path.exists() else '# '+config['name']+'\n'
    start='<!-- survival-report:start -->';end='<!-- survival-report:end -->'
    section=start+'\n\n## Experiment results\n\nDenominators: '+', '.join(f'{g}: {n}' for g,n in summary['denominators'].items())+'.\n\nFinal survivors: '+', '.join(f'{g}: {n}' for g,n in summary['final_survivors'].items())+'.\n\n[Survival](results/table1-survival-by-event.md) · [Changed-line coverage](results/table3-changed-line-coverage.md) · [Consistency](results/consistency.md) · [Figure](results/figure1-survival-by-commit.svg)\n\n'+end
    if summary.get('notes'):
        section=section.replace(end,'\n\n'.join(summary['notes'])+'\n\n'+end)
    if start in text and end in text:
        text=text[:text.index(start)]+section+text[text.index(end)+len(end):]
    else:text=text.rstrip()+'\n\n'+section+'\n'
    atomic_text(path,text)
    from ..generators.completion import record
    record(exp,config,summary)
    return 'Tables, SVG'+(' and PNG' if png else ' (PNG unavailable; see figure-render.log)')+', consistency checks, and final summary written'
