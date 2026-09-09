"""Source-bound standalone chart for the completed factorial pilot."""
from pathlib import Path
import hashlib
import json
import math

from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.shapes import Drawing, String, Rect
from reportlab.graphics import renderSVG, renderPDF
from reportlab.lib.colors import HexColor

ROOT = Path(__file__).resolve().parents[1]
STUDY = ROOT / 'evidence/apparel_state_pilot_v3'
DESTINATION = ROOT / 'artifacts/apparel_state_v3_20260908'
CONDITIONS = ('neither', 'bootstrap', 'guard', 'both')
ARMS = ('single', 'coordinator', 'on_demand')
COLORS = ('#33675b', '#be813d', '#648ba7')


def chart(drawing, y, data, maximum, step, fmt):
    bars = VerticalBarChart()
    bars.x, bars.y, bars.width, bars.height = 70, y, 870, 170
    bars.data = data
    bars.valueAxis.valueMin, bars.valueAxis.valueMax, bars.valueAxis.valueStep = 0, maximum, step
    bars.categoryAxis.categoryNames = ['Neither', 'Bootstrap only', 'Guard only', 'Both']
    bars.categoryAxis.labels.fontName, bars.categoryAxis.labels.fontSize = 'Helvetica', 12
    bars.valueAxis.labels.fontName, bars.valueAxis.labels.fontSize = 'Helvetica', 10
    bars.barLabelFormat = fmt
    bars.barLabels.fontName, bars.barLabels.fontSize = 'Helvetica', 10
    for i, color in enumerate(COLORS):
        bars.bars[i].fillColor, bars.bars[i].strokeColor = HexColor(color), None
    drawing.add(bars)


def make():
    paths = [STUDY / name for name in ('comparison.json', 'qa.json')]
    comparison, qa = [json.loads(p.read_text(encoding='utf-8')) for p in paths]
    if comparison['runs'] != 144 or qa['runs'] != 144 or qa['status'] != 'passed':
        raise ValueError('Complete, verified pilot required')
    drawing = Drawing(1000, 1000)
    drawing.add(Rect(0, 0, 1000, 1000, fillColor=HexColor('#ffffff'), strokeColor=None))
    def text(x, y, value, size=12, bold=False):
        drawing.add(String(x, y, value, fontName='Helvetica-Bold' if bold else 'Helvetica',
                           fontSize=size, fillColor=HexColor('#263c34')))
    text(50, 955, 'Commerce Logistics Lab: state and action checks', 22, True)
    text(50, 931, 'Development pilot | 12 scenarios x 4 configurations x 3 strategies = 144 runs', 12)
    for x, color, label in zip((70, 335, 650), COLORS, ('Single agent', 'Coordinator + experts', 'On-demand')):
        drawing.add(Rect(x, 897, 14, 11, fillColor=HexColor(color), strokeColor=None))
        text(x+21, 896, label, 12)
    groups = comparison['groups']
    text(70, 861, 'Completed tasks / 12 (business outcome + required evidence)', 14, True)
    chart(drawing, 663, [[groups[c][a]['task_completed'] for c in CONDITIONS] for a in ARMS], 12, 3, '%0.0f')
    costs = [[groups[c][a]['avg_cost_cny'] for c in CONDITIONS] for a in ARMS]
    max_cost = max(.02, math.ceil(max(max(row) for row in costs)*100)/100)
    text(70, 602, 'Average accounted cost (CNY per scheduled task)', 14, True)
    chart(drawing, 402, costs, max_cost, max_cost/4, '%0.4f')
    latency = [[groups[c][a]['avg_latency_seconds'] for c in CONDITIONS] for a in ARMS]
    max_latency = max(10, math.ceil(max(max(row) for row in latency)/10)*10)
    text(70, 341, 'Average end-to-end latency (seconds, complete timing only)', 14, True)
    chart(drawing, 141, latency, max_latency, max_latency/4, '%0.1f')
    text(50, 80, 'All conditions share the new operation guide, schema and evidence directory. Neither is not historical v1.', 10)
    text(50, 62, '28 account-balance HTTP 403 failures + 3 process interruptions. This trial cannot establish an algorithm improvement.', 10)
    text(50, 44, 'Cost includes reservations; latency includes fast API refusals. Simulated business; no production default was changed.', 10)
    DESTINATION.mkdir(parents=True, exist_ok=True)
    vector, preview = DESTINATION / 'state_action_comparison.svg', DESTINATION / 'state_action_comparison.png'
    renderSVG.drawToFile(drawing, str(vector))
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(renderPDF.drawToString(drawing)); page = doc[0]; bitmap = page.render(scale=1.3)
    bitmap.to_pil().save(preview)
    bitmap.close(); page.close(); doc.close()
    receipt = {'figure': vector.relative_to(ROOT).as_posix(), 'preview': preview.relative_to(ROOT).as_posix(),
               'source_sha256': {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
               'renderer': 'ReportLab VerticalBarChart; all value axes start at zero',
               'units': {'completion': 'count out of 12 scheduled tasks', 'cost': 'CNY per scheduled task', 'latency': 'seconds per fully timed task; interruption censored'}}
    (DESTINATION / 'figure_sources.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'vector': str(vector), 'preview': str(preview)}, ensure_ascii=False))


if __name__ == '__main__': make()
