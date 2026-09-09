"""Read-only integrity/accounting QA of the completed factorial pilot."""
from collections import Counter
from contextlib import closing
from decimal import Decimal
import json
import sqlite3

from apparel_fulfillment.agent import compact, pointer
from apparel_fulfillment.agent_evidence_v2 import citation_directory
from apparel_fulfillment.data import ROOT, digest
from research.apparel_experiment import sha, save
from research.apparel_state_pilot_v3 import DIRECTORY, CONDITIONS
from research.apparel_state_report_v3 import load_complete


def check():
    registration, summary, rows = load_complete()
    model_traces = citation_paths = checked_facts = 0
    ids, ledger_cost = set(), Decimal(0)
    ledger_status = Counter()
    database = 'file:' + (ROOT / 'evidence/api_budget.sqlite').as_posix() + '?mode=ro'
    with closing(sqlite3.connect(database, uri=True)) as db:
        for r in rows:
            folder = DIRECTORY / 'runs' / r['case_id'] / r['condition'] / r['arm']
            execution = json.loads((folder / 'execution.json').read_text(encoding='utf-8'))
            assert all(r[k] == value for k, value in execution.items())
            assert set(r) - set(execution) == {'score', 'original_score', 'supplementary_score'}
            assert r['registration_sha256'] == sha(DIRECTORY / 'registration.json')
            assert digest(r['before']) == r['initial_view_digest']
            b, g = CONDITIONS[r['condition']]
            assert r['interventions'] == {'bootstrap': b, 'enforce_contract': g}
            assert r['host_initial_reads'] == int(b)
            assert r['tool_calls'] <= 32 and r['model_calls'] <= 12
            # No claim about task quality follows from passing these hash checks.
            for trace in r['traces']:
                if trace['kind'] != 'model': continue
                assert digest(trace['messages']) == trace['input_sha256']
                text = compact(trace['messages'])
                assert len(text) == trace['input_characters']
                assert r['case_id'] not in text and 'SCDEV-' not in text
                assert 'allowed_by_line' not in text and 'minimum_differences' not in text
                model_traces += 1
            for obs in r['observations'].values():
                for path in citation_directory(obs)['paths']:
                    assert len(compact(pointer(obs, path))) <= 2500
                    citation_paths += 1
            if r.get('report'):
                for fact in r['report']['source_facts']:
                    assert compact(pointer(r['observations'][fact['observation_id']], fact['pointer'])) == compact(fact['value'])
                    checked_facts += 1
                if g: assert r['report']['operation_check']['passed']
                receipt = r['report']['execution_receipt']
                assert receipt['selected_lines'] == r['after']['selections']
                assert receipt['current_revision'] == r['after']['revision']
            attempt = json.loads((folder / 'attempt.json').read_text(encoding='utf-8'))
            records = db.execute('SELECT id,COALESCE(charged,reserved),status FROM calls WHERE purpose LIKE ?',
                                 (attempt['purpose'] + '%',)).fetchall()
            amount = sum(value for _, value, _ in records)
            assert Decimal(amount) / 1000000 == Decimal(r['accounted_and_reserved_cny'])
            ledger_cost += Decimal(amount) / 1000000
            run_ids = {ident for ident, _, _ in records}
            assert len(run_ids) == len(records) and not ids & run_ids
            assert {c['budget_call_id'] for c in r['calls'] if c.get('budget_call_id')} <= run_ids
            ids.update(run_ids)
            ledger_status.update(state for _, _, state in records)
    assert ledger_cost == sum((Decimal(r['accounted_and_reserved_cny']) for r in rows), Decimal(0))
    output = {'status': 'passed', 'scope': 'Integrity and accounting, not an independent task-performance claim',
              'runs': len(rows), 'model_input_hashes': model_traces, 'citation_paths_checked': citation_paths,
              'reported_fact_values_checked': checked_facts, 'unique_ledger_call_ids': len(ids),
              'ledger_status_counts': dict(ledger_status), 'accounted_and_reserved_cny': str(ledger_cost),
              'registration_sha256': sha(DIRECTORY / 'registration.json'), 'summary_sha256': sha(DIRECTORY / 'summary.json'),
              'source_sha256': sha(ROOT / 'research/apparel_state_qa_v3.py'),
              'reporting_files_sha256': {p: sha(ROOT / p) for p in ('research/apparel_state_report_v3.py',
                  'tests/test_apparel_state_report_v3.py', 'evidence/apparel_state_report_v3_tests.xml')},
              'business_constraint_violation_runs': sum(r['score']['constraint_violation'] for r in rows)}
    save(DIRECTORY / 'qa.json', output)
    print(json.dumps(output, ensure_ascii=False))


if __name__ == '__main__': check()
