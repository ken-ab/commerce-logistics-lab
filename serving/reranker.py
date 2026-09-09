"""Loopback-only Qwen service using the frozen local ranking implementation."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock

from ranking.model import LocalReranker


def validate_payload(data):
    if not isinstance(data,dict) or set(data)!={'query','products'}:
        raise ValueError('Expected query and products')
    if not isinstance(data['query'],str) or not 1<=len(data['query'])<=200:
        raise ValueError('Query length must be 1 to 200')
    rows = data['products']
    if not isinstance(rows,list) or not 1<=len(rows)<=100:
        raise ValueError('Expected 1 to 100 candidates')
    limits = {'id':40,'title':5000,'brand':1000,'color':1000,'description':1200}
    for row in rows:
        if not isinstance(row,dict) or set(row)!=set(limits):
            raise ValueError('Unexpected product fields')
        if any(not isinstance(row[k],str) or len(row[k])>size for k,size in limits.items()):
            raise ValueError('Invalid product text')
    if len({r['id'] for r in rows})!=len(rows) or any(not r['id'] for r in rows):
        raise ValueError('Unique nonempty product IDs are required')
    return data['query'],rows


def handler(model):
    inference = Lock()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass  # Do not log queries or bodies into access logs.

        def reply(self,status,data):
            body=json.dumps(data).encode()
            self.send_response(status)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def local_request(self):
            host=self.headers.get('Host','')
            expected={f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'}
            return host in expected and self.headers.get('Origin') in (None,'http://'+host)

        def do_GET(self):
            if not self.local_request():
                return self.reply(403,{'error':'local host required'})
            return self.reply(200 if self.path=='/health' else 404,
                {'ready':self.path=='/health','model':'Qwen/Qwen3-Reranker-0.6B','instruction':'product'})

        def do_POST(self):
            if not self.local_request():
                return self.reply(403,{'error':'local host required'})
            if self.path!='/rerank':
                return self.reply(404,{'error':'unknown endpoint'})
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 1<=size<=524288 or self.headers.get('Content-Type')!='application/json':
                    raise ValueError('Invalid JSON request size or content type')
                self.connection.settimeout(15)
                query,rows=validate_payload(json.loads(self.rfile.read(size)))
            except (ValueError,TypeError,OSError):
                return self.reply(400,{'error':'invalid bounded ranking request'})
            if not inference.acquire(blocking=False):
                return self.reply(503,{'error':'local ranker busy'})
            try:
                pairs=[(query,f"Title: {r['title']}\nBrand: {r['brand']}\nColor: {r['color']}\nDetails: {r['description']}") for r in rows]
                scores=model.scores(pairs)
                self.reply(200,{'model':'Qwen/Qwen3-Reranker-0.6B','instruction':'product',
                    'scores':[{'id':r['id'],'score':s} for r,s in zip(rows,scores)]})
            except Exception:
                self.reply(503,{'error':'local inference failed'})
            finally:
                inference.release()
    return Handler


def main():
    model=LocalReranker(instruction='product',batch_size=16,max_tokens=512)
    print(json.dumps({'ready':True,'address':'http://127.0.0.1:5175','optimization_check':model.verify_last_token_optimization()}),flush=True)
    ThreadingHTTPServer(('127.0.0.1',5175),handler(model)).serve_forever()


if __name__=='__main__':
    main()
