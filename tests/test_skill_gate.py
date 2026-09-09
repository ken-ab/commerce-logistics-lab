import pytest
from evaluation.skill_gate import compare_rows


def row(family,passed):
    return {'family':family,'score':{'passed':passed}}


def test_equal_overall_success_cannot_hide_stock_regression():
    baseline={'stock':row('stock_shortage',True),'description':row('describe_only',False)}
    candidate={'stock':row('stock_shortage',False),'description':row('describe_only',True)}
    result=compare_rows(baseline,candidate)
    assert result['critical_regressions']==['stock']
    assert result['improved']==['description']


def test_dropping_failed_case_invalidates_comparison():
    with pytest.raises(ValueError,match='membership'):
        compare_rows({'failure':row('stock_shortage',False)}, {})
