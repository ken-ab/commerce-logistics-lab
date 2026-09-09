"""Freeze a passed candidate or an explicitly rejected diagnostic comparison."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import sys

from commerce_lab.skills import SkillRegistry
from commerce_lab.state import Store
from evaluation.skill_gate import BUSINESS_FILES, assess
from research.model_config import ROOT, load_env

METHOD_FILES = (*BUSINESS_FILES, 'evaluation/run_campaign.py', 'evaluation/run_final.py',
    'evaluation/business_freeze.py', 'evaluation/business_metrics.py', 'evaluation/skill_gate.py',
    'evaluation/report_judge.py', 'research/BUSINESS_FINAL_PROTOCOL.md', 'research/SKILL_EXPERIMENT_PROTOCOL.md',
    'research/rate_card.json', 'research/budget_policy.json', 'data/commerce_cases_v1.json',
    'evidence/commerce_cases_manifest.json', 'data/catalog.sqlite')
INPUT_NAMES = ('baseline_dev','candidate_dev','baseline_val','candidate_val','audit','calibration','dev_audit')


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path):
    value = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda:source.read(1024*1024),b''):
            value.update(block)
    return value.hexdigest()


def dependencies():
    return {'python':sys.version, 'packages':dict(sorted(
        (d.metadata['Name'].lower().replace('_','-'),d.version) for d in importlib.metadata.distributions()))}


def public_config():
    values=load_env()
    # Explicit allowlist. Credentials and arbitrary environment fields never enter evidence.
    return {k:values.get(k) for k in ('AIHUBMIX_BASE_URL','AIHUBMIX_HTTP_PROXY_MODE',
        'DASHSCOPE_TEXT_BASE_URL','DASHSCOPE_HTTP_PROXY_MODE','COMMERCE_API_BUDGET_CNY')}


def check_selection(gate, active, *, include_rejected=False):
    if gate['eligible']:
        if active != gate['candidate_policy']:
            raise ValueError('The passed candidate must be the validated active policy')
        return {'status':'passed_and_active','eligible_for_activation':True}
    if not include_rejected:
        raise ValueError('Rejected candidates require an explicit diagnostic comparison')
    # This does not relax promotion. It permits measuring the terminal candidate
    # after the declared search budget, while leaving the original policy active.
    if not gate['reasons'] or any(not r.startswith('Unresolved or unsupported report claims:') for r in gate['reasons']):
        raise ValueError('Diagnostic comparison still requires complete, matched business and audit evidence')
    if active['id'] != gate['baseline_id'] or active == gate['candidate_policy']:
        raise ValueError('The rejected candidate must remain inactive; baseline must remain active')
    return {'status':'rejected_candidate_diagnostic','eligible_for_activation':False,
        'known_rejection_reasons':gate['reasons'],
        'selection_rationale':'Terminal sixth candidate after the predeclared six-candidate search limit; final test results cannot change selection or authorize activation.'}


def validate(path, *, root=ROOT, check_runtime=True):
    frozen=read(path)
    if frozen.get('status')!='frozen_before_final_business_test' or frozen.get('expected_cases_per_arm')!=160:
        raise ValueError('Invalid business test freeze')
    if not set(METHOD_FILES).issubset(frozen['files']):
        raise ValueError('Incomplete business method freeze')
    for relative,expected in frozen['files'].items():
        target=(root/relative).resolve()
        if not target.is_relative_to(root.resolve()) or sha(target)!=expected:
            raise ValueError('Frozen business input changed: '+relative)
    if check_runtime and (frozen['dependencies']!=dependencies() or frozen['public_provider_config']!=public_config()):
        raise ValueError('Frozen runtime or provider configuration changed')
    return frozen


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--gate',required=True,type=Path)
    parser.add_argument('--include-rejected-candidate',action='store_true')
    parser.add_argument('--output',type=Path,default=ROOT/'evidence/business_final_freeze.json')
    args=parser.parse_args()
    gate=read(args.gate)
    if set(gate.get('inputs',{}))!=set(INPUT_NAMES):
        raise ValueError('A complete skill gate is required')
    for relative,expected in gate['files'].items():
        if sha(ROOT/relative)!=expected:
            raise ValueError('Gate evidence changed: '+relative)
    reassessed=assess(*(Path(gate['inputs'][n]) for n in INPUT_NAMES))
    if (reassessed['eligible']!=gate['eligible'] or reassessed['reasons']!=gate['reasons']
            or reassessed['candidate_policy']!=gate['candidate_policy']):
        raise ValueError('Current evidence no longer matches the recorded skill gate')
    selection=check_selection(gate,SkillRegistry(Store()).active(),include_rejected=args.include_rejected_candidate)
    if (ROOT/'evidence/business_final_registration.json').exists():
        raise ValueError('A final business experiment is already registered')
    cases=read(ROOT/'data/commerce_cases_v1.json')['cases']
    final=[c for c in cases if c['partition']=='test']
    if len(final)!=160 or len({c['id'] for c in final})!=160:
        raise ValueError('Expected all 160 sealed cases exactly once per arm')
    files=set(METHOD_FILES)|set(gate['files'])|{args.gate.resolve().relative_to(ROOT).as_posix()}
    history=sorted((ROOT/'evidence/skills').glob('trace-skill-*/policy.json'))
    if args.include_rejected_candidate and (len(history)!=6 or not any(read(p)==gate['candidate_policy'] for p in history)):
        raise ValueError('The declared six-candidate search history must be preserved')
    for path in history:
        files.add(path.relative_to(ROOT).as_posix())
        files.add((path.parent/'origin.json').relative_to(ROOT).as_posix())
    # Editable upstream distributions require source hashes, not only version strings.
    for package in ('tau2','commerce_common','shopping_agent'):
        module=__import__(package)
        source_root=Path(module.__file__).resolve().parent
        for source in source_root.rglob('*.py'):
            files.add(source.relative_to(ROOT).as_posix())
    baseline=read(Path(gate['inputs']['baseline_val'])/'config.json')['policy']
    model=read(Path(gate['inputs']['candidate_val'])/'config.json')['model']
    frozen={'status':'frozen_before_final_business_test','created_at':datetime.now(timezone.utc).isoformat(),
        'expected_cases_per_arm':160,'model':model,'judge_model':read(Path(gate['inputs']['calibration'])/'config.json')['model'],
        'candidate_selection':selection,
        'arms':{'baseline_multi':{'policy':baseline,'topology':'multi'},
            'candidate_multi':{'policy':gate['candidate_policy'],'topology':'multi'},
            'baseline_single':{'policy':baseline,'topology':'single'}},
        'files':{name:sha(ROOT/name) for name in sorted(files)},'dependencies':dependencies(),
        'public_provider_config':public_config(),'workers':2,
        'order_salt':'business-final-interleaved-v1',
        'rules':'One attempt per arm/case. Resume restores completed records; interrupted attempts stay failures. No test-driven tuning. All 160 cases remain in every denominator.'}
    with args.output.open('x',encoding='utf-8') as out:
        out.write(json.dumps(frozen,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'freeze':str(args.output),'sha256':sha(args.output),'arms':list(frozen['arms'])}))


if __name__=='__main__':
    main()
