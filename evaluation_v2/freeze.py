"""Bind the selected v2 method to complete validation before any final test call."""
import argparse
from datetime import datetime, timezone
from pathlib import Path

from evaluation.business_freeze import read, sha, dependencies, public_config
from evaluation.report_judge import save
from evaluation_v2.gate import assess
from evaluation_v2.prepare import load_cases
from evaluation_v2.run import ARMS, MODEL, method_files
from research.model_config import ROOT


STATUS = 'v2_frozen_before_final_test'
DATA_FILES = ('data/commerce_cases_v2.json','evidence/commerce_cases_v2_manifest.json','data/catalog.sqlite')


def contained(path, root=ROOT):
    path = path.resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('Evidence path leaves the project')
    return path


def validated_selection(gate_path):
    gate_path = contained(gate_path)
    gate = read(gate_path)
    if not gate.get('passed'):
        raise ValueError('V2 final test requires a passed validation gate')
    for path, expected in gate['sources_sha256'].items():
        if sha(contained(Path(path))) != expected:
            raise ValueError('Validation evidence changed')
    current = assess(Path(gate['directory']))
    ignored = {'created_at'}
    if {k:v for k,v in current.items() if k not in ignored} != {k:v for k,v in gate.items() if k not in ignored}:
        raise ValueError('Validation no longer reproduces the recorded gate')
    return gate


def create(gate_path, output, *, root=ROOT):
    if root.resolve() != ROOT.resolve():
        raise ValueError('Real method freezing uses the configured project')
    if (root/'evidence/v2_test_registration.json').exists():
        raise ValueError('The final v2 experiment is already registered')
    gate = validated_selection(gate_path)
    cases, manifest = load_cases()
    final = [c for c in cases if c['partition'] == 'test']
    if len(final) != 160 or len({c['id'] for c in final}) != 160:
        raise ValueError('All 160 final cases must be present exactly once')
    paths = set(method_files()) | {root/p for p in DATA_FILES} | {gate_path.resolve()}
    paths.update(contained(Path(p)) for p in gate['sources_sha256'])
    validation = Path(gate['directory'])
    # Include the raw reports, snapshots and model-audit responses, not only counts.
    for arm in ARMS:
        paths.update((validation/arm).glob('**/*.json'))
        audit = read(validation/arm/'audit_registration.json')
        paths.update(contained(Path(audit['directory'])).glob('**/*.json'))
    frozen = {'status':STATUS,'created_at':datetime.now(timezone.utc).isoformat(),
        'gate':gate_path.resolve().relative_to(root).as_posix(),'gate_sha256':sha(gate_path),
        'selection':'structured_multi passed the predeclared validation gate; identity_multi is the fixed comparator',
        'expected_cases_per_arm':160,'case_ids':[c['id'] for c in final],
        'case_manifest_sha256':manifest['sha256'],'model':MODEL,'arms':list(ARMS),'workers':2,
        'order_salt':'v2-final-product-interleaved-20260908',
        'files':{contained(p).relative_to(root).as_posix():sha(p) for p in sorted(paths)},
        'dependencies':dependencies(),'public_provider_config':public_config(),
        'rules':'One generated attempt per case and arm. Resume preserves complete results; interrupted attempts remain failures. Final scores cannot reselect a method.'}
    import json
    with output.open('x',encoding='utf-8') as stream:
        stream.write(json.dumps(frozen,ensure_ascii=False,indent=2)+'\n')
    return frozen


def validate(path, *, root=ROOT, check_runtime=True):
    frozen = read(path)
    if (frozen.get('status') != STATUS or frozen.get('expected_cases_per_arm') != 160
            or frozen.get('arms') != list(ARMS) or frozen.get('model') != MODEL or frozen.get('workers') != 2
            or len(frozen.get('case_ids',[])) != 160 or len(set(frozen['case_ids'])) != 160):
        raise ValueError('Invalid v2 final freeze')
    required = set(DATA_FILES) | {p.relative_to(ROOT).as_posix() for p in method_files()} | {frozen['gate']}
    if not required.issubset(frozen['files']):
        raise ValueError('Incomplete v2 frozen method')
    for name, expected in frozen['files'].items():
        if sha(contained(root/name,root)) != expected:
            raise ValueError('Frozen v2 input changed: '+name)
    if sha(root/frozen['gate']) != frozen['gate_sha256'] or not read(root/frozen['gate']).get('passed'):
        raise ValueError('Frozen selection is no longer valid')
    if check_runtime and (frozen['dependencies'] != dependencies() or frozen['public_provider_config'] != public_config()):
        raise ValueError('Frozen runtime/provider configuration changed')
    return frozen


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--gate',type=Path,default=ROOT/'evidence/v2_validation_gate.json')
    parser.add_argument('--output',type=Path,default=ROOT/'evidence/v2_final_freeze.json')
    args = parser.parse_args()
    frozen = create(args.gate,args.output)
    print({'file':str(args.output),'sha256':sha(args.output),'files':len(frozen['files'])})


if __name__ == '__main__':
    main()
