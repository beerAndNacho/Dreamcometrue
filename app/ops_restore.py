"""Offline restore of a trusted same-version server backup; never a web endpoint."""
import argparse
import sqlite3
from contextlib import closing
from . import main  # register every ORM table
from .operations import VERSION, OpsError, instance_lock, verify, create_backup, backend, db_path, sqlite_read, sqlite_check, pg_run
from .db import engine
from .settings import save_settings

def restore(id,confirmation):
    if confirmation!='RESTORE':raise OpsError('명시적 RESTORE 확인이 필요합니다.')
    with instance_lock():
        m,p=verify(id)
        if m['version']!=VERSION:raise OpsError('현재 프로그램과 동일한 버전의 백업만 복원할 수 있습니다.')
        safety=create_backup('before-restore')
        # Restoring old delivery state can replay alerts. Pause automation before changing DB.
        save_settings({'ALERT_ENABLED':'0','AUTO_COLLECT':'0','BACKUP_ENABLED':'0'})
        engine.dispose()
        if backend()=='sqlite':
            try:
                # SQLite backup API commits a consistent snapshot into the existing file.
                with closing(sqlite_read(p)) as src,closing(sqlite3.connect(db_path(),timeout=10)) as dst:src.backup(dst)
                sqlite_check(db_path())
            except (sqlite3.Error,OpsError):raise OpsError('복원 검사 실패. 서버를 시작하지 말고 복원 직전 백업 ID를 확인하세요: '+safety['id']) from None
        else:
            try:pg_run(['pg_restore','--clean','--if-exists','--no-owner','--no-privileges','--single-transaction','--exit-on-error','--dbname',engine.url.database,str(p)],db=True,timeout=300)
            except OpsError:raise OpsError('복원 실패. 복원 직전 백업 ID: '+safety['id']) from None
        return safety['id']

def cli():
    parser=argparse.ArgumentParser(description='Stop all Macro Invest writers before restoring. Trusted server backup only.')
    parser.add_argument('backup_id');parser.add_argument('--confirm',required=True)
    args=parser.parse_args()
    try:
        safety=restore(args.backup_id,args.confirm)
        print('Restore finished. Pre-restore backup:',safety)
    except OpsError as e:parser.exit(1,str(e)+'\n')
if __name__=='__main__':cli()
