"""Opt-in refinement preserves product identity and never bypasses evidence/session checks."""
import copy,json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import delivery_ranking as ranking
from commerce_lab.state import BusinessError
from research.budget import BudgetLedger


def products(n=10):
    return [{'id':f'us:p{i}','title':f'Public product {i}','brand':'fixture','color':'blue',
             'description':'public description','retrieval':{'method':'local_qwen_rerank'}} for i in range(n)]


class Client:
    def __init__(self,path,invalid=False):
        self.ledger=BudgetLedger(path/'ledger.sqlite',path/'policy.json');self.calls=[];self.invalid=invalid
    def chat(self,messages,**kwargs):
        self.calls.append((messages,kwargs))
        payload=json.loads(messages[1]['content'])
        aliases=[c['id'] for c in payload['candidates']][::-1]
        if self.invalid:aliases[-1]=aliases[0]
        return {'message':{'tool_calls':[{'function':{'name':'rank_candidates','arguments':json.dumps({'order':aliases})}}]},
                'finish_reason':'tool_calls','estimated_cost_cny':'0','latency_seconds':0}


def test_refinement_only_reorders_existing_products(tmp_path,monkeypatch):
    monkeypatch.setattr(ranking,'AUDITS',tmp_path/'audits')
    rows=products();before=copy.deepcopy(rows);client=Client(tmp_path)
    output,meta=ranking.refine('blue shirt',rows,client=client,selection_loader=lambda:{'fixture':True})
    assert {r['id'] for r in output}=={r['id'] for r in rows}
    assert rows==before and meta['status']=='refined' and len(client.calls)==1
    payload=json.loads(client.calls[0][0][1]['content'])
    assert all(set(c)=={'id','text'} for c in payload['candidates'])
    assert all('us:p' not in c['text'] for c in payload['candidates'])
    assert client.calls[0][1]['model']=='gpt-5.6-luna'


def test_invalid_model_permutation_falls_back_without_retry(tmp_path,monkeypatch):
    monkeypatch.setattr(ranking,'AUDITS',tmp_path/'audits')
    rows=products();client=Client(tmp_path,invalid=True)
    output,meta=ranking.refine('blue shirt',rows,client=client,selection_loader=lambda:{})
    assert output==rows and meta['status']=='fallback' and len(client.calls)==1
    assert json.loads(next((tmp_path/'audits').glob('*.json')).read_text())['response']


def test_invalid_evidence_stops_before_any_model_call(tmp_path,monkeypatch):
    monkeypatch.setattr(ranking,'AUDITS',tmp_path/'audits');client=Client(tmp_path)
    def rejected():raise ValueError('Hash mismatch')
    output,meta=ranking.refine('blue shirt',products(),client=client,selection_loader=rejected)
    assert meta['status']=='fallback' and not client.calls


def test_missing_local_reranker_does_not_silently_change_the_measured_strategy(tmp_path,monkeypatch):
    monkeypatch.setattr(ranking,'AUDITS',tmp_path/'audits');client=Client(tmp_path)
    rows=products();rows[0]['retrieval']['method']='fts_bm25_fallback'
    output,meta=ranking.refine('blue shirt',rows,client=client,selection_loader=lambda:{})
    assert output==rows and meta['status']=='fallback' and not client.calls


def test_endpoint_checks_session_and_input_before_paid_refinement():
    class Catalog:
        def search(self,query,**kw):assert kw['limit']==10;return products()
    class Store:
        catalog=Catalog()
        seen=[]
        def session(self,sid):
            if sid!='fixture-session':raise BusinessError('Unknown local session')
        def remember_products(self,sid,rows):self.seen=rows
    store=Store();calls=[]
    def refiner(q,rows):calls.append(q);return rows[::-1],{'status':'refined','accounted_and_reserved_cny':'0'}
    app=FastAPI();ranking.install_refinement(app,store,refiner=refiner)
    with TestClient(app) as client:
        assert client.post('/api/search/refine',json={'query':'blue shirt'}).status_code==422
        assert client.post('/api/search/refine',json={'query':'blue shirt'},headers={'X-Session-Id':'wrong'}).status_code==401
        assert client.post('/api/search/refine',json={'query':'x'*201},headers={'X-Session-Id':'fixture-session'}).status_code==422
        assert calls==[]
        r=client.post('/api/search/refine',json={'query':'blue shirt'},headers={'X-Session-Id':'fixture-session'})
        assert r.status_code==200 and len(r.json()['products'])==8 and len(store.seen)==8 and len(calls)==1
