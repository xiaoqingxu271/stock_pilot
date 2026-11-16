"""One-command demo launcher.

``python -m stock_pilot.demo`` starts the API and prints the console URL plus
the three demo SKUs. ``--split`` also starts the inventory and mall HTTP MCP
servers and points the orchestrator at them. The database is seeded on first
use; ``--fresh`` deletes only the configured demo DB before starting.
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import uvicorn

from stock_pilot.config import DEFAULT_DB_PATH, PROJECT_ROOT

_CHECKS = (
    ("正常调价", "SKU-TSHIRT-001", "白色-M", "59.00", "审批通过 → 已上架"),
    ("库存不足", "SKU-HOODIE-003", "黑色-L", "99.00", "零写操作，直接终止"),
    ("映射歧义", "SKU-AMBIG-002", "白色-M", "69.00", "挂起，人工选候选"),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stock-pilot-demo",
        description="Start the StockPilot approval console with seeded demo data.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("STOCK_PILOT_HOST", "127.0.0.1"),
        help="API bind host (default 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("STOCK_PILOT_PORT", "8000")),
        help="API port (default 8000)",
    )
    parser.add_argument(
        "--db",
        default=os.environ.get("STOCK_PILOT_DB", str(DEFAULT_DB_PATH)),
        help="SQLite path (default data/stock_pilot.db)",
    )
    parser.add_argument(
        "--split",
        action="store_true",
        help="start inventory + mall HTTP MCP servers and route tools to them",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="delete the configured demo database first",
    )
    return parser


def port_open(host: str, port: int, timeout: float = 0.25) -> bool:
    probe_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    with socket.socket() as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((probe_host, port)) == 0


def wait_for_port(host: str, port: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if port_open(host, port):
            return
        time.sleep(0.1)
    raise SystemExit(f"MCP server on {host}:{port} did not start within {timeout:.0f}s")


def _child_env(db_path: str) -> dict[str, str]:
    env = dict(os.environ)
    env["STOCK_PILOT_DB"] = db_path
    env.setdefault("PYTHONUTF8", "1")
    src = str(PROJECT_ROOT / "src")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = src if not existing else f"{src}{os.pathsep}{existing}"
    return env


def _clear_demo_db(db_path: str) -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(f"{db_path}{suffix}").unlink(missing_ok=True)


def _start_split_servers(
    db_path: str, host: str, inventory_port: int, mall_port: int
) -> list[subprocess.Popen]:
    for port, name in ((inventory_port, "inventory"), (mall_port, "mall")):
        if port_open(host, port):
            raise SystemExit(
                f"port {port} ({name} MCP server) is already in use; "
                "stop the other process or override STOCK_PILOT_*_PORT"
            )

    env = _child_env(db_path)
    env["STOCK_PILOT_INVENTORY_PORT"] = str(inventory_port)
    env["STOCK_PILOT_MALL_PORT"] = str(mall_port)
    processes = [
        subprocess.Popen(
            [sys.executable, "-m", "stock_pilot.mcp_inventory"], env=env
        ),
        subprocess.Popen([sys.executable, "-m", "stock_pilot.mcp_mall"], env=env),
    ]
    try:
        wait_for_port(host, inventory_port)
        wait_for_port(host, mall_port)
    except BaseException:
        _stop(processes)
        raise
    return processes


def _stop(processes: list[subprocess.Popen]) -> None:
    for process in processes:
        if process.poll() is None:
            process.terminate()
    for process in processes:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def _banner(
    host: str, port: int, db_path: str, split: bool, fresh: bool
) -> None:
    shown_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    print()
    print("StockPilot 审批台：" f"http://{shown_host}:{port}/")
    print(f"数据库：{db_path}" + ("（已重建）" if fresh else ""))
    print(f"工具拓扑：{'库存 / 商城两个 HTTP MCP Server' if split else '进程内'}")
    print()
    print("演示用例：")
    for name, sku, spec, price, outcome in _CHECKS:
        print(f"  {name:<6} {sku:<16} {spec:<6} ¥{price:<6} → {outcome}")
    print()
    print("按 Ctrl+C 退出。")
    print()


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    db_path = str(Path(args.db).expanduser().resolve())
    host = args.host
    port = args.port

    if port_open(host, port):
        raise SystemExit(
            f"port {port} is already in use; stop the other process or pass --port"
        )

    os.environ["STOCK_PILOT_DB"] = db_path
    os.environ["STOCK_PILOT_HOST"] = host
    os.environ["STOCK_PILOT_PORT"] = str(port)
    if args.fresh:
        _clear_demo_db(db_path)

    inventory_port = int(os.environ.get("STOCK_PILOT_INVENTORY_PORT", "8101"))
    mall_port = int(os.environ.get("STOCK_PILOT_MALL_PORT", "8102"))
    processes: list[subprocess.Popen] = []
    try:
        if args.split:
            processes = _start_split_servers(
                db_path, host, inventory_port, mall_port
            )
            os.environ["STOCK_PILOT_TOOL_CLIENT"] = "mcp-http"
            os.environ["STOCK_PILOT_INVENTORY_URL"] = (
                f"http://127.0.0.1:{inventory_port}/mcp"
            )
            os.environ["STOCK_PILOT_MALL_URL"] = (
                f"http://127.0.0.1:{mall_port}/mcp"
            )
        else:
            os.environ["STOCK_PILOT_TOOL_CLIENT"] = "inprocess"

        _banner(host, port, db_path, args.split, args.fresh)
        uvicorn.run(
            "stock_pilot.api:create_app",
            factory=True,
            host=host,
            port=port,
            log_level="info",
        )
    except KeyboardInterrupt:
        pass
    finally:
        _stop(processes)


if __name__ == "__main__":
    main()
