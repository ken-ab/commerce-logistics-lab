import json
from threading import Thread
from http.server import ThreadingHTTPServer
from urllib.request import ProxyHandler,Request,build_opener
import pytest

from commerce_lab.retrieval import RerankedCatalog
from serving.reranker import handler,validate_payload

ROWS=[{'id':i,'title':i,'brand':'Test','color':'Blue','description':'Cotton'} for i in ('low','high')]


class CatalogFixture:
    def search(self,query,**filters):
        assert filters=={'limit':100,'locale':'us','color':'Blue','max_price':20}
        return ROWS


def test_reranking_preserves_filters_and_sources_and_labels_invalid_fallback():
    valid=lambda q,r:[{'id':'low','score':.1},{'id':'high','score':.9}]
    for scorer,expected,mode in [(valid,['high','low'],'local_qwen_rerank'),
            (lambda q,r:[{'id':'fabricated','score':1}],['low','high'],'fts_bm25_fallback')]:
        rows=RerankedCatalog(CatalogFixture(),scorer).search('shirt',locale='us',color='Blue',max_price=20)
        assert [r['id'] for r in rows]==expected
        assert all(r['description']=='Cotton' and r['retrieval']['method']==mode for r in rows)


def test_local_http_contract_without_loading_gpu():
    class Model:
        def scores(self,pairs):
            assert len(pairs)==2 and all('Cotton' in doc for _,doc in pairs)
            return [.1,.9]
    server=ThreadingHTTPServer(('127.0.0.1',0),handler(Model()))
    worker=Thread(target=server.serve_forever,daemon=True);worker.start()
    try:
        req=Request(f'http://127.0.0.1:{server.server_port}/rerank',
            data=json.dumps({'query':'shirt','products':ROWS}).encode(),headers={'Content-Type':'application/json'})
        with build_opener(ProxyHandler({})).open(req,timeout=3) as response:
            result=json.load(response)
        assert result['scores']==[{'id':'low','score':.1},{'id':'high','score':.9}]
        with pytest.raises(ValueError):
            validate_payload({'query':'shirt','products':ROWS,'labels':['E','I']})
    finally:
        server.shutdown();server.server_close();worker.join(timeout=3)
