"""4.8: isolated DB snapshots, restoration, auth, retries and journal replacement."""
import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from contextlib import closing
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select, func
from sqlalchemy.orm import sessionmaker
from app.main import app
from app import operations as op, ops_restore, collect as coll, journal
from app.db import Base, Session, init, Obs, Status, Revision, Run, now
from app.settings import load_settings

AUTH=('admin','test-admin-password-1234');HEAD={'X-Macro-Admin':'1'}

@pytest.fixture
def isolated(monkeypatch,tmp_path):
    # Independent DB and settings, no interference with other tests or real credentials.
    engine=create_engine('sqlite:///'+str(tmp_path/'db.sqlite3'))
    session=sessionmaker(bind=engine,expire_on_commit=False)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(op,'engine',engine);monkeypatch.setattr(op,'Session',session)
    monkeypatch.setattr(ops_restore,'engine',engine)
    monkeypatch.setenv('BACKUP_DIR',str(tmp_path/'backups'))
    monkeypatch.setenv('SETTINGS_DIR',str(tmp_path/'settings'))
    yield session
    engine.dispose()


def test_backup_contains_consistent_sqlite_data_and_excludes_settings(isolated,tmp_path):
    from app.settings import save_settings
    save_settings({'DART_API_KEY':'private-example-key'})
    with isolated.begin() as db:db.add(Obs(series='cpi',period=date(2024,1,1),value=111,fetched=now()))
    m=op.create_backup();assert m['format']=='macro-db-v1' and m['demo'] is True
    found,p=op.verify(m['id']);assert found==m and op.list_items()==[m]
    with closing(sqlite3.connect(p)) as db:
        assert db.execute('SELECT value FROM observations WHERE series=?',('cpi',)).fetchone()[0]==111
    assert b'private-example-key' not in p.read_bytes() and not (p.parent/'settings.enc').exists()
    assert set(x.suffix for x in p.parent.iterdir())=={'.sqlite3','.json'}


def test_restore_roundtrip_and_pause_automation(isolated):
    with isolated.begin() as db:db.add(Obs(series='cpi',period=date(2024,1,1),value=111,fetched=now()))
    m=op.create_backup()
    with isolated.begin() as db:db.get(Obs,('cpi',date(2024,1,1))).value=222
    with pytest.raises(op.OpsError):ops_restore.restore(m['id'],'NO')
    with op.instance_lock():
        with pytest.raises(op.OpsError):ops_restore.restore(m['id'],'RESTORE')
    sid=ops_restore.restore(m['id'],'RESTORE');assert sid!=m['id']
    with isolated() as db:assert db.get(Obs,('cpi',date(2024,1,1))).value==111
    prev,p=op.verify(sid);assert prev['reason']=='before-restore'
    with closing(sqlite3.connect(p)) as db:assert db.execute('SELECT value FROM observations').fetchone()[0]==222
    assert all(load_settings()[k]=='0' for k in ('AUTO_COLLECT','ALERT_ENABLED','BACKUP_ENABLED'))


def test_hash_corruption_schema_and_id_validation(isolated):
    m=op.create_backup();_,p=op.item(m['id']);raw=bytearray(p.read_bytes());raw[-1]^=1;p.write_bytes(raw)
    with pytest.raises(op.OpsError,match='SHA-256'):op.verify(m['id'])
    for bad in ('../db','a'*500,'..','settings.enc'):
        with pytest.raises(op.OpsError):op.item(bad)
    with closing(sqlite3.connect(p.parent/'wrong.sqlite3')) as db:db.execute('CREATE TABLE fake(x)');db.commit()
    with pytest.raises(op.OpsError,match='스키마'):op.sqlite_check(p.parent/'wrong.sqlite3')


def test_backup_auth_download_verify_delete(isolated):
    c=TestClient(app)
    assert c.get('/operations').status_code==401 and c.get('/api/operations').status_code==401
    assert c.post('/api/operations/backup',auth=AUTH).status_code==403
    assert c.post('/api/operations/backup',auth=AUTH,headers={**HEAD,'Origin':'https://evil.invalid'}).status_code==403
    r=c.post('/api/operations/backup',auth=AUTH,headers=HEAD);assert r.status_code==200,r.text
    m=r.json();ident=m['id'];url='/api/operations/backups/'+ident
    assert c.get(url+'/download').status_code==401
    r=c.get(url+'/download',auth=AUTH);assert r.status_code==200 and len(r.content)==m['size'] and r.headers['cache-control']=='no-store'
    assert c.get(url+'/download?metadata=true',auth=AUTH).json()==m
    assert c.post(url+'/verify',auth=AUTH,headers=HEAD).json()['ok']
    assert c.delete(url+'?confirm=true',auth=AUTH,headers=HEAD).status_code==400
    second=c.post('/api/operations/backup',auth=AUTH,headers=HEAD).json()
    assert c.delete(url,auth=AUTH,headers=HEAD).status_code==400
    assert c.delete(url+'?confirm=true',auth=AUTH,headers=HEAD).json()['state']=='deleted'
    assert c.get(url+'/download',auth=AUTH).status_code==404
    status=c.get('/api/operations',auth=AUTH).json();assert status['version']=='4.8.0' and len(status['indicators'])==38 and len(status['backups'])==1 and status['backups'][0]['id']==second['id']
    assert c.get('/operations',auth=AUTH).status_code==200


