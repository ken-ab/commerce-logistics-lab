"""Local app entry point for the independently validated replication, when complete."""
from pathlib import Path

from audit_replication.assess import read_phase
from audit_replication.method import validate_final
from commerce_lab_v2.structured import REPORT_VERSION
from delivery import create_delivery_app, read, sha, run_selected
from delivery_budget import business_client
from delivery_selected_ranking import refine, verified_selection
from evaluation_v2.run import MODEL
from research.model_config import ROOT, load_env


def release_evidence(root=ROOT, *, final_validator=None, phase_reader=None):
    freeze = root/'evidence/audit_replication_final_freeze.json'
    registration_path = root/'evidence/audit_replication_test_registration.json'
    if not freeze.exists() or not registration_path.exists():
        raise RuntimeError('Independent validation and all final trials must finish before application activation')
    (final_validator or validate_final)()
    registration = read(registration_path)
    if registration.get('status')!='complete' or registration.get('final_freeze_sha256')!=sha(freeze):
        raise RuntimeError('The registered final experiment is not complete or does not match its freeze')
    phase = (phase_reader or read_phase)('test')
    if (phase['scheduled_per_arm']!=160 or set(phase['arms'])!={'identity_multi','structured_multi'}
            or Path(phase['directory']).resolve()!=Path(registration['directory']).resolve()):
        raise ValueError('Final evidence does not contain the complete registered paired comparison')
    for arm,result in phase['arms'].items():
        if len(result['rows'])!=160 or result['business_summary']['cases']!=160:
            raise ValueError('A final comparison arm is incomplete')
    sources = dict(phase['sources_sha256'])
    sources['evidence/audit_replication_final_freeze.json'] = sha(freeze)
    sources['evidence/audit_replication_test_registration.json'] = sha(registration_path)
    return {'status':'independent_validation_passed_final_measured','model':MODEL,'selected_arm':'structured_multi',
        'report_version':REPORT_VERSION,'final_directory':str(Path(phase['directory']).relative_to(root)),
        'business':{arm:item['business_summary'] for arm,item in phase['arms'].items()},
        'report_audits':{arm:{'scheduled':160,**item['counts'],
            'critical_issues':len(item['critical_issues'])} for arm,item in phase['arms'].items()},
        'sources_sha256':sources,'original_v2_gate_passed':False,
        'scope':'Passed independent validation after transport repair; complete final synthetic shared-template experiment. '
                'Final failures remain in reported denominators; this is not real-order certification.'}


def run_operational(session_id, task, run_id=None, *, store, model=MODEL):
    return run_selected(session_id,task,run_id,store=store,model=model,client=business_client())


def create_app():
    ranking=verified_selection()
    app=create_delivery_app(evidence_loader=release_evidence,runner=run_operational,refiner=refine)

    @app.get('/api/ranking/selection')
    def ranking_selection():
        return ranking

    return app


if __name__=='__main__':
    import uvicorn
    config = load_env()
    uvicorn.run(create_app(),host='127.0.0.1',port=int(config.get('COMMERCE_PORT',config.get('PORT','5174'))))
