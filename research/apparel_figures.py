"""Standalone, source-linked research chart using the bundled ReportLab charts."""
from pathlib import Path
import hashlib
import json
import math

from reportlab.graphics.shapes import Drawing, String, Rect
from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics import renderSVG, renderPDF
from reportlab.lib.colors import HexColor

ROOT = Path(__file__).resolve().parents[1]
STUDY = ROOT / 'evidence/apparel_strategy_v1/test'
DESTINATION = ROOT / 'artifacts/apparel_research_20260908'
ARMS = ('single', 'coordinator', 'on_demand')
LABELS = ('Single', 'Coordinator', 'On-demand')


def chart(drawing, x, y, width, height, data, maximum, step, color_values, fmt):
    bars = VerticalBarChart()
    bars.x, bars.y, bars.width, bars.height = x, y, width, height
    bars.data = data
    bars.valueAxis.valueMin = 0
    bars.valueAxis.valueMax = maximum
    bars.valueAxis.valueStep = step
    bars.categoryAxis.categoryNames = LABELS
    bars.categoryAxis.labels.fontName = 'Helvetica'
    bars.categoryAxis.labels.fontSize = 11
    bars.valueAxis.labels.fontName = 'Helvetica'
    bars.valueAxis.labels.fontSize = 9
    bars.barLabelFormat = fmt
    bars.barLabels.fontName = 'Helvetica'
    bars.barLabels.fontSize = 10
    for i, color in enumerate(color_values):
        bars.bars[i].fillColor = HexColor(color)
        bars.bars[i].strokeColor = None
    drawing.add(bars)


def make():
    primary_path, secondary_path = STUDY / 'summary.json', STUDY / 'task_audit_v3.json'
    primary = json.loads(primary_path.read_text(encoding='utf-8'))
    supplementary = json.loads(secondary_path.read_text(encoding='utf-8'))
    if primary['status'] != 'complete' or primary['runs'] != 324 or supplementary['status'] != 'complete':
        raise ValueError('Complete, audited results required before rendering')
    DESTINATION.mkdir(parents=True, exist_ok=True)
    drawing = Drawing(1000, 730)
    drawing.add(Rect(0, 0, 1000, 730, fillColor=HexColor('#ffffff'), strokeColor=None))
    drawing.add(String(50, 690, 'Commerce Logistics Lab: orchestration trade-offs', fontName='Helvetica-Bold', fontSize=21, fillColor=HexColor('#24392f')))
    drawing.add(String(50, 668, '108 simulated apparel tasks per strategy | 324 final runs | fixed gpt-5.6-luna', fontName='Helvetica', fontSize=12, fillColor=HexColor('#5b685e')))
    drawing.add(String(70, 640, 'Completion (%)', fontName='Helvetica-Bold', fontSize=14))
    for x, color, label in ((595, '#aeb9b2', 'Original strict checklist'), (790, '#315d4d', 'Supplementary audit')):
        drawing.add(Rect(x, 637, 11, 10, fillColor=HexColor(color), strokeColor=None))
        drawing.add(String(x + 17, 636, label, fontName='Helvetica', fontSize=10))
    chart(drawing, 70, 407, 870, 205,
          [[primary['arms'][a]['task_accuracy'] * 100 for a in ARMS],
           [supplementary['arms'][a]['task_accuracy'] * 100 for a in ARMS]], 100, 20, ['#aeb9b2', '#315d4d'], '%0.1f')
    drawing.add(String(70, 338, 'Average accounted cost (CNY / task)', fontName='Helvetica-Bold', fontSize=13))
    costs = [supplementary['arms'][a]['avg_cost_cny'] for a in ARMS]
    maximum_cost = max(.02, math.ceil(max(costs) * 100) / 100)
    chart(drawing, 70, 89, 400, 209, [costs], maximum_cost, maximum_cost / 4, ['#bb7a35'], '%0.4f')
    drawing.add(String(554, 338, 'Task latency (seconds)', fontName='Helvetica-Bold', fontSize=13))
    for x, color, label in ((769, '#668191', 'Mean'), (855, '#bfcbd1', 'P95')):
        drawing.add(Rect(x, 335, 10, 10, fillColor=HexColor(color), strokeColor=None))
        drawing.add(String(x + 16, 335, label, fontName='Helvetica', fontSize=10))
    latency = [[supplementary['arms'][a][key] for a in ARMS] for key in ('avg_latency_seconds', 'p95_latency_seconds')]
    maximum_latency = math.ceil(max(latency[1]) / 20) * 20
    chart(drawing, 554, 89, 400, 209, latency, maximum_latency, maximum_latency / 4, ['#668191', '#bfcbd1'], '%0.1f')
    drawing.add(String(50, 34, 'Synthetic business data; all scheduled failures retained. Cost includes unknown reservations and is not a supplier invoice.', fontName='Helvetica', fontSize=9, fillColor=HexColor('#5b685e')))
    drawing.add(String(50, 19, 'Supplementary definitions were set before final testing; the full report preserves the original score and paired uncertainty intervals.', fontName='Helvetica', fontSize=9, fillColor=HexColor('#5b685e')))
    path = DESTINATION / 'orchestration_comparison.svg'
    renderSVG.drawToFile(drawing, str(path))
    # Render this same vector drawing for local visual QA; no image manipulation.
    import pypdfium2 as pdfium
    document = pdfium.PdfDocument(renderPDF.drawToString(drawing))
    preview = DESTINATION / 'orchestration_comparison.png'
    page = document[0]
    bitmap = page.render(scale=1.5)
    bitmap.to_pil().save(preview)
    bitmap.close()
    page.close()
    document.close()
    sources = {'figure': str(path.relative_to(ROOT)), 'units': {'completion': 'percent of 108 scheduled tasks', 'cost': 'CNY per scheduled task', 'latency': 'seconds per scheduled task'},
               'sources_sha256': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in (primary_path, secondary_path)},
               'renderer': 'ReportLab VerticalBarChart, zero-based value axes', 'dimensions': [1000, 730]}
    (DESTINATION / 'figure_sources.json').write_text(json.dumps(sources, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'svg': str(path), 'preview': str(preview), 'sources': str(DESTINATION / 'figure_sources.json')}, ensure_ascii=False))


if __name__ == '__main__': make()
