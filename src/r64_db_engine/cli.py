"""r64-db-engine CLI. SPEC §10."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from r64_db_engine import __version__
from r64_db_engine.core import logging as r64log
from r64_db_engine.core.config import Config, load_config
from r64_db_engine.core.daemon import Daemon, build_daemon
from r64_db_engine.core.health import HealthServer
from r64_db_engine.core.metrics import register_collectors, start_metrics_server
from r64_db_engine.core.systemd import install_unit, render_unit

DEFAULT_CONFIG = "/etc/r64-db-engine/config.yaml"


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.cmd is None:
        parser.print_help()
        return 2
    return _dispatch(args)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="r64-db-engine")
    sub = p.add_subparsers(dest="cmd")

    pr = sub.add_parser("run", help="start the daemon")
    pr.add_argument("--config", default=DEFAULT_CONFIG)
    pr.add_argument("--once", action="store_true", help="run each table once and exit")

    pv = sub.add_parser("validate", help="parse + validate config and connect")
    pv.add_argument("--config", default=DEFAULT_CONFIG)

    pd = sub.add_parser("discover", help="list source tables and incremental candidates")
    pd.add_argument("--config", default=DEFAULT_CONFIG)
    pd.add_argument("--schema", default=None)

    ps = sub.add_parser("status", help="query a running daemon's /health")
    ps.add_argument("--health-url", default="http://localhost:8765/health")

    sub.add_parser("version", help="print version and exit")

    pi = sub.add_parser("install-systemd", help="generate a systemd unit")
    pi.add_argument("--user", default="row64")
    pi.add_argument("--group", default=None)
    pi.add_argument("--config", default=DEFAULT_CONFIG)
    pi.add_argument(
        "--dry-run",
        action="store_true",
        help="print the unit instead of writing it",
    )

    return p


def _dispatch(args: argparse.Namespace) -> int:
    if args.cmd == "version":
        print(__version__)
        return 0
    if args.cmd == "status":
        return _cmd_status(args)
    if args.cmd == "install-systemd":
        return _cmd_install_systemd(args)
    if args.cmd == "validate":
        return asyncio.run(_cmd_validate(args.config))
    if args.cmd == "discover":
        return asyncio.run(_cmd_discover(args.config, args.schema))
    if args.cmd == "run":
        return asyncio.run(_cmd_run(args.config, args.once))
    raise AssertionError(f"unknown command: {args.cmd}")


# ---- commands -------------------------------------------------------


async def _cmd_validate(config_path: str) -> int:
    cfg = _load(config_path)
    r64log.configure(cfg.telemetry.log_level, cfg.telemetry.log_format)
    daemon = build_daemon(cfg)
    try:
        await daemon.driver.connect(cfg.postgres.model_dump())
        any_err = False
        for table in cfg.tables:
            resolved = cfg.resolve_table(table)
            result = await daemon.driver.validate_table(resolved)
            status = "ok" if result.ok else "ERROR"
            print(f"[{status}] {table.target} ({table.source})")
            for err in result.errors:
                print(f"   error: {err}")
                any_err = True
            for warn in result.warnings:
                print(f"   warn:  {warn}")
        return 1 if any_err else 0
    finally:
        await daemon.driver.close()


async def _cmd_discover(config_path: str, schema: str | None) -> int:
    cfg = _load(config_path)
    r64log.configure(cfg.telemetry.log_level, cfg.telemetry.log_format)
    daemon = build_daemon(cfg)
    try:
        await daemon.driver.connect(cfg.postgres.model_dump())
        tables = await daemon.driver.discover(schema_filter=schema)
        for t in tables:
            rows = "?" if t.estimated_rows is None else str(t.estimated_rows)
            print(f"{t.schema}.{t.name}  (≈{rows} rows)")
            if t.candidate_incremental_keys:
                print(f"   incremental candidates: {', '.join(t.candidate_incremental_keys)}")
            for c in t.columns:
                null = "" if c.nullable else " NOT NULL"
                print(f"     - {c.name}: {c.source_type}{null} -> {c.pandas_dtype}")
        return 0
    finally:
        await daemon.driver.close()


async def _cmd_run(config_path: str, once: bool) -> int:
    cfg = _load(config_path)
    r64log.configure(cfg.telemetry.log_level, cfg.telemetry.log_format)
    daemon: Daemon = build_daemon(cfg)

    health_task: asyncio.Task | None = None
    if cfg.telemetry.health_port:
        server = HealthServer(daemon.status_snapshot, cfg.telemetry.health_port)
        await server.start()
        health_task = asyncio.create_task(server.serve_forever())

    if cfg.telemetry.metrics_port and start_metrics_server(cfg.telemetry.metrics_port):
        register_collectors(daemon.status_snapshot)

    loop = asyncio.get_running_loop()
    daemon.install_signal_handlers(loop)
    try:
        await daemon.run(once=once)
    finally:
        if health_task is not None:
            health_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await health_task
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    try:
        with urllib.request.urlopen(args.health_url, timeout=3) as resp:
            body = json.loads(resp.read())
    except urllib.error.URLError as exc:
        print(f"daemon unreachable at {args.health_url}: {exc}", file=sys.stderr)
        return 2
    _print_status(body)
    return 0 if body.get("status") in ("ok", "degraded") else 1


def _cmd_install_systemd(args: argparse.Namespace) -> int:
    group = args.group or args.user
    if args.dry_run:
        print(render_unit(args.user, group, args.config))
        return 0
    path = install_unit(args.user, group, args.config)
    print(f"installed unit at {path}")
    print("next steps:")
    print("   sudo systemctl daemon-reload")
    print("   sudo systemctl enable --now r64-db-engine")
    return 0


def _print_status(body: dict[str, Any]) -> None:
    print(f"status:   {body.get('status')}")
    print(f"uptime:   {body.get('uptime_seconds')}s")
    print(f"version:  {body.get('version')}")
    pg = body.get("postgres", {})
    print(f"postgres: {pg.get('host')}/{pg.get('database')} connected={pg.get('connected')}")
    print("tables:")
    for t in body.get("tables", []):
        line = f"   - {t['target']:<28} status={t['status']:<8} mode={t['mode']:<13}"
        if t.get("last_success_at"):
            line += f" last_ok={t['last_success_at']}"
        print(line)
        if t.get("last_error"):
            print(f"       error: {t['last_error']}")


def _load(path: str) -> Config:
    if not Path(path).exists():
        print(f"config not found: {path}", file=sys.stderr)
        sys.exit(2)
    try:
        return load_config(path)
    except Exception as exc:
        print(f"config error: {exc}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    sys.exit(main())
