#!/usr/bin/env sh
set -eu

cd /app
exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8099
