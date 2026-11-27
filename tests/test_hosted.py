"""Fresh interpreter is required because hosting settings load on import."""
import os
import subprocess
import sys

def test_hosted_private_access(tmp_path):
    env = {**os.environ, 'DEMO_MODE':'0','HOSTED_MODE':'1',
           'PUBLIC_ORIGIN':'https://example.onrender.com',
           'ADMIN_PASSWORD':'test-hosted-secret-password-123456',
           'SETTINGS_DIR':str(tmp_path/'settings'), 'AUTO_COLLECT':'0',
           'DATABASE_URL':'sqlite:///'+str(tmp_path/'hosted.db')}
    code = '''
from fastapi.testclient import TestClient
from app.main import app
import os
base='https://example.onrender.com'
auth=('admin',os.environ['ADMIN_PASSWORD'])
with TestClient(app,base_url=base) as c:
    for path in ('/', '/admin','/api/dashboard','/static/app.js','/notifications','/api/notifications','/manifest.webmanifest','/sw.js','/docs','/openapi.json'):
        assert c.get(path).status_code==401, path
    assert c.get('/healthz').status_code==200
    assert c.get('/',auth=auth).status_code==200
    assert c.get('/api/dashboard',auth=auth).status_code==200
    assert c.get('/admin',auth=auth).status_code==200
    assert c.get('/static/app.js',auth=auth).headers['Cache-Control']=='no-store'
    assert c.get('/',auth=auth,headers={'Host':'evil.example'}).status_code==400
    h={'X-Macro-Admin':'1','Origin':base}
    assert c.put('/api/admin/settings',auth=auth,headers=h,json={'values':{'FRED_API_KEY':'test-key'}}).status_code==200
    assert c.put('/api/admin/settings',auth=auth,headers={**h,'Origin':'https://evil.example'},json={'values':{}}).status_code==403
    assert c.get('/api/admin/settings',auth=auth).json()['configured']['FRED_API_KEY']
    assert 'test-key' not in c.get('/api/admin/settings',auth=auth).text
'''
    subprocess.run([sys.executable,'-c',code],env=env,check=True,timeout=40)
