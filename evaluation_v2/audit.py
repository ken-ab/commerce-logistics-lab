"""Audit the complete new-case campaign, retaining every scheduled denominator."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from evaluation.report_judge import evidence_for, judge, save, VERSION as FACT_VERSION
from evaluation_v2.communication import audit_communication, calibration_cases, signature
from evaluation_v2.prepare import load_cases
from research.model_config import ROOT
from research.model_client import BudgetedChatClient


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def failed(error):
    return {'status': 'audit_failed', 'error': str(error)[:1000],
            'transport_failures': getattr(error, 'attempts', [])}


def audit_item(item, output, calibration):
    ident = item['id']
    if not item.get('report'):
        return {'id': ident, 'status': 'not_assessable', 'reason': 'No completed report; retained in denominator.'}
    result = {'id': ident, 'status': 'assessed'}
    if not calibration:
        try:
            result['facts'] = {'status': 'audited', **judge(item['report']['answer'], item['evidence'],
                model='qwen3.8-max', raw_path=output/'raw_facts'/(ident+'.json'))}
        except Exception as error:
            result['facts'] = failed(error)
    try:
        result['communication'] = {'status': 'audited', **audit_communication(item['case'], item['report'],
            raw_path=output/'raw_communication'/(ident+'.json'))}
    except Exception as error:
        result['communication'] = failed(error)
    if calibration:
        result['expected'] = item['expected']
        result['agrees_with_fixture'] = (result['communication']['status'] == 'audited'
            and result['communication']['passed'] == item['expected'])
    return result


def campaign_items(campaign):
    config, rows = read(campaign/'config.json'), read(campaign/'results.json')
    ids = config['case_ids']
    if len(rows) != len(ids) or len(set(ids)) != len(ids) or {r['case_id'] for r in rows} != set(ids):
        raise ValueError('Audit requires all registered cases exactly once')
    if read(campaign/'summary.json')['cases'] != len(ids):
        raise ValueError('Incomplete campaign summary')
    cases, manifest = load_cases()
    if config['case_manifest_sha256'] != manifest['sha256']:
        raise ValueError('Campaign used a different dataset')
    by_id = {c['id']:c for c in cases}
    items = []
    for ident in ids:
        folder = campaign/ident
        record = read(folder/'actual_run.json') if (folder/'actual_run.json').exists() else None
        report = (record.get('result') or {}).get('report') if record else None
        evidence = None
        if report:
            score = read(folder/'external_score/score.json') if (folder/'external_score/score.json').exists() else {}
            evidence = evidence_for(record, by_id[ident]['task'], before=read(folder/'initial_state.json'),
                after=read(folder/'live_state.json'), replay_verified=bool(score.get('live_replay_state_matches')))
        items.append({'id': ident, 'case': {k:by_id[ident][k] for k in ('family','task','response_language')},
                      'report': report, 'evidence': evidence})
    return items, config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--calibrate', action='store_true')
    parser.add_argument('--campaign', type=Path)
    args = parser.parse_args()
    if args.calibrate == bool(args.campaign):
        raise ValueError('Choose calibration or one complete campaign')
    if args.calibrate:
        items, config = calibration_cases(), {}
    else:
        items, config = campaign_items(args.campaign)
    # One registration per logical audit. Failures require diagnosis, not an
    # automatic second complete run to obtain a more favorable result.
    register = ROOT/'evidence/v2_communication_calibration_registration.json' if args.calibrate else args.campaign/'audit_registration.json'
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    output = ROOT/'evidence/v2_audits'/(stamp+('_calibration' if args.calibrate else '_'+config['arm']))
    output.parent.mkdir(parents=True, exist_ok=True)
    with register.open('x', encoding='utf-8') as f:
        json.dump({'status':'registered', 'directory':str(output), 'created_at':stamp}, f, indent=2)
    output.mkdir()
    (output/'raw_facts').mkdir()
    (output/'raw_communication').mkdir()
    scope = {'mode':'calibration' if args.calibrate else 'campaign', 'campaign':str(args.campaign) if args.campaign else None,
        'communication':signature(), 'facts_version':FACT_VERSION, 'judge_model':'qwen3.8-max',
        'source_results_sha256':hashlib.sha256((args.campaign/'results.json').read_bytes()).hexdigest() if args.campaign else None,
        'case_ids':[i['id'] for i in items], 'workers':2}
    save(output/'config.json',scope)
    save(output/'inputs.json',items)
    for source in (Path(__file__), ROOT/'evaluation_v2/communication.py', ROOT/'evaluation/report_judge.py'):
        (output/source.name).write_bytes(source.read_bytes())
    rows = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(audit_item,item,output,args.calibrate) for item in items]
        for future in as_completed(futures):
            row = future.result()
            rows.append(row)
            save(output/'results.json',sorted(rows,key=lambda r:r['id']))
            print(json.dumps({'completed':len(rows),'total':len(items),'id':row['id'],
                'fact_verdict':row.get('facts',{}).get('decision',{}).get('verdict'),
                'communication_passed':row.get('communication',{}).get('passed'),
                'agrees':row.get('agrees_with_fixture')},ensure_ascii=False),flush=True)
    summary = {**scope, 'directory':str(output), 'scheduled':len(items),
        'facts_supported':sum(r.get('facts',{}).get('decision',{}).get('verdict')=='supported' for r in rows),
        'communication_passed':sum(r.get('communication',{}).get('passed',False) for r in rows),
        'calibration_agreements':sum(r.get('agrees_with_fixture',False) for r in rows) if args.calibrate else None,
        'global_budget':BudgetedChatClient().ledger.summary()}
    save(output/'summary.json',summary)
    save(register, {'status':'complete', 'directory':str(output), 'completed_at':datetime.now(timezone.utc).isoformat(),
                    'summary_sha256':hashlib.sha256((output/'summary.json').read_bytes()).hexdigest()})
    print(json.dumps(summary,ensure_ascii=False,indent=2),flush=True)


if __name__ == '__main__':
    main()
