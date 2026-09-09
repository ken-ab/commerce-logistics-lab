"""Evidence-based skill promotion. A rejected experiment never becomes active."""
import argparse
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from commerce_lab.skills import SkillPolicy, SkillRegistry
from commerce_lab.state import Store
from evaluation.report_judge import calibration_cases, evidence_for
from research.model_config import ROOT

CRITICAL = {'stock_shortage','untrusted_text','budget_infeasible','blocked_route'}
BUSINESS_FILES = ('commerce_lab/agent.py','commerce_lab/backend.py','commerce_lab/state.py',
    'commerce_lab/catalog.py','commerce_lab/skills.py','logistics_lab/planning.py',
    'evaluation/environment.py','evaluation/tau_bridge.py','research/model_client.py',
    'research/model_config.py','research/budget.py','research/tls_transport.py')


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def campaign(path, partition):
    config, rows, summary = [read(path/name) for name in ('config.json','results.json','summary.json')]
    expected = {c['id'] for c in read(ROOT/'data/commerce_cases_v1.json')['cases'] if c['partition']==partition}
    if (config['partition'] != partition or len(rows) != len(expected)
            or len(rows) != summary['cases'] or len({r['case_id'] for r in rows}) != len(rows)
            or {r['case_id'] for r in rows} != expected or set(config['case_ids']) != expected):
        raise ValueError('A complete frozen campaign partition is required')
    if summary['passed'] != sum(r['score']['passed'] for r in rows):
        raise ValueError('Campaign summary and case outcomes differ')
    return config, {r['case_id']:r for r in rows}, summary


def compare_rows(baseline, candidate):
    if set(baseline) != set(candidate):
        raise ValueError('Paired comparisons require identical case membership')
    improved = [i for i in baseline if candidate[i]['score']['passed'] and not baseline[i]['score']['passed']]
    regressed = [i for i in baseline if baseline[i]['score']['passed'] and not candidate[i]['score']['passed']]
    return {'improved':sorted(improved),'regressed':sorted(regressed),
        'critical_regressions':sorted(i for i in regressed if candidate[i]['family'] in CRITICAL)}


def report_gate(audit, calibration, validation):
    config, rows, summary = [read(audit/name) for name in ('config.json','results.json','summary.json')]
    cal_config, cal_rows, cal_summary = [read(calibration/name) for name in ('config.json','results.json','summary.json')]
    val_config = read(validation/'config.json')
    inputs = {r['id']:r for r in read(audit/'inputs.json')}
    reasons = []
    if (config['mode'] != 'campaign' or config['partition'] != val_config['partition']
            or config['source_results_sha256'] != digest(validation/'results.json')):
        reasons.append('Report audit is not bound to the candidate validation results')
    if (len(rows) != len(val_config['case_ids']) or len({r['id'] for r in rows}) != len(rows)
            or {r['id'] for r in rows} != set(val_config['case_ids']) or summary['cases'] != len(rows)):
        reasons.append('Report audit coverage is incomplete')
    if (cal_config['mode'] != 'calibration' or len(cal_rows) != cal_config['fixture_count']
            or {r['id'] for r in cal_rows} != {r['id'] for r in calibration_cases()}
            or len({r['id'] for r in cal_rows}) != len(cal_rows)
            or cal_summary['cases'] != len(cal_rows) or not all(r.get('agrees_with_fixture') is True for r in cal_rows)):
        reasons.append('Report judge calibration is incomplete or failed')
    for field in ('judge_version','system_prompt_sha256','code_sha256','model','client_code_sha256','rate_card_sha256'):
        if field not in config or field not in cal_config:
            reasons.append('Audit/calibration signature is missing: '+field)
            continue
        if config[field] != cal_config[field]:
            reasons.append('Audit and calibration differ: '+field)
    if config['code_sha256'] != digest(ROOT/'evaluation/report_judge.py'):
        reasons.append('Report evaluator changed after calibration/audit')
    if config.get('client_code_sha256')!=digest(ROOT/'research/model_client.py'):
        reasons.append('Report model client changed after calibration/audit')
    if config.get('rate_card_sha256')!=digest(ROOT/'research/rate_card.json'):
        reasons.append('Report price card changed after calibration/audit')
    tasks = {c['id']:c['task'] for c in read(ROOT/'data/commerce_cases_v1.json')['cases']}
    for ident in val_config['case_ids']:
        case_dir = validation/ident
        required = ['actual_run.json','initial_state.json','live_state.json','external_score/score.json']
        if any(not (case_dir/name).is_file() for name in required):
            reasons.append('Candidate evidence files are missing: '+ident)
            continue
        record = read(case_dir/'actual_run.json')
        expected_answer = record.get('result',{}).get('report',{}).get('answer')
        if ident not in inputs or inputs[ident]['answer'] != expected_answer:
            reasons.append('Audit answer differs from the actual report: '+ident)
            continue
        if not expected_answer:
            continue
        before = read(validation/ident/'initial_state.json')
        after = read(validation/ident/'live_state.json')
        score = read(validation/ident/'external_score/score.json')
        expected = evidence_for(record,tasks[ident],before=before,after=after,
            replay_verified=bool(score.get('live_replay_state_matches')))
        if inputs[ident]['evidence'] != expected:
            reasons.append('Audit sources differ from recorded state/tool evidence: '+ident)
    unresolved = [r['id'] for r in rows if r['status'] != 'audited' or r.get('decision',{}).get('verdict') != 'supported']
    if unresolved:
        reasons.append('Unresolved or unsupported report claims: '+', '.join(sorted(unresolved)))
    return reasons


