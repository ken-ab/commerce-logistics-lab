"""UI recovery fixes over the unchanged v1 agent; no report-prototype activation."""
from contextlib import closing

from fastapi import Header, HTTPException
from fastapi.responses import FileResponse

from commerce_lab.agent import run_sync
from commerce_lab.app import create_app
from commerce_lab.retrieval import RerankedCatalog
from commerce_lab.state import BusinessError, Store
from research.model_config import ROOT, load_env


def create_ui_app(store=None, *, testing=False, runner=run_sync):
    store = store or Store(catalog=RerankedCatalog())
    app = create_app(store, testing=testing, runner=runner)
    # Keep all original API behavior/lifespan; only replace the root document.
    app.router.routes[:] = [route for route in app.router.routes
                           if not (getattr(route, 'path', None) == '/' and 'GET' in getattr(route, 'methods', set()))]

    @app.get('/')
    def index():
        return FileResponse(ROOT/'web_v2/index.html')

    @app.get('/api/proposals/{proposal_id}')
    def proposal_status(proposal_id: str, x_session_id: str = Header()):
        try:
            store.session(x_session_id)
        except BusinessError:
            raise HTTPException(401, 'Create or resume a valid local session') from None
        with closing(store.connect()) as db:
            row = db.execute('SELECT order_id FROM proposals WHERE id=? AND session_id=?',
                             (proposal_id, x_session_id)).fetchone()
        if row is None:
            raise HTTPException(404, 'Proposal not found in this session')
        return {'proposal_id': proposal_id, 'status': 'confirmed' if row['order_id'] else 'awaiting_local_confirmation',
                'order_id': row['order_id']}

    return app


app = create_ui_app()


if __name__ == '__main__':
    import uvicorn
    config = load_env()
    uvicorn.run(app, host='127.0.0.1', port=int(config.get('COMMERCE_PORT', config.get('PORT','5174'))))
