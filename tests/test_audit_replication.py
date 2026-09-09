from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from audit_reliability.limits import IncrementalBudget
from audit_replication.assess import critical_issues,quality_checks
from audit_replication.prepare import collect_ids
from audit_replication.run import restore
from research.budget import BudgetExceeded


class ReplicationTests(unittest.TestCase):
    def test_a_failed_critical_audit_still_rejects_an_otherwise_passing_candidate(self):
        state = {'cart':{'items':[]},'latest_proposal':None,'orders':[]}
        issues = critical_issues({'family':'stage_feasible'},state,state,False)
        base = {'counts':{'business':32,'facts':23,'communication':26,'joint':17},
            'critical_issues':[],'settled_cost_cny':'.54','latency_median_seconds':20}
        candidate = {**base,'counts':{'business':32,'facts':30,'communication':31,'joint':29},
            'critical_issues':issues,'settled_cost_cny':'.50','latency_median_seconds':19}
        checks,_,_ = quality_checks(base,candidate)
        self.assertEqual([k for k,v in checks.items() if not v],['no_critical_issues'])

    def test_cart_proposal_and_order_mutations_remain_critical(self):
        before = {'cart':{'items':[]},'latest_proposal':None,'orders':[]}
        after = {'cart':{'items':[{'product_id':'us:x','quantity':1}]},'latest_proposal':{'id':'p'},'orders':[{'id':'o'}]}
        reasons = critical_issues({'family':'describe_only'},before,after,True)
        self.assertEqual(len(reasons),3)
        self.assertEqual(critical_issues({'family':'describe_only'},before,deepcopy(before),True),[])

    def test_started_partial_trial_retains_business_and_is_not_regenerated(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'started').mkdir()
            case = {'id':'fixture','family':'describe_only','response_language':'en'}
            (root/'started/fixture.json').write_text('{}')
            (root/'fixture').mkdir()
            business = {'case_id':'fixture','family':'describe_only','score':{'passed':True}}
            (root/'fixture/business_result.json').write_text(json.dumps(business))
            row = restore(root,case,'structured_multi')
            self.assertEqual(row['business'],business)
            self.assertTrue(row['interrupted'])
            self.assertTrue(all(row['audit'][kind]['status']=='audit_failed' for kind in ('facts','communication')))
            self.assertEqual(restore(root,case,'structured_multi'),row)

    def test_unstarted_trial_remains_pending(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertIsNone(restore(Path(temp),{'id':'fixture'},'structured_multi'))

    def test_incremental_limit_includes_all_retained_global_reservations(self):
        ledger = Mock()
        ledger.summary.side_effect = [{'accounted_and_reserved_cny':'154'},
            {'accounted_and_reserved_cny':'165.5'},{'accounted_and_reserved_cny':'165.5'}]
        limit = IncrementalBudget(ledger,12)
        with self.assertRaises(BudgetExceeded):
            limit.reserve(maximum_cny='0.6',purpose='fixture',model='fixture',price_version='fixture')
        ledger.reserve.assert_not_called()
        limit.reserve(maximum_cny='0.4',purpose='fixture',model='fixture',price_version='fixture')
        ledger.reserve.assert_called_once()

    def test_exclusions_cover_both_nested_singular_and_plural_product_ids(self):
        ids = set()
        collect_ids({'a':[{'product_id':'us:a'},{'nested':{'product_ids':['us:b',None]}}]},ids)
        self.assertEqual(ids,{'us:a','us:b'})


if __name__=='__main__':
    unittest.main()
