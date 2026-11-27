from fastapi.testclient import TestClient
from app.main import app
from app.db import DEMO
from app.catalog import SERIES
import pytest

@pytest.mark.skipif(not DEMO,reason='Run with DEMO_MODE=1')
def test_demo_api():
    with TestClient(app) as c:
        assert c.get('/').status_code==200
        d=c.get('/api/dashboard').json()
        assert d['demo'] and len(d['indicators'])==len(SERIES)
        assert d['model']['allocation'] is None or sum(d['model']['allocation'])==100
        assert c.get('/api/series/cli_us').json()['demo']
        assert c.get('/api/export/cli_us').status_code==200
        assert c.get('/api/revisions/cli_us').json()['revisions']
        assert c.get('/api/series/does_not_exist').status_code==404
        assert c.post('/api/collect').status_code==401
        assert c.post('/api/backtest',json={'csv':'invalid'}).status_code==400
