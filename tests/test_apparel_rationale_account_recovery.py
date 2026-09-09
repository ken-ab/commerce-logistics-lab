import pytest
from research import apparel_rationale_account_recovery as recovery


def test_only_explicit_400_account_denial_is_newly_classified():
    assert recovery.account_denial('HTTPError HTTP 400: https://help.aliyun.com/zh/model-studio/error-code#overdue-payment')
    assert not recovery.account_denial('HTTPError HTTP 400: invalid schema')
    assert not recovery.account_denial('TimeoutError')


def test_exhausted_original_attempt_allowance_sends_nothing(monkeypatch):
    class Inner:
        def ensure_available(self,model):pass
        def chat(self,*a,**kw):raise AssertionError('Must not submit')
    client=recovery.RecoveryClient(1,Inner());client.requests=[{}]
    monkeypatch.setattr(recovery.base,'ledger',lambda:{'study_micro_cny':75_772_459,'global_micro_cny':415_523_859})
    with pytest.raises(RuntimeError,match='allowance exhausted'):client.chat([])


def test_global_ceiling_is_checked_before_reservation(monkeypatch):
    class Inner:
        def ensure_available(self,model):pass
        def chat(self,*a,**kw):raise AssertionError('Must not submit')
    client=recovery.RecoveryClient(2,Inner())
    monkeypatch.setattr(recovery.base,'ledger',lambda:{'study_micro_cny':75_772_459,'global_micro_cny':476_000_000})
    with pytest.raises(RuntimeError,match='budget bound'):client.chat([])
    assert client.requests==[]
