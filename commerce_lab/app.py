"""Loopback-only research UI and API. Model jobs use the shared spending ledger."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import json
from pathlib import Path
import re
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import Field

from commerce_lab.agent import Args, CartChange, run_sync
from commerce_lab.state import BusinessError, Store
from commerce_lab.retrieval import RerankedCatalog
from commerce_lab.jobs import JobManager
from research.budget import BudgetLedger
from research.model_config import ROOT, load_env


class RunRequest(Args):
    task: str = Field(min_length=2, max_length=4000)
    model: Literal['qwen3.8-max', 'qwen3.8-flash', 'gpt-5.6-luna'] = 'qwen3.8-max'


def create_app(store: Store | None = None, *, testing=False, runner=run_sync):
    store = store or Store(catalog=RerankedCatalog())
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='commerce-job')
    jobs = JobManager(store)
    pending = {}

    @asynccontextmanager
    async def lifespan(app):
        jobs.recover()
        yield
        for ident,future in list(pending.items()):
            if future.cancel():
                jobs.interrupt(ident,'Application shutdown cancelled this queued job before execution.')
        pool.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title='Commerce & Logistics Lab', lifespan=lifespan)

    @app.middleware('http')
    async def local_origin(request: Request, call_next):
        host = request.headers.get('host', '')
        if not (re.fullmatch(r'(127\.0\.0\.1|localhost)(:\d+)?', host) or testing and host == 'testserver'):
            return JSONResponse({'detail': 'Local host required'}, status_code=403)
        origin = request.headers.get('origin')
        if request.method != 'GET' and origin and origin != 'http://' + host:
            return JSONResponse({'detail': 'Same-origin request required'}, status_code=403)
        return await call_next(request)

    @app.exception_handler(BusinessError)
    async def business_error(request, error):
        return JSONResponse({'detail': str(error)}, status_code=409)

    def session(x_session_id: str = Header()):
        try:
            store.session(x_session_id)
        except BusinessError:
            raise HTTPException(401, 'Create or resume a valid local session') from None
        return x_session_id

    @app.get('/')
    def index():
        return FileResponse(ROOT / 'web/index.html')

    @app.post('/api/sessions')
    def new_session():
        return store.session()

    @app.get('/api/status')
    def status():
        manifest = ROOT / 'evidence/catalog_ingestion.json'
        data = json.loads(manifest.read_text(encoding='utf-8')) if manifest.exists() else {}
        ledger = BudgetLedger(ROOT / 'evidence/api_budget.sqlite', ROOT / 'research/budget_policy.json')
        return {'catalog': data, 'budget': ledger.summary(), 'budget_limit_cny': 300,
                'models': ['qwen3.8-max', 'qwen3.8-flash', 'gpt-5.6-luna'], 'upstream': 'anthropics/commerce-agents',
                'mode': 'local research simulation', 'evaluation_status': 'business final test pending; ranking method frozen',
                'search': 'filtered FTS5 candidates with optional local Qwen reranking; fallback is labelled in each result'}

    @app.get('/api/search')
    def search(q: str, locale: Literal['us', 'es', 'jp'] = 'us', session_id: str = Depends(session)):
        rows = store.catalog.search(q[:200], locale=locale, limit=8)
        store.remember_products(session_id, rows)
        return {'products': rows}

    @app.get('/api/products/{product_id}')
    def product(product_id: str, session_id: str = Depends(session)):
        row = store.catalog.get(product_id)
        if not row:
            raise HTTPException(404, 'Product not found')
        store.remember_products(session_id, [row])
        return {**row, 'stock': store.stock(product_id)}

    @app.get('/api/cart')
    def cart(session_id: str = Depends(session)):
        return store.cart(session_id)

    @app.post('/api/cart')
    def update_cart(change: CartChange, session_id: str = Depends(session)):
        return store.change_cart(session_id, **change.model_dump())

    @app.get('/api/orders')
    def orders(session_id: str = Depends(session)):
        return {'orders': store.orders(session_id)}

    @app.post('/api/runs')
    def start_run(request: RunRequest, session_id: str = Depends(session)):
        run_id = store.new_run(session_id, request.task, exclusive=True)
        def execute():
            try:
                runner(session_id, request.task, run_id, store=store, model=request.model)
            finally:
                jobs.interrupt(run_id,'The application worker ended without recording a terminal result.')
        try:
            jobs.register(run_id)
            future=pool.submit(execute)
            pending[run_id]=future
            future.add_done_callback(lambda done:pending.pop(run_id,None))
        except Exception:
            jobs.interrupt(run_id,'The application could not start this job.')
            raise
        return {'run_id': run_id, 'status': 'queued'}

    @app.get('/api/runs/{run_id}')
    def get_run(run_id: str, session_id: str = Depends(session)):
        result = store.run(session_id, run_id)
        if not result:
            raise HTTPException(404, 'Task not found')
        return result

    @app.post('/api/proposals/{proposal_id}/confirm')
    def confirm(proposal_id: str, session_id: str = Depends(session)):
        return store.confirm_proposal(session_id, proposal_id)

    return app


app = create_app()


if __name__ == '__main__':
    import uvicorn
    config = load_env()
    uvicorn.run(app, host='127.0.0.1', port=int(config.get('COMMERCE_PORT', config.get('PORT', '5174'))))
