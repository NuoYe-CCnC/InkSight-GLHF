#!/bin/sh
# InkSight 后端启动（位于 shared/backend，自定位）
# 用法：./run-backend.sh            # 端口 8080
#       PORT=18080 ./run-backend.sh # 指定端口
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT" || exit 1
if [ ! -f .env ]; then
    cp .env.example .env
    echo "[提示] 已生成空白 .env。未填写付费 API Key 也可启动，新闻将使用本地每日寄语。"
fi
export PYTHONDONTWRITEBYTECODE=1
exec .venv/bin/python -m uvicorn api.index:app --host 0.0.0.0 --port "${PORT:-8080}" "$@"
