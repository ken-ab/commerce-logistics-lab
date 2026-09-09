"""Local proposal-review release; refuse activation without reconciled evidence."""
import hashlib
import json

from fastapi.responses import FileResponse, HTMLResponse

from apparel_fulfillment.interactive_reliability import UI_POLICY, scoped_operation_agent
from delivery_apparel_sources import create_app as create_previous_app
from research.model_config import ROOT


def reliability_release():
    audit_path = ROOT / 'evidence/apparel_reliability_audit_20260909.json'
    summary_path = ROOT / 'evidence/apparel_reliability_study_v1/summary.json'
    audit = json.loads(audit_path.read_text(encoding='utf-8'))
    summary = json.loads(summary_path.read_text(encoding='utf-8'))
    required_gate = {'minimum_each_arm', 'no_arm_acceptance_regression', 'no_protected_violations',
                     'total_cost_limit', 'latency_limit_each_arm', 'program_comparison_correct'}
    if not (audit['audit_completed'] and audit['business_and_ledger_reconciled']
            and audit['all_program_checks_passed'] and audit['engineering_gate_passed']
            and set(audit['engineering_gate']) == required_gate and all(audit['engineering_gate'].values())):
        raise RuntimeError('Proposal reliability has not passed its registered engineering gate')
    conditions = {v + '_' + arm for v in ('v5', 'v6') for arm in ('single', 'coordinator', 'on_demand')}
    if set(summary['groups']) != conditions or any(g['runs'] != 24 for g in summary['groups'].values()):
        raise RuntimeError('The registered 144 first attempts are incomplete')
    files = {**audit['verified_sha256'], **audit['post_audit_snapshot_sha256'],
             'research/audit_apparel_reliability_study.py': audit['audit_source_sha256']}
    for name, expected in files.items():
        with (ROOT / name).open('rb') as handle:
            if hashlib.file_digest(handle, 'sha256').hexdigest() != expected:
                raise RuntimeError('Proposal reliability evidence changed: ' + name)
    return {'policy_version': UI_POLICY, 'explicit_operations': ['review_proposal'],
            'other_explicit_operations': 'apparel-interactive-product-sources-v1',
            'custom_operation_policy': 'existing_custom_workflow',
            'engineering_gate': audit['engineering_gate'],
            'comparison': {name: {key: value[key] for key in ('runs', 'passed', 'cost_cny', 'mean_latency_seconds')}
                           for name, value in summary['groups'].items()},
            'report_url': '/apparel-reliability-research-report',
            'scope': '24 developer-authored simulated states per version and arm; no real merchant accuracy claim.'}


def create_app():
    release = reliability_release()
    app = create_previous_app()
    app.state.apparel_jobs.state_factory = scoped_operation_agent
    app.router.routes[:] = [r for r in app.router.routes if getattr(r, 'path', None) != '/']

    @app.get('/')
    def workspace():
        html = (ROOT / 'apparel_web/index.html').read_text(encoding='utf-8')
        notice = ('<p class="notice" style="max-width:1264px;margin:0 auto 20px">'
                  '复核提案时，可查看新旧运输班次、事件影响和等待时间的程序对照。'
                  '<a href="/apparel-reliability-research-report" target="_blank" rel="noopener">'
                  '提案复核：144 次对照结果 ↗</a> · '
                  '<a href="/apparel-source-research-report" target="_blank" rel="noopener">商品资料核验结果 ↗</a></p>')
        html = html.replace('<footer>', notice + '<footer>', 1)
        html = html.replace('<script src="/apparel.js"></script>',
                            '<script src="/apparel.js"></script><script src="/apparel-reliability.js"></script>', 1)
        return HTMLResponse(html)

    @app.get('/apparel-reliability.js')
    def reliability_script():
        return FileResponse(ROOT / 'apparel_web/reliability.js', media_type='application/javascript')

    @app.get('/api/apparel/reliability-policy')
    def reliability_policy():
        return {**release, 'active_job_id': app.state.apparel_jobs.active}

    @app.get('/apparel-reliability-research-report')
    def reliability_report():
        return FileResponse(ROOT / 'research/APPAREL_RELIABILITY_RESULTS.md', media_type='text/markdown; charset=utf-8')

    return app


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(create_app(), host='127.0.0.1', port=5176)
