from pathlib import Path
from threading import Event

from fastapi.testclient import TestClient

from commerce_lab.app import create_app
from commerce_lab.state import Store


class EmptyCatalog:
    def get(self, ident):
        return None


def test_http_session_isolation_origin_guard_and_duplicate_job(tmp_path: Path):
    store = Store(EmptyCatalog(), tmp_path / 'business.sqlite')
    release = Event()

    def held_runner(session, task, run_id, **kwargs):
        release.wait(10)
        store.update_run(run_id, 'completed', {'fixture': True})

    try:
        with TestClient(create_app(store, testing=True, runner=held_runner)) as client:
            assert client.get('/api/cart').status_code == 422
            assert client.post('/api/sessions', headers={'Origin': 'https://unrelated.example'}).status_code == 403
            assert client.get('/', headers={'Host': 'unrelated.example'}).status_code == 403
            a = client.post('/api/sessions').json()['id']
            b = client.post('/api/sessions').json()['id']
            ha, hb = {'X-Session-Id': a}, {'X-Session-Id': b}
            job = client.post('/api/runs', headers=ha, json={'task': 'Read policies'})
            assert job.status_code == 200
            ident = job.json()['run_id']
            assert client.get('/api/runs/' + ident, headers=ha).status_code == 200
            assert client.get('/api/runs/' + ident, headers=hb).status_code == 404
            assert client.post('/api/runs', headers=ha, json={'task': 'Duplicate run'}).status_code == 409
            assert client.post('/api/proposals/unknown/confirm', headers=hb).status_code == 409
            release.set()
    finally:
        release.set()
