#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null || ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo '请先安装 Python 3.11 或更高版本：https://www.python.org/downloads/'
  exit 1
fi
[[ -f backend/.env ]] || cp backend/.env.example backend/.env
if [[ ! -x backend/.venv/bin/python ]]; then
  python3 -m venv backend/.venv
fi
backend/.venv/bin/python -m pip install -r backend/requirements.txt
export PYTHONPATH="$PWD/backend"
echo '规划服务：http://localhost:8000/docs（真机测试请使用 Mac 局域网 IP）'
exec backend/.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
