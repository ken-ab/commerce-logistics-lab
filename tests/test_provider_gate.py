from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from research.model_client import ModelCallError
from research.provider_gate import GuardedChatClient, ProviderGate, ProviderHeld, account_failure
from research.guarded_dispatch import dispatch


def test_persistent_provider_scope_and_explicit_recovery(tmp_path):
    gate = ProviderGate(tmp_path / 'hold.sqlite')
    gate.hold('aihubmix', 'account_balance_insufficient')
    restored = ProviderGate(gate.path)
    with pytest.raises(ProviderHeld): restored.check('aihubmix')
    restored.check('dashscope')
    restored.acknowledge_recovery('aihubmix')
    gate.check('aihubmix')
    assert account_failure(ModelCallError('HTTPError HTTP 403: model access forbidden')) is None
    assert account_failure(ModelCallError('HTTP 429: busy')) is None


def test_block_before_client_reservation_and_preserve_first_error(tmp_path):
    class Client:
        card = {'models': {'fixture': {'provider': 'aihubmix'}}}
        sends = 0
        def chat(self, *args, **kwargs):
            self.sends += 1
            raise ModelCallError('HTTPError HTTP 403: Your account balance is insufficient.')
    client, gate = Client(), ProviderGate(tmp_path / 'hold.sqlite')
    guarded = GuardedChatClient(client, gate)
    with pytest.raises(ModelCallError, match='HTTP 403'): guarded.chat([], model='fixture')
    with pytest.raises(ProviderHeld): guarded.chat([], model='fixture')
    assert client.sends == 1


def test_bounded_dispatch_leaves_queued_jobs_unattempted_on_hold(tmp_path):
    gate = ProviderGate(tmp_path / 'hold.sqlite')
    attempts = []
    def worker(job):
        attempts.append(job)
        gate.hold('aihubmix', 'account_balance_insufficient')
        return {'failed': True}
    results = []
    with pytest.raises(ProviderHeld):
        for pair in dispatch(range(20), worker, lambda: gate.check('aihubmix'), max_workers=1):
            results.append(pair)
    assert attempts == [0] and len(results) == 1


def test_other_inflight_worker_cannot_make_its_next_request_after_hold(tmp_path):
    gate = ProviderGate(tmp_path / 'hold.sqlite')
    entered, release = Event(), Event()
    class Client:
        card = {'models': {'fixture': {'provider': 'aihubmix'}}}
        sends = 0
        def chat(self, *args, **kwargs):
            self.sends += 1
            entered.set()
            assert release.wait(3)
            return {'ok': True}
    client = Client()
    guarded = GuardedChatClient(client, gate)
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(guarded.chat, [], model='fixture')
        try:
            assert entered.wait(2)
            gate.hold('aihubmix', 'payment_required')
        finally:
            release.set()
        assert result.result() == {'ok': True}
    with pytest.raises(ProviderHeld): guarded.chat([], model='fixture')
    assert client.sends == 1