def assess(baseline_dev, candidate_dev, baseline_val, candidate_val, audit, calibration, dev_audit):
    bd, br, bs = campaign(baseline_dev,'development')
    cd, cr, cs = campaign(candidate_dev,'development')
    bv, bvr, bvs = campaign(baseline_val,'validation')
    cv, cvr, cvs = campaign(candidate_val,'validation')
    reasons = []
    if cs['passed'] <= bs['passed'] or compare_rows(br,cr)['critical_regressions']:
        reasons.append('Development gain or critical-regression gate failed')
    if SkillPolicy.model_validate(cd['policy']).checked() != SkillPolicy.model_validate(cv['policy']).checked():
        reasons.append('Candidate changed between development and validation')
    if SkillPolicy.model_validate(bd['policy']).checked() != SkillPolicy.model_validate(bv['policy']).checked():
        reasons.append('Baseline changed between development and validation')
    if bv['model'] != cv['model'] or bv.get('topology','multi') != cv.get('topology','multi'):
        reasons.append('Validation model or topology differs')
    for name in BUSINESS_FILES:
        if bv['code_sha256'].get(name) != cv['code_sha256'].get(name):
            reasons.append('Paired business implementation differs: '+name)
        if cv['code_sha256'].get(name) != digest(ROOT/name):
            reasons.append('Business implementation changed after validation: '+name)
    paired = compare_rows(bvr,cvr)
    if cvs['passed'] < 31 or cvs['passed'] < bvs['passed'] or paired['critical_regressions']:
        reasons.append('Validation pass-count or critical-regression gate failed')
    if any('evaluation_error' in r['score'] for r in [*bvr.values(),*cvr.values()]):
        reasons.append('Validation contains grading errors')
    baseline_cost = sum((Decimal(r.get('settled_cost_cny','0')) for r in bvr.values()),Decimal(0))
    candidate_cost = sum((Decimal(r.get('settled_cost_cny','0')) for r in cvr.values()),Decimal(0))
    if baseline_cost <= 0 or candidate_cost > baseline_cost * Decimal('1.35'):
        reasons.append('Candidate cost exceeds the declared 1.35 baseline limit or baseline cost is unavailable')
    # Successful response usage does not account for timed-out requests. Refuse
    # a cost-based promotion if any candidate call lacks a settled response.
    for ident, row in cvr.items():
        record_path = candidate_val/ident/'actual_run.json'
        if not record_path.is_file():
            reasons.append('Candidate run record is missing: '+ident)
            continue
        record = read(record_path)
        completed_calls = sum(t['kind']=='model_response' for t in record['traces'])
        if completed_calls != row.get('model_calls') or any(t['kind']=='model_error' for t in record['traces']):
            reasons.append('Candidate cost has unaccounted/uncertain calls: '+ident)
    reasons.extend(report_gate(audit,calibration,candidate_val))
    reasons.extend(report_gate(dev_audit,calibration,candidate_dev))
    directories = [baseline_dev,candidate_dev,baseline_val,candidate_val,audit,calibration,dev_audit]
    evidence = {}
    for directory in directories:
        for name in ('config.json','results.json','summary.json'):
            path = directory/name
            evidence[path.resolve().relative_to(ROOT).as_posix()] = digest(path)
    for directory in (candidate_dev,candidate_val):
        for path in sorted(directory.glob('*/actual_run.json')):
            evidence[path.resolve().relative_to(ROOT).as_posix()] = digest(path)
    for directory in (audit,dev_audit,calibration):
        evidence[(directory/'inputs.json').resolve().relative_to(ROOT).as_posix()] = digest(directory/'inputs.json')
    return {'evaluated_at':datetime.now(timezone.utc).isoformat(),'eligible':not reasons,'reasons':reasons,
        'inputs':{name:str(path.resolve()) for name,path in zip(
            ('baseline_dev','candidate_dev','baseline_val','candidate_val','audit','calibration','dev_audit'),directories)},
        'baseline_id':bv['policy']['id'],'candidate_id':cv['policy']['id'],'candidate_policy':cv['policy'],
        'validation':{'baseline_passed':bvs['passed'],'candidate_passed':cvs['passed'],'cases':len(cvr),
            'baseline_cost_cny':str(baseline_cost),'candidate_cost_cny':str(candidate_cost),'paired':paired},
        'files':evidence,'limitations':'32 synthetic validation cases; no statistical/general-market claim; LLM report audit is fallible.'}


def main():
    parser = argparse.ArgumentParser()
    for name in ('baseline-dev','candidate-dev','baseline-val','candidate-val','audit','calibration','dev-audit'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--activate',action='store_true')
    args = parser.parse_args()
    gate = assess(args.baseline_dev,args.candidate_dev,args.baseline_val,args.candidate_val,args.audit,args.calibration,args.dev_audit)
    output = ROOT/'evidence/skill_gates'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ.json')
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(gate,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    if args.activate:
        if not gate['eligible']:
            raise SystemExit('Skill rejected; evidence saved to '+str(output))
        registry = SkillRegistry(Store())
        registry.register(gate['candidate_policy'],{'gate':str(output)})
        registry.activate(gate['candidate_id'],expected_parent=gate['baseline_id'],
            evidence={'gate':str(output),'gate_sha256':digest(output)})
    print(json.dumps({'gate':str(output),'eligible':gate['eligible'],'activated':args.activate and gate['eligible'],
        'reasons':gate['reasons'],'validation':gate['validation']},ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
