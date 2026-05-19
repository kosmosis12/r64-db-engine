"""Optional Prometheus metrics exposition. SPEC §8.3."""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def start_metrics_server(port: int) -> bool:
    """Start the Prometheus HTTP server on the given port.

    Returns True on success, False if prometheus_client isn't installed
    or the port is 0.
    """
    if port <= 0:
        return False
    try:
        from prometheus_client import start_http_server  # type: ignore[import-not-found]
    except ImportError:
        log.warning("prometheus_client not installed; metrics disabled")
        return False
    start_http_server(port)
    log.info("prometheus metrics exposed on :%d", port)
    return True


def register_collectors(snapshot_fn: Any) -> None:
    """Register custom collectors backed by the daemon's status snapshot.

    Best-effort: silently no-ops if prometheus_client is missing.
    """
    try:
        from prometheus_client import REGISTRY  # type: ignore[import-not-found]
        from prometheus_client.core import (  # type: ignore[import-not-found]
            GaugeMetricFamily,
        )
    except ImportError:
        return

    class _DaemonCollector:
        def collect(self):  # type: ignore[no-untyped-def]
            snap = snapshot_fn()
            up = GaugeMetricFamily(
                "r64_db_engine_postgres_up",
                "Postgres reachability",
                value=int(snap["postgres"]["connected"]),
            )
            uptime = GaugeMetricFamily(
                "r64_db_engine_uptime_seconds",
                "Daemon uptime in seconds",
                value=snap["uptime_seconds"],
            )
            rows = GaugeMetricFamily(
                "r64_db_engine_rows_pulled_total",
                "Cumulative rows pulled per target",
                labels=["target"],
            )
            for t in snap["tables"]:
                rows.add_metric([t["target"]], t["rows_pulled_total"])
            yield up
            yield uptime
            yield rows

    REGISTRY.register(_DaemonCollector())


__all__ = ["start_metrics_server", "register_collectors"]
