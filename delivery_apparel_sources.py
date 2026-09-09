"""Local entry point for the validated current-product-material policy."""
import hashlib
import json

from fastapi.responses import FileResponse, HTMLResponse

from apparel_fulfillment.interactive_sources import InteractiveSourceOperationAgent, UI_POLICY
from delivery_apparel import create_app as create_previous_app
from research.model_config import ROOT


def source_release():
    audit = json.loads((ROOT / 'evidence/apparel_source_validation_audit_20260909.json').read_text(encoding='utf-8'))
    summary = json.loads((ROOT / 'evidence/apparel_source_validation_v1/summary.json').read_text(encoding='utf-8'))
    if not audit['passed'] or not summary['engineering_gate_passed']:
        raise RuntimeError('The product-source comparison has not passed its registered checks')
    for name, expected in audit['verified_sha256'].items():
        with (ROOT / name).open('rb') as file:
            if hashlib.file_digest(file, 'sha256').hexdigest() != expected:
                raise RuntimeError('Product-source release evidence changed: ' + name)
    return {'policy_version': UI_POLICY, 'explicit_operations_only': True,
            'custom_operation_policy': 'existing_custom_workflow',
            'comparison': {name: {key: value[key] for key in ('runs', 'passed', 'cost_cny', 'mean_latency_seconds')}
                           for name, value in summary['groups'].items()},
            'report_url': '/apparel-source-research-report',
            'scope': '24 known-catalogue simulated states per arm; no real merchant success-rate claim.'}


def create_app():
    release = source_release()
    app = create_previous_app()
    app.state.apparel_jobs.state_factory = InteractiveSourceOperationAgent
    app.router.routes[:] = [r for r in app.router.routes if getattr(r, 'path', None) != '/']

    @app.get('/')
    def workspace():
        html = (ROOT / 'apparel_web/index.html').read_text(encoding='utf-8')
        link = '<p class="notice" style="max-width:1264px;margin:0 auto 20px">选择具体操作后，Agent 会核对当前商品资料，并保留读取依据。<a href="/apparel-source-research-report" target="_blank" rel="noopener">商品资料核验：48 次对照结果 ↗</a></p>'
        return HTMLResponse(html.replace('<footer>', link + '<footer>', 1))

    @app.get('/api/apparel/source-policy')
    def source_policy():
        return {**release, 'active_job_id': app.state.apparel_jobs.active}

    @app.get('/apparel-source-research-report')
    def source_report():
        return FileResponse(ROOT / 'research/APPAREL_SOURCE_REVIEW_RESULTS.md', media_type='text/markdown; charset=utf-8')

    return app


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(create_app(), host='127.0.0.1', port=5176)
