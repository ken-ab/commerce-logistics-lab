"""Export a source-bound scientific chart of the completed new-order replication."""
from pathlib import Path
import hashlib
import json
import math

from reportlab.graphics import renderSVG, renderPDF
from reportlab.graphics.shapes import Drawing, String, Rect
from reportlab.lib.colors import HexColor
import pypdfium2 as pdfium

from research.apparel_state_figures_v3 import chart, CONDITIONS, ARMS, COLORS

ROOT=Path(__file__).resolve().parents[1]
STUDY=ROOT/'evidence/apparel_state_replication_v3'
DESTINATION=ROOT/'artifacts/apparel_state_replication_20260908'


def make():
    paths=[STUDY/'comparison.json', STUDY/'qa.json', ROOT/'research/apparel_state_figures_v3.py', Path(__file__)]
    comparison=json.loads(paths[0].read_text(encoding='utf-8'))
    qa=json.loads(paths[1].read_text(encoding='utf-8'))
    assert comparison['runs']==qa['runs']==144 and qa['status']=='passed'
    groups=comparison['groups']
    drawing=Drawing(1000,1000)
    drawing.add(Rect(0,0,1000,1000,fillColor=HexColor('#ffffff'),strokeColor=None))
    def text(x,y,value,size=12,bold=False):
        drawing.add(String(x,y,value,fontName='Helvetica-Bold' if bold else 'Helvetica',fontSize=size,fillColor=HexColor('#263c34')))
    text(50,955,'Commerce Logistics Lab: new order-state replication',21,True)
    text(50,931,'12 new simulated orders x 4 configurations x 3 strategies | Known catalogue and task families',12)
    for x,color,label in zip((70,335,650),COLORS,('Single agent','Coordinator + experts','On-demand')):
        drawing.add(Rect(x,897,14,11,fillColor=HexColor(color),strokeColor=None))
        text(x+21,896,label)
    text(70,861,'Completed tasks / 12 (correct business state + required evidence)',14,True)
    chart(drawing,663,[[groups[c][a]['task_completed'] for c in CONDITIONS] for a in ARMS],12,3,'%0.0f')
    costs=[[groups[c][a]['avg_cost_cny'] for c in CONDITIONS] for a in ARMS]
    maximum=max(.02,math.ceil(max(max(row) for row in costs)*100)/100)
    text(70,602,'Average accounted cost (CNY per scheduled task)',14,True)
    chart(drawing,402,costs,maximum,maximum/4,'%0.4f')
    latency=[[groups[c][a]['avg_latency_seconds'] for c in CONDITIONS] for a in ARMS]
    maximum=max(10,math.ceil(max(max(row) for row in latency)/10)*10)
    text(70,341,'Average end-to-end latency (seconds per scheduled task)',14,True)
    chart(drawing,141,latency,maximum,maximum/4,'%0.1f')
    effect=comparison['factorial_effects']['effects']['both_minus_neither']['task_accuracy']
    lo,hi=effect['paired_scenario_95_interval']
    text(50,80,f'Both minus neither: {100*effect["difference"]:+.2f} pp; paired-scenario 95% interval [{100*lo:+.2f}, {100*hi:+.2f}]. Small-sample description.',10)
    reports=sum(comparison['totals'][c]['completed_reports'] for c in CONDITIONS)
    text(50,62,f'Completed model reports: {reports}/144. All failures retained. All conditions share the new guide, schema and evidence directory.',10)
    text(50,44,'Cost includes reservations; latency is not pure thinking time. Simulated business, zero real users. No automatic default change.',10)
    DESTINATION.mkdir(parents=True,exist_ok=True)
    svg=DESTINATION/'state_replication.svg';png=DESTINATION/'state_replication.png';pdf=DESTINATION/'state_replication.pdf'
    renderSVG.drawToFile(drawing,str(svg));renderPDF.drawToFile(drawing,str(pdf))
    doc=pdfium.PdfDocument(pdf);page=doc[0];bitmap=page.render(scale=1.3);bitmap.to_pil().save(png)
    bitmap.close();page.close();doc.close()
    (DESTINATION/'sources.json').write_text(json.dumps({'sources_sha256':{p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
        'renderer':'ReportLab grouped bars; zero-based quantitative axes','files':[str(p.relative_to(ROOT)) for p in (svg,png,pdf)]},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'preview':str(png),'vector':str(svg),'pdf':str(pdf)},ensure_ascii=False))


if __name__=='__main__':make()
