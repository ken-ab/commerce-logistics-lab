"""One registered transport probe using unchanged, previously fixed judge fixtures."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
from itertools import zip_longest
import json
from pathlib import Path

from audit_reliability.limits import IncrementalBudget
from audit_reliability.transport import ObservedAuditClient, VerifiedAuditTransport, VERSION
from evaluation.report_judge import calibration_cases as fact_cases, judge, save
from evaluation_v2.communication import calibration_cases as communication_cases, audit_communication
from research.model_config import ROOT


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    # Inputs and expected decisions must be exactly the earlier fixed fixtures.
    facts, communication = fact_cases(), communication_cases()
    prior_facts = ROOT/'evidence/report_audits/20260907T142518252421Z_calibration'
    prior_communication = ROOT/'evidence/v2_audits/20260907T174415438062Z_calibration'
    if facts != read(prior_facts/'inputs.json') or communication != read(prior_communication/'inputs.json'):
        raise ValueError('Frozen calibration fixtures differ')
    if sha(ROOT/'evaluation/report_judge.py') != read(prior_facts/'config.json')['code_sha256']:
        raise ValueError('Fact judge changed')
    if sha(ROOT/'evaluation_v2/communication.py') != read(prior_communication/'config.json')['communication']['code_sha256']:
        raise ValueError('Communication judge changed')
    register = ROOT/'evidence/audit_transport_calibration_registration.json'
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    output = ROOT/'evidence/audit_transport_calibration'/stamp
    with register.open('x',encoding='utf-8') as handle:
        json.dump({'status':'registered','directory':str(output),'created_at':stamp},handle,indent=2)
    output.mkdir(parents=True)
    (output/'raw').mkdir()
    transport = VerifiedAuditTransport(output/'transport')
    client = ObservedAuditClient(transport=transport)
    client.ledger = IncrementalBudget(client.ledger,12)
    items = []
    for f,c in zip_longest(facts,communication):
        if f:
            items.append({'kind':'facts',**f})
        if c:
            items.append({'kind':'communication',**c})
    save(output/'inputs.json',items)
    paths = list((ROOT/'audit_reliability').glob('*.py')) + [ROOT/p for p in (
        'evaluation/report_judge.py','evaluation_v2/communication.py','research/model_client.py',
        'research/budget.py','research/model_config.py','research/rate_card.json','research/budget_policy.json')]
    signatures = {p.relative_to(ROOT).as_posix():sha(p) for p in paths}
    config = {'version':VERSION,'mode':'fixed-fixture-transport-probe','workers':2,'fixture_count':41,
        'judge_model':'qwen3.8-max','maximum_audit_transport_attempts':2,
        'additional_accounting_ceiling_cny':12,'starting_global_budget':client.ledger.summary(),
        'inputs_sha256':sha(output/'inputs.json'),'files':signatures,
        'preregistered_acceptance':{'complete_audits':41,'fixture_agreements':41,
            'first_attempt_completions':41,'at_least_one_reused_connection':True},
        'scope':'Unchanged fixtures, judgments, prompts and model. No retry of a completed invalid or unfavorable audit. '
                'This is an operational calibration, not a randomized causal comparison with the previous transport. '
                'Original validation failure is permanent; this probe cannot promote that validation.'}
    save(output/'config.json',config)
    for path in paths:
        target = output/'code_snapshot'/path.relative_to(ROOT)
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(path.read_bytes())
    def run(item):
        row = {'id':item['id'],'kind':item['kind'],'expected':item['expected']}
        try:
            raw = output/'raw'/(item['id']+'.json')
            if item['kind']=='facts':
                result = judge(item['answer'],item['evidence'],client=client,model='qwen3.8-max',raw_path=raw)
                actual = result['decision']['verdict']
            else:
                result = audit_communication(item['case'],item['report'],client=client,raw_path=raw)
                actual = result['passed']
            row.update(status='audited',result=result,agrees=actual==item['expected'],actual=actual,
                       transport_attempts=read(raw).get('transport_attempts',1))
        except Exception as error:
            row.update(status='audit_failed',agrees=False,error=str(error)[:1000],
                       transport_failures=getattr(error,'attempts',[]))
        return row
    rows = []
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run,item) for item in items]
            for future in as_completed(futures):
                row = future.result()
                rows.append(row)
                save(output/'results.json',sorted(rows,key=lambda x:(x['kind'],x['id'])))
                print(json.dumps({'completed':len(rows),'total':len(items),'id':row['id'],
                    'status':row['status'],'agrees':row['agrees']}),flush=True)
    finally:
        transport.close()
    traces = [read(p) for p in (output/'transport').glob('*.json')]
    checks = {'complete_audits':sum(r['status']=='audited' for r in rows)==41,
        'fixture_agreements':sum(r['agrees'] for r in rows)==41,
        'first_attempt_completions':sum(r['status']=='audited' and r['transport_attempts']==1 for r in rows)==41,
        'connection_reuse':any(t.get('reused_connection') for t in traces),
        'method_unchanged':all(sha(ROOT/p)==value for p,value in signatures.items())}
    summary = {'directory':str(output),'scheduled':41,'completed':len(rows),'passed':all(checks.values()),
        'checks':checks,'agreements':sum(r['agrees'] for r in rows),'http_attempts':len(traces),
        'connection_reuses':sum(t.get('reused_connection',False) for t in traces),
        'transport_failures':sum(t.get('status')=='failed' for t in traces),
        'global_budget':client.ledger.summary(),'config_sha256':sha(output/'config.json')}
    save(output/'summary.json',summary)
    save(register,{'status':'complete','directory':str(output),'summary_sha256':sha(output/'summary.json'),
                   'completed_at':datetime.now(timezone.utc).isoformat()})
    print(json.dumps(summary,ensure_ascii=False,indent=2),flush=True)


if __name__ == '__main__':
    main()
