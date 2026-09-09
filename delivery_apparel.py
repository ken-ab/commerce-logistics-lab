"""Apparel research workspace alongside the existing, measured catalogue app."""
from fastapi.responses import FileResponse

from apparel_fulfillment.api import install_apparel
from delivery_replication import create_app as create_measured_app
from research.model_config import ROOT


def create_app():
    app = create_measured_app()
    app.router.routes[:] = [r for r in app.router.routes if getattr(r, 'path', None) != '/']
    install_apparel(app)

    @app.get('/')
    def workspace():
        return FileResponse(ROOT / 'apparel_web/index.html')

    @app.get('/catalog')
    def measured_catalogue():
        return FileResponse(ROOT / 'delivery_web/index.html')

    @app.get('/apparel.js')
    def apparel_script():
        return FileResponse(ROOT / 'apparel_web/app.js', media_type='application/javascript')

    @app.get('/apparel-research-report')
    def apparel_report():
        path = ROOT / 'research/APPAREL_RESULTS.md'
        if not path.exists(): path = ROOT / 'research/APPAREL_EXPERIMENT_PROTOCOL.md'
        return FileResponse(path, media_type='text/markdown; charset=utf-8')

    @app.get('/apparel-state-research-report')
    def apparel_state_report():
        return FileResponse(ROOT / 'research/APPAREL_STATE_REPLICATION_RESULTS.md', media_type='text/markdown; charset=utf-8')

    return app


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(create_app(), host='127.0.0.1', port=5176)
