import os
from commerce_lab.jobs import JobManager,process_identity
from commerce_lab.state import Store


class EmptyCatalog:
    def get(self,ident):
        return None


def test_only_jobs_with_verified_dead_or_replaced_owners_are_recovered(tmp_path):
    store=Store(EmptyCatalog(),tmp_path/'business.sqlite')
    sid=store.session()['id']
    manager=JobManager(store,identity_lookup=lambda pid:'original-process')
    owned=store.new_run(sid,'Owned job')
    unrelated=store.new_run(sid,'Unowned external job')
    manager.register(owned)
    assert manager.recover()==[]
    manager.identity_lookup=lambda pid:'unknown'
    assert manager.recover()==[]
    manager.identity_lookup=lambda pid:'reused-pid-with-new-creation-time'
    assert manager.recover()==[owned]
    assert store.run(sid,owned)['result']['state_preserved'] is True
    assert store.run(sid,unrelated)['status']=='queued'
    assert manager.recover()==[]


def test_current_process_identity_is_observable_without_signalling_it():
    first=process_identity(os.getpid())
    assert first not in (None,'unknown')
    assert process_identity(os.getpid())==first
