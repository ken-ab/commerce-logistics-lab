"""Focused checks for paid-request safety, failure accounting and streaming clocks."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import io
import json
import math
from pathlib import Path
import tempfile
import unittest

from model_selection_100.budget import SelectionBudget
from model_selection_100.client import SelectionClient, consume, identity_key
from model_selection_100.metrics import decode_order, score, summarize
from model_selection_100.configured import decode
from model_selection_100.run import selected_queries
from research.budget import BudgetExceeded


class SelectionSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        policy=self.root/'policy.json'
        policy.write_text(json.dumps({'currency':'CNY','total_limit':480,'automatic_spend_ceiling':480,
                         'maximum_per_call_cny':480,'state':'ready'}))
        self.ledger=SelectionBudget(self.root/'ledger.sqlite',policy)

    def reserve(self,cost,purpose):
        return self.ledger.reserve(maximum_cny=str(cost),purpose='model-selection-100:'+purpose,
                                   model='fixture',price_version='fixture')

    def test_old_spend_is_not_reset_and_races_cannot_exceed_480(self):
        with closing(self.ledger.connect()) as db, db:
            db.execute("INSERT INTO calls VALUES ('old','2026-09-07','legacy-experiment','fixture','fixture',479000000,NULL,'uncertain',NULL,NULL)")
        def attempt(i):
            try:self.reserve(.6,str(i));return True
            except BudgetExceeded:return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(attempt,range(8))),1)
        self.assertEqual(self.ledger.summary()['accounted_and_reserved_cny'],'479.6')

    def test_same_request_cannot_be_billed_twice(self):
        self.reserve(.1,'a')
        with self.assertRaisesRegex(BudgetExceeded,'duplicate'):
            self.reserve(.1,'a')

    def test_a_policy_edit_cannot_raise_user_authorization(self):
        p=self.ledger.policy();p['total_limit']=p['automatic_spend_ceiling']=999
        self.ledger.policy_path.write_text(json.dumps(p))
        with self.assertRaises(BudgetExceeded):self.reserve(481,'overspend')

    def test_new_task_cap_counts_existing_calibration_and_cannot_be_raised_by_config(self):
        self.reserve(99.9,'calibration:prior')
        policy=self.ledger.policy();policy['selection_task_limit_cny']=480
        self.ledger.policy_path.write_text(json.dumps(policy))
        with self.assertRaisesRegex(BudgetExceeded,'task CNY 100'):
            self.reserve(.2,'later')

    def test_screening_reserves_room_for_final_validation(self):
        self.reserve(81.9,'calibration:prior')
        with self.assertRaisesRegex(BudgetExceeded,'screening allocation'):
            self.reserve(.2,'screen:one')
        self.reserve(.2,'validation:one')

    def test_cost_overrun_halts_all_models(self):
        call=self.reserve(.1,'a')
        with self.assertRaises(BudgetExceeded):self.ledger.settle(call,cost_cny='.2',usage={'prompt_tokens':1})
        with self.assertRaises(BudgetExceeded):self.reserve(.01,'b')

    def test_client_does_not_double_bill_reasoning_or_infer_missing_usage(self):
        registry=self.root/'evidence/model_selection_100/candidates.json'
        registry.parent.mkdir(parents=True)
        registry.write_text(json.dumps({'models':[{'id':'fixture','reasoning_effort':'none',
                            'pricing_usd_per_million':{'input':1,'output':2}}]}))
        policy=self.root/'model_selection_100/budget_policy.json';policy.parent.mkdir()
        policy.write_text(self.ledger.policy_path.read_text())
        def transport(_request):
            f=io.StringIO(json.dumps({'model':'fixture','choices':[{'message':{'content':'{"order":["a"]}'},
                          'finish_reason':'stop'}],'usage':{'prompt_tokens':100,'completion_tokens':30,
                          'completion_tokens_details':{'reasoning_tokens':20}}}))
            f.headers={'Content-Type':'application/json'};return f
        client=SelectionClient(root=self.root,config={'AIHUBMIX_BASE_URL':'https://aihubmix.com/v1',
                                                     'AIHUBMIX_API_KEY':'fixture-only'},transport=transport)
        r=client.chat('fixture',{'query':'fixture'},purpose='model-selection-100:test:one',
                      reservation_record=self.root/'started.json')
        self.assertTrue(r['api_success'])
        self.assertAlmostEqual(float(r['estimated_cost_cny']),(100+30*2)*8/1_000_000)
        self.assertEqual(r['usage']['reasoning_tokens'],20)
        self.assertIsNone(r['timing']['ttft_seconds'])


class MetricTests(unittest.TestCase):
    def test_only_registered_models_get_inline_reasoning_adapter(self):
        response={'message':{'content':'<think>Fixture reasoning.</think>\n{"order":["b","a"]}'},'finish_reason':'stop'}
        self.assertEqual(decode({'inline_think_adapter':True},response,['a','b']),['b','a'])
        with self.assertRaises(ValueError):decode({},response,['a','b'])
        broken={'message':{'content':'<think>not closed {"order":["b","a"]}'},'finish_reason':'stop'}
        with self.assertRaises(ValueError):decode({'inline_think_adapter':True},broken,['a','b'])

    def test_active_samples_are_disjoint_and_locale_balanced(self):
        groups=[]
        for stage,count in [('screen',24),('shortlist',60),('validation',150)]:
            rows=selected_queries(stage)
            self.assertEqual(len(rows),count)
            for locale in ('us','es','jp'):
                self.assertEqual(sum(q['locale']==locale for q in rows),count//3)
            groups.extend(q['query_group_sha256'] for q in rows)
        self.assertEqual(len(set(groups)),234)

    def test_exact_permutation_required(self):
        for content in ['{"order":["a","a"]}','{"order":["a"]}','{"order":["a","unknown"]}',
                        'Explanation {"order":["a","b"]}']:
            with self.assertRaises((ValueError,TypeError)):
                decode_order({'content':content},['a','b'],'stop')
        self.assertEqual(decode_order({'content':'```json\n{"order":["b","a"]}\n```'},['a','b'],'stop'),['b','a'])

    def test_truncated_answers_are_not_counted_as_success(self):
        with self.assertRaises(ValueError):decode_order({'content':'{"order":["a"]}'},['a'],'length')

    def test_failed_requests_remain_in_quality_cost_and_latency(self):
        def row(valid,cost,latency):
            return {'submitted':True,'valid_response':valid,'status':'valid' if valid else 'http_error',
                'metrics':{'ndcg_at_10':1,'hit_exact_at_1':1,'mrr_exact':1},
                'pipeline_metrics':{'ndcg_at_10':1,'hit_exact_at_1':1,'mrr_exact':1},
                'response':{'api_success':valid,'identity_match':valid,'accounted_and_reserved_cny':str(cost),
                   'estimated_cost_cny':str(cost) if valid else None,'latency_seconds':latency,
                   'usage':{'prompt_tokens':30,'completion_tokens':5} if valid else {}}}
        r=summarize([row(True,.01,2),row(False,.09,90)],2)
        self.assertEqual(r['accuracy'],.5)
        self.assertEqual(r['ndcg_at_10'],.5)
        self.assertEqual(r['pipeline_with_fallback_metrics']['hit_exact_at_1'],1)
        self.assertAlmostEqual(r['avg_cost_cny'],.05)
        self.assertEqual(r['avg_latency_seconds'],46)
        self.assertEqual(r['effective_success_rate'],.5)
        self.assertEqual(r['unknown_cost_calls'],1)
        self.assertIsNone(r['avg_reasoning_tokens'])
        self.assertIsNone(summarize([row(True,.01,2)],2)['overall_score'])

    def test_more_expensive_or_slower_equal_quality_does_not_win(self):
        base=score(.8,.9,.001,2,1)['overall_score']
        self.assertLess(score(.8,.9,.01,2,1)['overall_score'],base)
        self.assertLess(score(.8,.9,.001,10,1)['overall_score'],base)
        self.assertLess(score(.7,.8,.001,2,1)['overall_score'],base)
        with self.assertRaises(ValueError):score(.8,.9,.001,math.nan,1)

    def test_identity_is_not_fuzzy_family_matching(self):
        self.assertEqual(identity_key('gpt-5.6-luna'),identity_key('gpt-56-luna'))
        self.assertEqual(identity_key('claude-haiku-4-5-20251001'),identity_key('claude-haiku-4-5'))
        self.assertNotEqual(identity_key('gpt-5.6-luna'),identity_key('gpt-5.6-sol'))

    def test_stream_clock_separates_first_answer_from_reasoning(self):
        class Stream:
            headers={'Content-Type':'text/event-stream'}
            def __iter__(self):
                chunks=[{'choices':[{'delta':{'role':'assistant'}}]},
                    {'choices':[{'delta':{'reasoning_content':'x'}}]},
                    {'choices':[{'delta':{'reasoning_content':'y'}}]},
                    {'model':'fixture','choices':[{'delta':{'content':'{"order":'}}]},
                    {'choices':[{'delta':{'content':'["a"]}'},'finish_reason':'stop'}]},
                    {'usage':{'prompt_tokens':10,'completion_tokens':20},'choices':[]}]
                return iter([('data: '+json.dumps(c)+'\n').encode() for c in chunks]+[b'data: [DONE]\n'])
        times=iter([.5,1,2,3,4,4.5,5])
        result,t=consume(Stream(),0,clock=lambda:next(times))
        self.assertEqual(t['ttft_seconds'],1)
        self.assertEqual(t['time_to_first_answer_seconds'],3)
        self.assertEqual(t['answer_stream_span_seconds'],1)
        self.assertEqual(t['observed_reasoning_span_seconds'],1)
        self.assertIsNone(t['pure_thinking_seconds'])
        self.assertEqual(result['choices'][0]['message']['content'],'{"order":["a"]}')

    def test_nonstream_response_does_not_fabricate_ttft(self):
        f=io.StringIO('{"choices":[]}');f.headers={'Content-Type':'application/json'}
        _,timing=consume(f,0)
        self.assertTrue(all(v is None for v in timing.values()))


if __name__=='__main__':unittest.main()