def test_automatic_rotation_never_deletes_manual(isolated,monkeypatch):
    manual=op.create_backup();first=op.create_backup('automatic');second=op.create_backup('automatic')
    op.prune_automatic(1)
    ids={m['id'] for m in op.list_items()};assert ids=={manual['id'],second['id']}
    assert not (op.root()/(first['id']+'.sqlite3')).exists()
    # Daily attempt record prevents duplicate runs even when process restarts.
    monkeypatch.setattr(op,'DEMO',False);monkeypatch.setattr(op,'load_settings',lambda:{'BACKUP_ENABLED':'1','BACKUP_HOUR_KST':'0','BACKUP_KEEP':'3'})
    calls=[];monkeypatch.setattr(op,'create_backup',lambda reason:calls.append(reason));op.auto_once();op.auto_once()
    assert calls==['automatic'] and op.run_state()['state']=='ok'


def test_backup_disk_guard_and_lock(isolated,monkeypatch):
    import shutil
    monkeypatch.setattr(op.shutil,'disk_usage',lambda p:shutil._ntuple_diskusage(1024,1024,0))
    with pytest.raises(op.OpsError,match='공간'):op.create_backup()
    assert op.list_items()==[]
    with op.instance_lock():
        with pytest.raises(op.OpsError):
            with op.instance_lock():pass


def test_integrity_future_and_stale_and_unknown(isolated):
    with isolated.begin() as db:
        for sid,day in [('cpi',date(2000,1,1)),('sp500',op.today()+timedelta(days=1)),('not-a-series',date(2000,1,1))]:db.add(Obs(series=sid,period=day,value=100,fetched=now()))
        db.add(Status(series='cpi',state='error',message='failure',attempted=now(),success=None))
    r=TestClient(app).get('/api/operations/integrity',auth=AUTH).json();assert r['checked']==3
    issues={(x['series'],x['issue']) for x in r['issues']};assert ('cpi','최근 원자료 관측일 오래됨') in issues and ('not-a-series','정의되지 않은 계열') in issues and ('sp500','observations 미래 날짜 또는 비유한 수치') in issues
    with isolated() as db:assert db.scalar(select(func.count()).select_from(Obs))==3


def test_target_retry_reuses_validation_preserves_other_indicators(monkeypatch):
    init();c=TestClient(app)
    assert c.post('/api/operations/retry/cpi',auth=AUTH,headers=HEAD).status_code==409
    assert c.post('/api/operations/retry/unknown',auth=AUTH,headers=HEAD).status_code==404
    monkeypatch.setattr(coll,'DEMO',False);monkeypatch.setattr(coll,'load_settings',lambda:{'HISTORY_START':'1900-01-01','FRED_API_KEY':''})
    key=('cpi',date(1901,1,1));seen=[]
    monkeypatch.setattr(coll.p,'fred',lambda client,s,start,end,cfg:(seen.append(s.id) or {key[1]:123}))
    with Session() as db:runs=db.scalar(select(func.count()).select_from(Run))
    try:
        assert coll.LOCK.acquire(False);r=coll.retry_indicator('cpi');assert r['ok']==1 and seen==['cpi'] and not coll.LOCK.locked()
        with Session() as db:assert db.get(Obs,key).value==123 and db.scalar(select(func.count()).select_from(Run))==runs
    finally:
        with Session.begin() as db:db.execute(delete(Obs).where(Obs.series==key[0],Obs.period==key[1]));db.execute(delete(Revision).where(Revision.series==key[0],Revision.period==key[1]))


def test_journal_import_preview_conflict_wrong_mode_and_replay(isolated,monkeypatch):
    monkeypatch.setattr(journal,'Session',isolated)
    c=TestClient(app);archive={'format':'macro-invest-journal-v1','demo':True,'revision':77,'book':{'entries':[{'id':'x','day':'2024-01-01','kind':'deposit','amount':'1000'}],'marks':[],'valuation_days':[]}}
    req={'archive':archive,'expected_revision':0};preview='/api/journal/import/preview';url='/api/journal/import'
    assert c.post(preview,auth=AUTH,json=req).status_code==403
    assert c.post(preview,auth=AUTH,headers=HEAD,json={**req,'archive':{**archive,'demo':False}}).status_code==400
    assert c.put(url,auth=AUTH,headers=HEAD,json=req).status_code==400
    r=c.post(preview,auth=AUTH,headers=HEAD,json=req);assert r.status_code==200,r.text;assert r.json()['cash']=='1000.00'
    assert c.get('/api/journal',auth=AUTH).json()['revision']==0
    r=c.put(url,auth=AUTH,headers=HEAD,json={**req,'confirmed':True});assert r.status_code==200,r.text;assert r.json()['revision']==1 and r.json()['current']['cash']=='1000.00'
    assert c.put(url,auth=AUTH,headers=HEAD,json={**req,'confirmed':True}).status_code==409
    bad={**archive,'book':{**archive['book'],'entries':[{'id':'w','day':'2024-01-01','kind':'withdraw','amount':'1000'}]}}
    assert c.post(preview,auth=AUTH,headers=HEAD,json={'archive':bad,'expected_revision':1}).status_code==400
    assert c.get('/api/journal',auth=AUTH).json()['revision']==1


def test_sqlite_wal_snapshot_excludes_uncommitted_writes(isolated):
    path=op.db_path()
    with closing(sqlite3.connect(path)) as writer:
        assert writer.execute('PRAGMA journal_mode=WAL').fetchone()[0]=='wal'
        writer.execute('CREATE TABLE IF NOT EXISTS smoke (val INTEGER)');writer.commit()
        writer.execute('INSERT INTO smoke VALUES (1)');writer.commit()
        writer.execute('INSERT INTO smoke VALUES (2)')
        m=op.create_backup();_,p=op.verify(m['id'])
        with closing(sqlite3.connect(p)) as db:
            assert db.execute('SELECT val FROM smoke').fetchall()==[(1,)]
        writer.rollback()
