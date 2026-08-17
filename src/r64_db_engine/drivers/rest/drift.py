"""Structured repair events — the recipe lane's half of drift detection.

When a response fails its `response_schema`, the provider has changed. That is
a build-time problem with a build-time answer (re-research, re-author,
re-admit), so this module records the event and alerts; it never tries to cope.

One JSON line per event, appended to
`factory/evidence/drift/<source>-<YYYYMMDD>.jsonl`, plus an ntfy alert through
the fleet's existing `ntfy-fail@` conventions.

Appended rather than overwritten because the accumulation is the signal: a
recipe failing validation once an hour between weekly sweeps is a different
situation from one that failed once, and an overwriting log cannot tell them
apart. `factory-conformance-sweep` surfaces the accumulated events.

Both side effects are best-effort and never mask the original failure. An ntfy
binary that is missing, or a read-only evidence directory, must not convert a
loud validation failure into a confusing secondary error — the caller raises
regardless.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DRIFT_DIR_ENV = "R64_FACTORY_DRIFT_DIR"
DEFAULT_DRIFT_DIR = Path(__file__).resolve().parents[4] / "factory" / "evidence" / "drift"
NTFY_BINARY = "/usr/bin/ntfy"


@dataclass(frozen=True)
class DriftEvent:
    source: str
    recipe: str
    url: str
    page: int
    reason: str
    detail: str
    json_path: list[Any] = field(default_factory=list)
    schema_path: list[Any] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        doc = asdict(self)
        doc["json_path"] = [str(p) for p in self.json_path]
        doc["schema_path"] = [str(p) for p in self.schema_path]
        doc["observed_utc"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return doc


def drift_dir() -> Path:
    """Where events land. Overridable by env so tests never write to the repo."""
    override = os.environ.get(DRIFT_DIR_ENV)
    return Path(override) if override else DEFAULT_DRIFT_DIR


def emit_drift(event: DriftEvent, scrubber: Any | None = None) -> Path | None:
    """Record the event and alert. Best-effort; never raises.

    The scrubber runs over the FINAL SERIALIZED JSON, not merely over the
    fields — belt and braces. Field-level scrubbing depends on every current and
    future field being remembered; scrubbing the serialized line is a property
    of the bytes that actually reach disk, which is the thing that matters.

    Drift events are AGENT-READ: the next agent opens this record in order to
    repair the connector, so a credential landing here is a credential in model
    context. Law 3 is enforced at this sink, not assumed upstream of it.
    """
    path = None
    scrub = scrubber.scrub if scrubber is not None else (lambda text: text)
    try:
        directory = drift_dir()
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d")
        path = directory / f"{event.source}-{stamp}.jsonl"
        line = scrub(json.dumps(event.as_dict(), sort_keys=True))
        with open(path, "a") as handle:
            handle.write(line + "\n")
        log.error("rest_drift source=%s recipe=%s reason=%s", event.source, event.recipe, event.reason)
    except OSError as exc:
        log.error("rest_drift: could not write repair event: %s", exc)

    _notify(event, scrub)
    return path


def _notify(event: DriftEvent, scrub: Any = None) -> None:
    """ntfy alert, matching the fleet's `ntfy-fail@` message shape.

    Scrubbed too: an alert body travels further than a log line, and a
    credential in a push notification is a credential on somebody's phone.
    """
    scrub = scrub or (lambda text: text)
    if not Path(NTFY_BINARY).exists():
        log.warning("rest_drift: %s not present, skipping ntfy alert", NTFY_BINARY)
        return
    title = scrub(f"r64 recipe drift: {event.source}/{event.recipe}")
    body = scrub(
        f"{event.reason} — {event.detail[:400]}. Re-research and re-admit; do not retry."
    )
    try:
        subprocess.run(
            [NTFY_BINARY, "publish", "--title", title, "--priority", "high",
             "--tags", "rotating_light", "kosmesh-9f46768cb4bd14b3", body],
            capture_output=True,
            timeout=20,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 - alerting must never mask the failure
        log.warning("rest_drift: ntfy publish failed: %s", exc)


def read_events(source: str, directory: Path | None = None) -> list[dict[str, Any]]:
    """Every recorded event for a source, oldest first. Used by the sweep."""
    directory = directory or drift_dir()
    if not directory.exists():
        return []
    events: list[dict[str, Any]] = []
    for path in sorted(directory.glob(f"{source}-*.jsonl")):
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                log.warning("rest_drift: unparseable line in %s", path)
    return events


def all_events(directory: Path | None = None) -> list[dict[str, Any]]:
    directory = directory or drift_dir()
    if not directory.exists():
        return []
    sources = {p.name.rsplit("-", 1)[0] for p in directory.glob("*.jsonl")}
    return [e for source in sorted(sources) for e in read_events(source, directory)]


__all__ = [
    "DRIFT_DIR_ENV",
    "DriftEvent",
    "all_events",
    "drift_dir",
    "emit_drift",
    "read_events",
]
