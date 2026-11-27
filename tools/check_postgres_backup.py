"""Run only against the dedicated disposable CI PostgreSQL service."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from datetime import date
url=os.environ.get('DATABASE_URL','');parsed=urlsplit(url.replace('postgresql+psycopg://','postgresql://'))
if os.environ.get('OPS_PG_TEST')!='1' or parsed.hostname!='127.0.0.1' or parsed.path!='/macroinvest_ops_test':
    raise SystemExit('Disposable local macroinvest_ops_test database required.')
with TemporaryDirectory(prefix='macro-ops-pg-') as tmp:
    os.environ.update(DEMO_MODE='0',HOSTED_MODE='0',SETTINGS_DIR=str(Path(tmp)/'settings'),BACKUP_DIR=str(Path(tmp)/'backups'),ADMIN_PASSWORD='ci-disposable-password',AUTO_COLLECT='0',ALERT_ENABLED='0')
    from app.main import init
    from app.db import Session, Obs, Revision, now, engine
    from app.operations import create_backup,verify
    from app.ops_restore import restore
    init();day=date(2024,1,1)
    with Session.begin() as db:
        db.add(Obs(series='cpi',period=day,value=100,fetched=now()))
        db.add(Revision(series='cpi',period=day,value=100,seen=now()))
    m=create_backup();assert m['backend']=='postgresql';verify(m['id'])
    with Session.begin() as db:
        db.get(Obs,('cpi',day)).value=200
        db.add(Revision(series='cpi',period=day,value=200,seen=now()))
    safety=restore(m['id'],'RESTORE');verify(safety)
    with Session.begin() as db:
        assert db.get(Obs,('cpi',day)).value==100
        assert db.get(Revision,1).value==100 and db.get(Revision,2) is None
        r=Revision(series='cpi',period=day,value=101,seen=now());db.add(r);db.flush();assert r.id>1
    engine.dispose();print('PostgreSQL backup/restore and sequence smoke passed')
