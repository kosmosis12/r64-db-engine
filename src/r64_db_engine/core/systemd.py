"""systemd unit installer. SPEC §10."""

from __future__ import annotations

import shutil
from pathlib import Path

UNIT_PATH = Path("/etc/systemd/system/r64-db-engine.service")

_TEMPLATE = """[Unit]
Description=r64-db-engine — Postgres -> Row64 ramdb daemon
After=network.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
User={user}
Group={group}
ExecStart={exe} run --config {config}
Restart=on-failure
RestartSec=5s
StandardOutput=journal
StandardError=journal
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
"""


def render_unit(user: str, group: str, config_path: str, exe: str | None = None) -> str:
    exe_resolved = exe or shutil.which("r64-db-engine") or "r64-db-engine"
    return _TEMPLATE.format(user=user, group=group, exe=exe_resolved, config=config_path)


def install_unit(
    user: str,
    group: str,
    config_path: str,
    target: Path = UNIT_PATH,
    exe: str | None = None,
) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_unit(user, group, config_path, exe))
    return target


__all__ = ["render_unit", "install_unit", "UNIT_PATH"]
