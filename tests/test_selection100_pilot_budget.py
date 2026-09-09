"""Verify shared cases and proportional qualification gates after cost adaptation."""
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from model_selection_100 import budget100_run as run
from model_selection_100 import run as original


class PilotBudgetTests(unittest.TestCase):
    def test_222_disjoint_queries_and_original_registry_unchanged(self):
        groups=[]
        for stage,n in {'screen':12,'shortlist':60,'validation':150}.items():
            queries=run.selected_queries(stage)
            self.assertEqual(len(queries),n)
            self.assertEqual({loc:sum(q['locale']==loc for q in queries) for loc in ('us','es','jp')},dict.fromkeys(('us','es','jp'),n//3))
            groups.extend(q['query_group_sha256'] for q in queries)
        self.assertEqual(len(set(groups)),222)
        self.assertEqual(original.COUNTS['screen'],24)

    def test_all_pilot_queries_are_retained_in_order(self):
        self.assertEqual(run.selected_queries('screen')[:6],original.selected_queries('screen')[:6])
        self.assertEqual(run.selected_queries('screen'),original.selected_queries('screen')[:12])

    def test_initial_gate_keeps_accuracy_margin_and_requires_12_valid(self):
        base={'complete':True,'overall_score':80,'valid_responses':12,'identity_match_rate':1,
              'ndcg_at_10':.9,'accuracy':.9,'avg_cost_cny':.003}
        groups={run.REFERENCE:dict(base),
                'acceptable':dict(base,overall_score=90,accuracy=.84),
                'excess_accuracy_loss':dict(base,overall_score=95,accuracy=.8),
                'incomplete_reliability':dict(base,overall_score=96,valid_responses=11)}
        with tempfile.TemporaryDirectory() as name:
            out=Path(name)
            (out/'method.json').write_text('{}');(out/'screen_summary.json').write_text('{}')
            with patch.object(run,'OUT',out),patch.object(run,'validate'),patch.object(run,'report',return_value=groups):
                run.choose('screen')
            selection=json.loads((out/'shortlist_selection.json').read_text())
        self.assertEqual(selection['models'],['acceptable',run.REFERENCE])
