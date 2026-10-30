from ..config import group_names,group_labels
from html import escape
from ..io import read_csv,read_json,atomic_text


def build(exp,config):
    rows=read_csv(exp/'results/survival.csv');counts={g:n for g,n in read_json(exp/'results/denominators.json').items() if g in group_names(config)}
    labels=group_labels(config)
    colors=['#2563eb','#dc2626','#059669','#9333ea','#d97706','#0891b2']
    width,height=1000,550;left,top,right,bottom=75,60,960,430
    maximum=max(int(r['index']) for r in rows) or 1
    svg=[f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-label="Frozen-test survival by commit">', '<rect width="100%" height="100%" fill="white"/>','<g font-family="sans-serif" font-size="14" fill="#172033">',f'<text x="{left}" y="30" font-size="22">Frozen-test survival after {escape(config["anchor"])}</text>']
    for n in range(0,101,20):
        y=bottom-(bottom-top)*n/100
        svg += [f'<path d="M {left} {y} H {right}" stroke="#dde3eb"/>',f'<text x="{left-12}" y="{y+5}" text-anchor="end">{n}%</text>']
    svg += [f'<text x="500" y="465" text-anchor="middle">First-parent commit index</text>']
    for n in range(6):
        index=round(maximum*n/5);x=left+(right-left)*index/maximum
        svg.append(f'<text x="{x}" y="450" text-anchor="middle">{index}</text>')
    for number,(g,denom) in enumerate(counts.items()):
        color=colors[number%len(colors)]
        if denom:
            path=[]
            for i,row in enumerate(rows):
                x=left+(right-left)*int(row['index'])/maximum;y=bottom-(bottom-top)*int(row[g+'_alive'])/denom
                path.append(f'M {x} {y}' if i==0 else f'H {x} V {y}')
            svg.append(f'<path d="{" ".join(path)}" fill="none" stroke="{color}" stroke-width="2.5"/>')
        x=left+(number%3)*300;y=495+(number//3)*25
        svg.append(f'<text x="{x}" y="{y}" fill="{color}">{escape(labels[g])} (n={denom}{"; unavailable" if not denom else ""})</text>')
    svg.append('</g></svg>');atomic_text(exp/'results/figure1-survival-by-commit.svg','\n'.join(svg)+'\n')


def png(exp,config):
    from pathlib import Path
    from ..adapters.java_maven import JavaMavenAdapter
    from ..io import write_json
    adapter=JavaMavenAdapter(exp,config)
    helper=Path(__file__).with_name('RenderFigure.java')
    result=adapter.runner.execute(['java','-Djava.awt.headless=true',str(helper),str(exp/'results/figure1-survival-by-commit.svg'),str(exp/'results/figure1-survival-by-commit.png')],adapter.folder(0),exp/'results/figure-render.log',config['runner']['timeouts']['compile'])
    write_json(exp/'results/figure-render.json',result)
    return result['exit_code']==0
