"""Portable workbench using the same business rules and explicit-operation agents."""
from fastapi import Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from apparel_fulfillment.api import install_apparel
from apparel_fulfillment.interactive_reliability import scoped_operation_agent
from apparel_fulfillment.jobs import AgentJobs
from apparel_fulfillment.store import ApparelStore
from commerce_lab.retrieval import RerankedCatalog
from commerce_lab.state import Store, BusinessError
from delivery import create_delivery_app, run_selected
from delivery_budget import business_client, operational_ledger
from delivery_selected_ranking import refine
from apparel_fulfillment.agent import MODEL
from public_release.evidence import ROOT, verified_snapshot


def create_app(*, store=None, operations=None, jobs=None, testing=False, snapshot_loader=verified_snapshot):
    snapshot = snapshot_loader()
    store = store or Store(catalog=RerankedCatalog())
    operations = operations or ApparelStore()
    jobs = jobs or AgentJobs(operations, state_agent_factory=scoped_operation_agent)

    def runner(session_id, task, run_id=None, *, store, model=MODEL):
        return run_selected(session_id, task, run_id, store=store, model=model, client=business_client())

    def refiner(query, rows):
        return refine(query, rows, selection_loader=lambda: snapshot['ranking_selection'])

    app = create_delivery_app(store, testing=testing, runner=runner,
                              evidence_loader=lambda: snapshot['catalog'], refiner=refiner)
    install_apparel(app, operations=operations, sessions=store, jobs=jobs)
    previous_status = next(r.endpoint for r in app.router.routes if getattr(r, 'path', None) == '/api/status')
    replaced = {'/', '/api/status', '/api/apparel/research', '/api/apparel/status'}
    app.router.routes[:] = [r for r in app.router.routes if getattr(r, 'path', None) not in replaced]

    def owner(identity):
        try:
            store.session(identity)
        except BusinessError:
            raise HTTPException(401, 'Create a valid local session') from None

    def catalogue_available():
        path = getattr(store.catalog, 'path', None)
        return path is None or path.is_file()

    @app.middleware('http')
    async def require_catalogue(request, call_next):
        if (request.url.path == '/api/search' or request.url.path.startswith('/api/products/')) and not catalogue_available():
            return JSONResponse({'detail': 'Full ESCI catalogue is not installed. Run python -m public_release.setup catalog; apparel order tools remain available.'}, status_code=503)
        return await call_next(request)

    @app.get('/')
    def workspace():
        html = (ROOT / 'apparel_web/index.html').read_text(encoding='utf-8')
        html = html.replace('调用所产生的费用计入全项目 480 元额度。', '调用费用受本地配置的预算限制；公开版默认关闭付费调用。')
        notice = ('<p class="notice" style="max-width:1264px;margin:0 auto 20px">'
                  '公开研究版：37 个工作台变体，277 个研究变体；库存、规则、价格和运输均为模拟。'
                  '<a href="/apparel-reliability-research-report" target="_blank" rel="noopener">144 次运输提案对照</a> · '
                  '<a href="/apparel-source-research-report" target="_blank" rel="noopener">商品来源核验</a></p>')
        html = html.replace('<footer>', notice + '<footer>', 1)
        html = html.replace('<script src="/apparel.js"></script>', '<script src="/apparel.js"></script><script src="/apparel-reliability.js"></script>', 1)
        return HTMLResponse(html)

    @app.get('/api/status')
    def status():
        value = previous_status()
        ledger = operational_ledger()
        policy = ledger.policy()
        value.update(budget=ledger.summary(), budget_limit_cny=min(480, policy['total_limit'], policy['automatic_spend_ceiling']),
                     paid_calls_enabled=policy.get('state') == 'ready', distribution='public-research-release')
        value['catalog'] = {**value.get('catalog', {}), 'locally_available': catalogue_available()}
        if not catalogue_available():
            value['catalog']['products'] = 0
            value['catalog']['notice'] = 'The full public catalogue must be built locally; historical study counts are shown separately.'
        return value

    @app.get('/api/apparel/status')
    def apparel_status(x_session_id: str = Header()):
        owner(x_session_id)
        value = status()
        return {'budget_limit_cny': value['budget_limit_cny'], 'budget': value['budget'],
                'dataset_id': operations.base_world['dataset_id'], 'phase': snapshot['apparel_study']['status']}

    @app.get('/api/apparel/research')
    def apparel_research(x_session_id: str = Header()):
        owner(x_session_id)
        return snapshot['apparel_study']

    @app.get('/api/apparel/reliability-policy')
    def reliability_policy():
        return {**snapshot['reliability_policy'], 'active_job_id': jobs.active}

    @app.get('/api/apparel/source-policy')
    def source_policy():
        return {**snapshot['source_policy'], 'active_job_id': jobs.active}

    @app.get('/api/ranking/selection')
    def ranking_selection():
        return snapshot['ranking_selection']

    files = {'/catalog': ('delivery_web/index.html', 'text/html'),
             '/apparel.js': ('apparel_web/app.js', 'application/javascript'),
             '/apparel-reliability.js': ('apparel_web/reliability.js', 'application/javascript'),
             '/apparel-research-report': ('research/APPAREL_RESULTS.md', 'text/markdown'),
             '/apparel-state-research-report': ('research/APPAREL_STATE_REPLICATION_RESULTS.md', 'text/markdown'),
             '/apparel-source-research-report': ('research/APPAREL_SOURCE_REVIEW_RESULTS.md', 'text/markdown'),
             '/apparel-reliability-research-report': ('research/APPAREL_RELIABILITY_RESULTS.md', 'text/markdown')}
    def serve(name, media_type):
        def endpoint():
            return FileResponse(ROOT / name, media_type=media_type + '; charset=utf-8')
        return endpoint
    for url, (name, media_type) in files.items():
        app.add_api_route(url, serve(name, media_type), methods=['GET'], include_in_schema=False)
    return app
