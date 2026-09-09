import hashlib
import json

import pytest

import apparel_fulfillment.jobs as job_module
import delivery_apparel_reliability as release_module
from apparel_fulfillment.interactive_reliability import (
    InteractiveReliabilityOperationAgent, UI_POLICY, scoped_operation_agent)
from apparel_fulfillment.interactive_sources import InteractiveSourceOperationAgent
from test_apparel_agent import Client, tool
from test_apparel_reliability import finish_from_guide
from test_apparel_source_review import proposal_workspace, SKU, NOW


@pytest.mark.parametrize('operation', ['prepare_proposal', 'review_proposal'])
def test_new_review_adapter_keeps_current_selection_and_approval(proposal_workspace, operation):
    store, draft, _ = proposal_workspace
    agent = InteractiveReliabilityOperationAgent(store, 'owner', draft['id'], client=object(), now=NOW,
                                                 contract={'mode': operation})
    before = store.view('owner', draft['id'])
    with pytest.raises(ValueError, match='preserves current selections'):
        agent.invoke('select_variants', {'selections': [], 'expected_revision': draft['revision']})
    assert store.view('owner', draft['id']) == before


@pytest.mark.parametrize('mode', ['inspect_product', 'check_order', 'stage_candidate', 'prepare_proposal', 'review_proposal'])
def test_only_review_selects_new_policy(monkeypatch, mode):
    import apparel_fulfillment.interactive_reliability as module
    monkeypatch.setattr(module, 'InteractiveReliabilityOperationAgent', lambda *a, **kw: 'v6')
    monkeypatch.setattr(module, 'InteractiveSourceOperationAgent', lambda *a, **kw: 'v5')
    assert scoped_operation_agent(contract={'mode': mode}) == ('v6' if mode == 'review_proposal' else 'v5')


def test_persisted_review_has_program_comparison_and_no_confirmation(proposal_workspace, tmp_path, monkeypatch):
    store, draft, proposal = proposal_workspace
    monkeypatch.setattr(job_module, 'ROOT', tmp_path)
    client = Client([[tool('read_proposal', proposal_id=proposal['proposal_id']), tool('read_variant', sku=SKU)],
                     finish_from_guide])
    client.ensure_available = lambda _: None
    jobs = job_module.AgentJobs(store, directory=tmp_path / 'jobs', state_agent_factory=scoped_operation_agent,
                               client_factory=lambda: client)
    job = jobs.start('owner', draft['id'], 'Review and retain the current valid proposal.', 'single',
                     operation={'mode': 'review_proposal'})
    jobs.pool.shutdown(wait=True)
    record = jobs.get('owner', job['job_id'], full=True)
    result = record['result']
    assert record['status'] == 'completed' and result['policy_version'] == UI_POLICY
    assert result['report']['operation_check']['passed'] and result['report']['source_review']['passed']
    assert result['report']['revision_comparison']['new_proposal_id'] == proposal['proposal_id']
    assert result['report']['revision_explanation'] in result['report']['answer']
    assert result['after']['confirmation'] is None and result['after'] == result['before']
    assert jobs.get('owner', job['job_id'])['result']['report']['revision_explanation']


@pytest.fixture
def release_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(release_module, 'ROOT', tmp_path)
    (tmp_path / 'evidence/apparel_reliability_study_v1').mkdir(parents=True)
    (tmp_path / 'research').mkdir()
    source = tmp_path / 'research/audit_apparel_reliability_study.py'
    source.write_text('fixture', encoding='utf-8')
    checksum = hashlib.sha256(source.read_bytes()).hexdigest()
    audit = {'audit_completed': True, 'business_and_ledger_reconciled': True, 'all_program_checks_passed': True,
             'engineering_gate_passed': True, 'engineering_gate': {key: True for key in
                ('minimum_each_arm', 'no_arm_acceptance_regression', 'no_protected_violations',
                 'total_cost_limit', 'latency_limit_each_arm', 'program_comparison_correct')}, 'verified_sha256': {},
             'post_audit_snapshot_sha256': {}, 'audit_source_sha256': checksum}
    summary = {'groups': {v+'_'+arm: {'runs': 24, 'passed': 24, 'cost_cny': '1', 'mean_latency_seconds': 1}
                          for v in ('v5', 'v6') for arm in ('single', 'coordinator', 'on_demand')}}
    def save():
        (tmp_path / 'evidence/apparel_reliability_audit_20260909.json').write_text(json.dumps(audit), encoding='utf-8')
        (tmp_path / 'evidence/apparel_reliability_study_v1/summary.json').write_text(json.dumps(summary), encoding='utf-8')
    save()
    return audit, summary, source, save


@pytest.mark.parametrize('key', ['audit_completed', 'business_and_ledger_reconciled',
                                'all_program_checks_passed', 'engineering_gate_passed'])
def test_release_refuses_unfinished_or_failed_gate(release_fixture, key):
    audit, _, _, save = release_fixture
    audit[key] = False
    save()
    with pytest.raises(RuntimeError, match='engineering gate'):
        release_module.reliability_release()


def test_release_rejects_modified_source_or_missing_trial(release_fixture):
    _, summary, source, save = release_fixture
    assert release_module.reliability_release()['explicit_operations'] == ['review_proposal']
    source.write_text('modified after audit', encoding='utf-8')
    with pytest.raises(RuntimeError, match='evidence changed'):
        release_module.reliability_release()
    summary['groups']['v6_single']['runs'] = 23
    save()
    with pytest.raises(RuntimeError, match='incomplete'):
        release_module.reliability_release()
