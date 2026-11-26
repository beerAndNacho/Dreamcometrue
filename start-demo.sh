#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
export HOSTED_MODE=0 DEMO_MODE=1 AUTO_COLLECT=0
printf '%s\n' 'Open http://127.0.0.1:8000' 'Admin: http://127.0.0.1:8000/admin' 'User: admin. Password: data/settings/demo/admin-password.txt (unless ADMIN_PASSWORD is set).'
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
