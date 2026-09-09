"""Read-only local reranking; preserve filtered catalog records and provenance."""
import json
import math
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from commerce_lab.catalog import Catalog


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        return None


def request_scores(query, rows):
    payload = {'query':query,'products':[{k:r[k] for k in ('id','title','brand','color','description')} for r in rows]}
    request = Request('http://127.0.0.1:5175/rerank',data=json.dumps(payload).encode(),
        headers={'Content-Type':'application/json'},method='POST')
    with build_opener(NoRedirect,ProxyHandler({})).open(request,timeout=25) as response:
        raw = response.read(65537)
    if len(raw)>65536:
        raise ValueError('Oversized local ranking response')
    result = json.loads(raw)
    if not isinstance(result,dict) or result.get('model') != 'Qwen/Qwen3-Reranker-0.6B' or result.get('instruction') != 'product':
        raise ValueError('Unexpected local ranking method')
    return result['scores']


class RerankedCatalog:
    def __init__(self, catalog=None, scorer=request_scores):
        self.catalog = catalog or Catalog()
        self.path = getattr(self.catalog,'path',None)
        self.scorer = scorer

    def get(self, product_id):
        return self.catalog.get(product_id)

    def search(self, query, *, limit=8, **filters):
        limit = max(1,min(limit,100))
        rows = self.catalog.search(query,limit=100,**filters)
        if not rows:
            return []
        mode, reason = 'local_qwen_rerank', None
        try:
            predictions = self.scorer(query,rows)
            if (len(predictions)!=len(rows) or len({p['id'] for p in predictions})!=len(rows)
                    or {p['id'] for p in predictions}!={r['id'] for r in rows}):
                raise ValueError('Ranking must preserve exact candidate membership')
            scores = {p['id']:p['score'] for p in predictions}
            if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or not 0<=v<=1 for v in scores.values()):
                raise ValueError('Invalid local ranking score')
            rows = sorted(rows,key=lambda r:(-scores[r['id']],r['id']))
        except (OSError,ValueError,KeyError,TypeError):
            mode, reason = 'fts_bm25_fallback', 'local_reranker_unavailable_or_invalid'
        metadata = {'method':mode,'candidate_count':len(rows),'candidate_source':'filtered FTS5 top 100',
            'scope':'Reranking cannot recover products missing from the lexical candidate pool.'}
        if reason:
            metadata['fallback_reason'] = reason
        return [{**r,'retrieval':metadata} for r in rows[:limit]]
