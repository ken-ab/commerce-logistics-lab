import math
import pytest

from ranking.metrics import bm25, metrics


def test_esci_gain_order_and_perfect_ranking():
    result = metrics(['C','E','I','S'],[.01,1,0,.1],['c','e','i','s'])
    assert result['ndcg_at_10'] == 1
    assert result['mrr_exact'] == 1


def test_ndcg_uses_given_gains_without_exponentiating_and_ties_are_stable():
    result = metrics(['E','S'],[0,0],['z','a'])
    expected = (.1+1/math.log2(3))/(1+.1/math.log2(3))
    assert result['ndcg_at_10'] == pytest.approx(expected)
    assert result['mrr_exact'] == .5
    assert result['hit_exact_at_1'] == 0


def test_no_relevant_candidates_and_invalid_scores_are_not_hidden():
    assert metrics(['I'],[1],['x'])['ndcg_at_10'] == 0
    with pytest.raises(ValueError):
        metrics(['E'],[float('nan')],['x'])
    with pytest.raises(ValueError):
        metrics(['E','S'],[1,0],['x','x'])


def test_lexical_baseline_handles_accents_and_relevant_terms():
    scores = bm25('camisa azul',['Camisa azul de algodón','Plato rojo de cerámica'])
    assert scores[0] > scores[1]
