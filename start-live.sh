#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
[ -f .env ] || cp .env.example .env
export HOSTED_MODE=0 DEMO_MODE=0
printf '%s\n' 'Dashboard: http://127.0.0.1:8000' 'Admin: http://127.0.0.1:8000/admin' 'User: admin. Password: data/settings/live/admin-password.txt (created at first startup, unless ADMIN_PASSWORD is set).'
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
