import pytest
from pydantic import ValidationError

from commerce_lab.skills import BASELINE, SkillPolicy, SkillRegistry
from commerce_lab.state import BusinessError, Store


def test_candidate_is_inactive_and_transition_can_roll_back(tmp_path):
    registry = SkillRegistry(Store(path=tmp_path/'store.sqlite'))
    policy = SkillPolicy(id='candidate-one', role_guidance={'catalog':'Read details before referring to an ID.'}).checked()
    registry.register(policy, {'source':'development fixture'})
    assert registry.active() == BASELINE
    registry.activate('candidate-one', expected_parent='baseline-v1', evidence={'fixture_gate':True})
    assert registry.active()['id'] == 'candidate-one'
    with pytest.raises(BusinessError, match='stale'):
        registry.activate('candidate-one', expected_parent='baseline-v1', evidence={})
    registry.activate('baseline-v1', expected_parent='candidate-one', evidence={'reason':'fixture regression'}, rollback=True)
    assert registry.active() == BASELINE


def test_registered_version_cannot_be_silently_rewritten(tmp_path):
    registry = SkillRegistry(Store(path=tmp_path/'store.sqlite'))
    policy = SkillPolicy(id='candidate-one').checked()
    registry.register(policy, {})
    registry.register(policy, {})
    with pytest.raises(BusinessError, match='immutable'):
        registry.register({**policy, 'retry_empty_search':True}, {})


def test_skill_cannot_add_tools_guards_or_unbounded_instructions():
    for mutation in ({'tools':['confirm_order']},{'disable_guard':True},{'role_guidance':{'hacker':'code'}},
            {'role_guidance':{'catalog':'x'*1401}}):
        with pytest.raises((ValueError, ValidationError)):
            SkillPolicy.model_validate({**BASELINE, **mutation}).checked()
