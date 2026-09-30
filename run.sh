#!/usr/bin/env bash
# 一键启动：首次运行自动建库+种子数据，然后启动 API
set -e
cd "$(dirname "$0")"
if [ ! -f data/island_energy.db ]; then
  echo ">>> 初始化数据库与演示数据 ..."
  python3 -m app.seed
fi
echo ">>> 启动 API（看板 http://127.0.0.1:8000 ，文档 /docs）"
exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000
