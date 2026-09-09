"""Application wiring for the measured v2 configuration; experiment files stay frozen."""
import asyncio
import hashlib
import json
from pathlib import Path

from fastapi.responses import FileResponse

from commerce_lab.skills import BASELINE
from commerce_lab.state import Store
from commerce_lab.retrieval import RerankedCatalog
from commerce_lab_v2.app import create_ui_app
from commerce_lab_v2.structured import StructuredReportAgent, REPORT_VERSION
from evaluation.business_metrics import summarize
from evaluation.report_judge import VERSION as FACT_VERSION
from evaluation_v2.communication import signature as communication_signature
from apparel_fulfillment.agent import MODEL
from research.model_config import ROOT, load_env
from delivery_ranking import install_refinement


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inside(path, root):
    path = Path(path).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('Application evidence must remain inside the project')
    return path


def release_evidence(root=ROOT, *, freeze_validator=None, input_builder=None):
    """Require passed selection and complete final evidence, without reselecting on test scores."""
    # Distribution adaptation: load the offline evaluator only when this historical gate is requested.
    from evaluation_v2.audit import campaign_items
    from evaluation_v2.freeze import validate
    freeze_path = root/'evidence/v2_final_freeze.json'
    registration_path = root/'evidence/v2_test_registration.json'
    if not freeze_path.exists() or not registration_path.exists():
        raise RuntimeError('The selected method and final experiment are not yet complete')
    frozen = (freeze_validator or validate)(freeze_path)
    registration = read(registration_path)
    if registration['status'] != 'complete' or registration['freeze_sha256'] != sha(freeze_path):
        raise RuntimeError('Wait for the single registered final experiment to finish')
    directory = inside(registration['directory'], root)
    summary = read(directory/'summary.json')
    if summary.get('status') != 'complete' or summary.get('partition') != 'test':
        raise RuntimeError('Final summary is not complete')
    expected = set(frozen['case_ids'])
    reports = {}
    sources = {str(freeze_path.relative_to(root)):sha(freeze_path),
               str(registration_path.relative_to(root)):sha(registration_path),
               str((directory/'summary.json').relative_to(root)):sha(directory/'summary.json')}
    for arm in frozen['arms']:
        folder = directory/arm
        config, rows = read(folder/'config.json'), read(folder/'results.json')
        if (config['model'] != MODEL or config['freeze_sha256'] != sha(freeze_path)
                or len(rows) != 160 or {r['case_id'] for r in rows} != expected
                or len(config['case_ids']) != 160 or set(config['case_ids']) != expected):
            raise ValueError('Final application evidence differs from the selected configuration')
        if summary['arms'][arm] != summarize(rows):
            raise ValueError('Final business summary differs from its individual results')
        audit_path = folder/'audit_registration.json'
        if not audit_path.exists():
            raise RuntimeError('Both complete final report audits are required before application activation')
        registration = read(audit_path)
        if registration['status'] != 'complete':
            raise RuntimeError('Both complete final report audits are required before application activation')
        ad = inside(registration['directory'],root)
        audit_summary, audit_rows = read(ad/'summary.json'), read(ad/'results.json')
        if (sha(ad/'summary.json') != registration['summary_sha256']
                or audit_summary['source_results_sha256'] != sha(folder/'results.json')
                or audit_summary['scheduled'] != 160 or len(audit_rows) != 160
                or {r['id'] for r in audit_rows} != expected
                or audit_summary['judge_model'] != 'qwen3.8-max'
                or audit_summary['facts_version'] != FACT_VERSION
                or audit_summary['communication'] != communication_signature()):
            raise ValueError('Final report audit identity or completeness changed')
        original_inputs, _ = (input_builder or campaign_items)(folder)
        if read(ad/'inputs.json') != original_inputs:
            raise ValueError('Final audit inputs differ from the actual run evidence')
        facts = sum(r.get('facts',{}).get('decision',{}).get('verdict')=='supported' for r in audit_rows)
        communication = sum(bool(r.get('communication',{}).get('passed')) for r in audit_rows)
        if (audit_summary['facts_supported'] != facts or audit_summary['communication_passed'] != communication):
            raise ValueError('Final report counts differ from original judgments')
        reports[arm] = {'scheduled':160,'facts_supported':facts,'communication_passed':communication}
        for path in (folder/'results.json',audit_path,ad/'summary.json',ad/'results.json'):
            sources[str(path.relative_to(root))] = sha(path)
    return {'status':'validation_passed_final_measured','model':MODEL,'selected_arm':'structured_multi',
            'report_version':REPORT_VERSION,'final_directory':str(directory.relative_to(root)),
            'business':summary['arms'],'report_audits':reports,'sources_sha256':sources,
            'scope':'Known-product shared-template simulated tasks; not unrestricted shopping-dialogue or real-order certification.'}


def run_selected(session_id, task, run_id=None, *, store, model=MODEL, client=None):
    if model != MODEL:
        run_id = run_id or store.new_run(session_id,task)
        result = {'run_id':run_id,'error_type':'ConfigurationMismatch',
                  'error':'This measured configuration uses '+MODEL+'. No substitute model was called.',
                  'model_calls':0,'estimated_settled_cost_cny':'0'}
        store.trace(run_id,'host','run_failure',result)
        store.update_run(run_id,'failed',result)
        return result
    client_kwargs={'client':client} if client is not None else {}
    return asyncio.run(StructuredReportAgent(store=store,model=MODEL,policy=BASELINE,topology='multi',**client_kwargs)
                       .run(session_id,task,run_id))


def create_delivery_app(store=None, *, testing=False, runner=run_selected, evidence_loader=release_evidence, refiner=None):
    certification = evidence_loader()
    source_hashes = certification.get('sources_sha256',{})
    public_certification = {key:value for key,value in certification.items() if key!='sources_sha256'}
    public_certification.update(source_file_count=len(source_hashes),
        source_manifest_sha256=hashlib.sha256(json.dumps(source_hashes,sort_keys=True,separators=(',',':')).encode()).hexdigest())
    store = store or Store(catalog=RerankedCatalog())
    app = create_ui_app(store,testing=testing,runner=runner)
    install_refinement(app,store,**({'refiner':refiner} if refiner is not None else {}))
    base_status = next(r.endpoint for r in app.router.routes if getattr(r,'path',None)=='/api/status')
    app.router.routes[:] = [r for r in app.router.routes if getattr(r,'path',None) not in {'/','/api/status'}]

    @app.get('/')
    def index():
        return FileResponse(ROOT/'delivery_web/index.html')

    @app.get('/api/status')
    def status():
        data = base_status()
        data.update(models=[MODEL],evaluation_status=certification['status'],
                    execution_model=MODEL,report_version=REPORT_VERSION,evaluation=public_certification)
        return data

    @app.get('/api/evaluation/evidence')
    def evaluation_evidence():
        return certification

    return app


if __name__ == '__main__':
    import uvicorn
    config = load_env()
    uvicorn.run(create_delivery_app(),host='127.0.0.1',port=int(config.get('COMMERCE_PORT',config.get('PORT','5174'))))
