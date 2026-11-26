"""Tests never touch the user's database or API settings."""
import os
import tempfile
from pathlib import Path

_testdir=tempfile.TemporaryDirectory(prefix='macro-invest-tests-')
root=Path(_testdir.name)
os.environ.update(DEMO_MODE='1',HOSTED_MODE='0',DEMO_DATABASE_URL='sqlite:///'+str(root/'demo.db'),SETTINGS_DIR=str(root/'settings'),AUTO_COLLECT='0',ADMIN_PASSWORD='test-admin-password-1234')
