"""Freeze the replication before validation, then bind passed selection before test."""
from datetime import datetime, timezone
import json
from pathlib import Path

from audit_replication.prepare import DATA, MANIFEST, load_cases
from evaluation.business_freeze import read, sha, dependencies, public_config
from evaluation_v2.run import method_files, ARMS, MODEL
from research.model_client import BudgetedChatClient
from research.model_config import ROOT

METHOD = ROOT/'evidence/audit_replication_method.json'
FINAL_FREEZE = ROOT/'evidence/audit_replication_final_freeze.json'
GATE = ROOT/'evidence/audit_replication_validation_gate.json'


def write_once(path, value):
    with path.open('x',encoding='utf-8') as handle:
        json.dump(value,handle,ensure_ascii=False,indent=2)
        handle.write('\n')


def checked_path(path):
    path = Path(path).resolve()
    if not path.is_relative_to(ROOT.resolve()):
        raise ValueError('Replication evidence must remain inside this project')
    return path


def calibration():
    reg = read(ROOT/'evidence/audit_transport_calibration_registration.json')
    folder = checked_path(reg['directory'])
    summary, rows = read(folder/'summary.json'),read(folder/'results.json')
    if (reg['status']!='complete' or reg['summary_sha256']!=sha(folder/'summary.json')
            or summary.get('passed') is not True or len(rows)!=41
            or len({r['id'] for r in rows})!=41
            or not all(r.get('agrees') is True and r.get('transport_attempts')==1 for r in rows)):
        raise ValueError('The one fixed transport calibration must pass completely')
    config = read(folder/'config.json')
    if summary['config_sha256']!=sha(folder/'config.json') or config['inputs_sha256']!=sha(folder/'inputs.json'):
        raise ValueError('Calibration inputs/configuration changed')
    if any(sha(ROOT/path)!=value for path,value in config['files'].items()):
        raise ValueError('Calibrated evaluator or transport changed')
    return folder


def create():
    if METHOD.exists():
        raise ValueError('Replication method is already frozen')
    folder = calibration()
    cases,manifest = load_cases()
    if {part:sum(c['partition']==part for c in cases) for part in ('validation','test')} != {'validation':32,'test':160}:
        raise ValueError('Incorrect registered study size')
    paths = set(method_files())
    paths.update((ROOT/'audit_replication').glob('*.py'))
    paths.update((ROOT/'audit_reliability').glob('*.py'))
    paths.update(folder.glob('**/*.json'))
    paths.update({DATA,MANIFEST,ROOT/'data/catalog.sqlite',ROOT/'audit_replication/PROTOCOL.md',
        ROOT/'evidence/audit_transport_calibration_registration.json',ROOT/'evidence/v2_validation_gate.json',
        ROOT/'data/commerce_cases_v2.json',ROOT/'evidence/commerce_cases_v2_manifest.json'})
    frozen = {'status':'frozen_before_independent_replication','created_at':datetime.now(timezone.utc).isoformat(),
        'files':{checked_path(p).relative_to(ROOT).as_posix():sha(p) for p in sorted(paths)},
        'case_manifest_sha256':manifest['sha256'],'model':MODEL,'arms':list(ARMS),'workers':2,
        'case_ids':{part:[c['id'] for c in cases if c['partition']==part] for part in ('validation','test')},
        'order_salt':'audit-replication-paired-order-20260908','maximum_increment_cny':'75',
        'starting_global_budget':BudgetedChatClient().ledger.summary(),
        'dependencies':dependencies(),'public_provider_config':public_config(),
        'original_v2_gate_passed':read(ROOT/'evidence/v2_validation_gate.json')['passed'],
        'scope':'One prospective independent replication after transport repair; no alteration of the original failed gate.'}
    write_once(METHOD,frozen)
    return frozen


def validate():
    frozen = read(METHOD)
    if (frozen.get('status')!='frozen_before_independent_replication' or frozen.get('model')!=MODEL
            or frozen.get('arms')!=list(ARMS) or frozen.get('workers')!=2):
        raise ValueError('Invalid replication method identity')
    for name,value in frozen['files'].items():
        if sha(checked_path(ROOT/name))!=value:
            raise ValueError('Frozen replication input changed: '+name)
    if frozen['dependencies']!=dependencies() or frozen['public_provider_config']!=public_config():
        raise ValueError('Frozen replication runtime/provider configuration changed')
    cases,manifest = load_cases()
    if manifest['sha256']!=frozen['case_manifest_sha256'] or any(
        [c['id'] for c in cases if c['partition']==part]!=ids for part,ids in frozen['case_ids'].items()):
        raise ValueError('Replication case registration changed')
    return frozen


def final_freeze():
    from audit_replication.assess import assess
    validate()
    gate = read(GATE)
    reproduced = assess()
    if not gate.get('passed') or reproduced!=gate:
        raise ValueError('Independent validation did not pass reproducibly; final test remains sealed')
    if FINAL_FREEZE.exists():
        validate_final()
        return read(FINAL_FREEZE)
    directory = checked_path(gate['directory'])
    paths = {METHOD,GATE} | set(directory.glob('**/*.json'))
    frozen = {'status':'replication_final_frozen_before_test','method_sha256':sha(METHOD),
        'gate_sha256':sha(GATE),'created_at':datetime.now(timezone.utc).isoformat(),
        'files':{p.relative_to(ROOT).as_posix():sha(p) for p in sorted(paths)},
        'selection':'structured_multi selected by the sole independent validation; final results cannot reselect it'}
    write_once(FINAL_FREEZE,frozen)
    return frozen


def validate_final():
    validate()
    frozen = read(FINAL_FREEZE)
    if (frozen.get('status')!='replication_final_frozen_before_test' or frozen['method_sha256']!=sha(METHOD)
            or frozen['gate_sha256']!=sha(GATE) or not read(GATE).get('passed')):
        raise ValueError('Final freeze does not bind a passed replication validation')
    for name,value in frozen['files'].items():
        if sha(checked_path(ROOT/name))!=value:
            raise ValueError('Final frozen selection evidence changed: '+name)
    return frozen


if __name__=='__main__':
    value = create()
    print(json.dumps({'file':str(METHOD),'files':len(value['files']),'model':value['model']},indent=2))
