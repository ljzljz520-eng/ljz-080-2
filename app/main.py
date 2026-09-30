"""Command-line entry point for the island homestay energy host system."""
from __future__ import annotations

import argparse
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

from .api import AppState, create_db, create_handler
from .demo import reset_and_seed, seed_demo
from .storage import Database

DEFAULT_DB = Path("data/island_energy.db")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="海岛民宿能耗托管系统")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据库路径")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("init-db", help="初始化数据库表")
    sub.add_parser("seed", help="写入演示数据（保留已有数据）")
    sub.add_parser("reset-demo", help="删除数据库并重新生成演示数据")
    serve = sub.add_parser("serve", help="启动 Web/API 服务")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "serve"

    if command == "init-db":
        db = Database(args.db)
        db.init_db()
        print(f"数据库已初始化: {args.db}")
        db.close()
        return
    if command == "reset-demo":
        ids = reset_and_seed(args.db)
        print(f"演示数据已重置: {args.db}，房源 {len(ids['houses'])} 栋，已生成示例账单 {ids['completed_bill']}")
        return

    auto_seed = not Path(args.db).exists()
    db = create_db(args.db, auto_seed=auto_seed)
    if command == "seed":
        if not db.query_one("SELECT id FROM houses LIMIT 1"):
            ids = seed_demo(db)
            print(f"演示数据已写入，示例账单ID: {ids['completed_bill']}")
        else:
            print("数据库已有数据，跳过 seed；如需全新演示请执行 reset-demo")
        db.close()
        return

    host = getattr(args, "host", "0.0.0.0")
    port = getattr(args, "port", 8000)
    handler = create_handler(AppState(db))
    server = ThreadingHTTPServer((host, port), handler)
    print(f"海岛民宿能耗托管系统已启动: http://{host}:{port}")
    print("API 健康检查: /health ；看板: /")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
    finally:
        server.server_close()
        db.close()


if __name__ == "__main__":
    main()
