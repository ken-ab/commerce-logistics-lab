import json
import pytest
from research import apparel_rationale_continuation as c


def test_only_one_unused_retry_after_recorded_timeout(tmp_path):
    assert c.previous_attempts(tmp_path, 'case') == 0
    p=tmp_path/'case_attempt1.error.json'
    p.write_text(json.dumps({'attempt':1,'retryable':True}),encoding='utf-8')
    assert c.previous_attempts(tmp_path, 'case') == 1
    (tmp_path/'case.json').write_text('{}',encoding='utf-8')
    with pytest.raises(AssertionError):
        c.previous_attempts(tmp_path,'case')


def test_one_remaining_request_cannot_turn_into_two(monkeypatch):
    calls=[]; ids=set()
    class Inner:
        def chat(self,*args,**kwargs):
            calls.append(kwargs); ids.add('request-1')
            raise TimeoutError('synthetic failure')
    monkeypatch.setattr(c.base,'ledger',lambda:{'study_micro_cny':15_802_409})
    monkeypatch.setattr(c,'call_ids',lambda:set(ids))
    client=c.ContinuationClient(1,inner=Inner())
    with pytest.raises(TimeoutError): client.chat([],purpose='report_claim_audit')
    with pytest.raises(RuntimeError,match='allowance'): client.chat([],purpose='report_claim_audit')
    assert len(calls)==1 and client.requests[0]['budget_call_ids']==['request-1']
    assert calls[0]['purpose']==c.base.PREFIX+'report_claim_audit'


def test_shared_budget_guard_retains_existing_costs(monkeypatch):
    class Inner:
        def chat(self,*args,**kwargs): raise AssertionError('Must not call upstream')
    monkeypatch.setattr(c.base,'ledger',lambda:{'study_micro_cny':75_000_001})
    client=c.ContinuationClient(2,inner=Inner())
    with pytest.raises(RuntimeError,match='80 bound'): client.chat([],purpose='report_claim_audit')
    assert client.requests==[]
