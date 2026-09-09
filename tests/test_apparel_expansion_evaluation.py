"""Acceptance regressions against preserved real traces; no new model calls."""
from copy import deepcopy
from pathlib import Path
import json

import pytest

from research.apparel_expansion_evaluation import evaluate

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def trace_case():
    root = ROOT / 'evidence/apparel_source_validation_v1'
    case = next(c for c in json.loads((root / 'cases.json').read_text(encoding='utf-8')) if c['id'] == 'SR-09-0')
    case['expected'].setdefault('issue', None)
    execution = json.loads((root / 'runs/SR-09-0-v5_source_review/execution.json').read_text(encoding='utf-8'))
    seed = json.loads((root / 'initial/SR-09-0/seed.json').read_text(encoding='utf-8'))

    class Store:
        def assess(self, *args, **kwargs):
            return {'valid': True}

    return case, execution, Store(), seed


def test_known_valid_trace_retains_acceptance(trace_case):
    assert evaluate(*trace_case)['passed']


@pytest.mark.parametrize('defect', ['material', 'source_field', 'selection', 'requirement', 'proposal'])
def test_external_acceptance_does_not_trust_report_pass_flag(trace_case, defect):
    case, execution, store, seed = deepcopy(trace_case)
    if defect == 'material':
        execution['traces'] = [t for t in execution['traces'] if not (t['kind'] == 'tool' and t['tool'] == 'read_variant')]
    elif defect == 'source_field':
        execution['report']['source_facts'][0]['value'] = 'unsupported value'
    elif defect == 'selection':
        execution['report']['decision']['product_skus'] = ['invented SKU']
    elif defect == 'requirement':
        execution['after']['request']['shipping']['budget_cents'] += 1
    else:
        execution['after']['proposals'] = execution['after']['proposals'][:-1]
    result = evaluate(case, execution, store, seed)
    assert execution['report']['operation_check']['passed']
    assert not result['passed'] and result['failures']
