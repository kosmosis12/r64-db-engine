"""Evidence packs — the review artifact of record (Law 2).

> Trust the artifacts, not the code. Review operates on the pack plus
> cross-agent QA, not on the diff.

Two files per run, same stem:

- `EVIDENCE-<dialect>-<YYYYMMDD>.json` — machine form. Every check, both sides
  of every comparison, every source query issued, the artifact sha256 and row
  counts, and the environment the run happened in.
- `EVIDENCE-<dialect>-<YYYYMMDD>.md` — human form. Verdict line at the top,
  one table per check.

The bar for the `.md` is specific and worth stating plainly: **a reviewer must
be able to ratify the driver from that file without reading the diff.** If a
reader has to open the source to find out what a check actually compared, the
pack has failed at its job and the fix belongs here, not in the reviewer.

Both sides of every comparison are recorded, passing ones included. A pack that
listed only failures would let a reviewer confirm nothing — "no failures" and
"nothing was checked" would render identically.
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from factory.battery import FAIL, PASS, SKIPPED, CheckResult

# Packages whose versions materially change the artifact. pyarrow above all:
# it is exact-pinned in pyproject because it owns the block layout the
# consumer's cache is keyed on, and pandas because its string dtype is what
# decides `string` vs `large_string` (the B-3 fence).
TRACKED_PACKAGES = (
    "pyarrow",
    "pandas",
    "pydantic",
    "clickhouse_connect",
    "row64tools",
    "jsonschema",
    "httpx",
)


@dataclass
class EvidencePack:
    dialect: str
    table: str
    source: str
    checks: list[CheckResult]
    artifact: dict[str, Any]
    environment: dict[str, Any]
    invocation: dict[str, Any]
    generated_utc: str

    @property
    def verdict(self) -> str:
        if any(c.status == FAIL for c in self.checks):
            return FAIL
        return PASS

    @property
    def tally(self) -> dict[str, int]:
        return {
            PASS: sum(1 for c in self.checks if c.status == PASS),
            FAIL: sum(1 for c in self.checks if c.status == FAIL),
            SKIPPED: sum(1 for c in self.checks if c.status == SKIPPED),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "tally": self.tally,
            "dialect": self.dialect,
            "table": self.table,
            "source": self.source,
            "generated_utc": self.generated_utc,
            "invocation": self.invocation,
            "artifact": self.artifact,
            "environment": self.environment,
            "checks": [c.as_dict() for c in self.checks],
        }


def collect_environment(container: str | None = None) -> dict[str, Any]:
    """Everything about this machine that could change the artifact."""
    env: dict[str, Any] = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "packages": {},
        "git": _git_facts(),
    }
    for name in TRACKED_PACKAGES:
        env["packages"][name] = _package_version(name)
    if container:
        env["container"] = _container_facts(container)
    return env


def _package_version(name: str) -> str:
    try:
        module = __import__(name)
    except ImportError:
        return "<not installed>"
    return str(getattr(module, "__version__", "<no __version__>"))


def _git_facts() -> dict[str, Any]:
    def run(*args: str) -> str:
        try:
            return subprocess.run(
                ["git", *args], capture_output=True, text=True, timeout=15, check=False
            ).stdout.strip()
        except Exception:  # noqa: BLE001 - a pack from a non-git tree is still a pack
            return ""

    return {
        "commit": run("rev-parse", "HEAD"),
        "branch": run("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(run("status", "--porcelain")),
    }


def _container_facts(name: str) -> dict[str, Any]:
    """Image tag and digest of the source container, if docker can be reached."""
    try:
        out = subprocess.run(
            ["docker", "inspect", name, "--format", "{{.Config.Image}}\t{{.Image}}\t{{.State.Status}}"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        return {"name": name, "error": str(exc)}
    if out.returncode != 0:
        return {"name": name, "error": out.stderr.strip()}
    parts = out.stdout.strip().split("\t")
    if len(parts) != 3:
        return {"name": name, "error": f"unexpected docker output: {out.stdout!r}"}
    return {"name": name, "image": parts[0], "image_id": parts[1], "status": parts[2]}


def build_pack(
    *,
    dialect: str,
    table: str,
    source: str,
    checks: list[CheckResult],
    artifact: dict[str, Any],
    invocation: dict[str, Any],
    container: str | None = None,
) -> EvidencePack:
    return EvidencePack(
        dialect=dialect,
        table=table,
        source=source,
        checks=checks,
        artifact=artifact,
        environment=collect_environment(container),
        invocation=invocation,
        generated_utc=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def write_pack(pack: EvidencePack, evidence_dir: Path, *, date: str | None = None) -> tuple[Path, Path]:
    """Write both forms. Returns (json_path, md_path)."""
    evidence_dir.mkdir(parents=True, exist_ok=True)
    # LOCAL date in the filename, UTC instant inside the pack (`generated_utc`).
    # The filename matches how every other dated artifact in this repo is named
    # and how an operator refers to "today's run"; a UTC stamp would file an
    # evening run under tomorrow. The unambiguous instant is never lost — it is
    # one field down, in both forms of the pack.
    stamp = date or datetime.now().strftime("%Y%m%d")
    stem = f"EVIDENCE-{pack.dialect}-{stamp}"
    json_path = evidence_dir / f"{stem}.json"
    md_path = evidence_dir / f"{stem}.md"
    json_path.write_text(json.dumps(pack.as_dict(), indent=2, sort_keys=False) + "\n")
    md_path.write_text(render_markdown(pack))
    return json_path, md_path


_STATUS_MARK = {PASS: "PASS", FAIL: "**FAIL**", SKIPPED: "SKIPPED"}


def render_markdown(pack: EvidencePack) -> str:
    t = pack.tally
    lines: list[str] = []
    lines.append(f"# EVIDENCE — {pack.dialect} / {pack.table}")
    lines.append("")
    lines.append(
        f"**VERDICT: {pack.verdict}** — {t[PASS]} passed, {t[FAIL]} failed, "
        f"{t[SKIPPED]} skipped. Generated {pack.generated_utc}."
    )
    lines.append("")
    lines.append(
        "> This pack is the review artifact (Law 2). Every comparison below records BOTH "
        "sides, passing ones included, so that a reviewer can ratify the driver from this "
        "file without reading the diff."
    )
    lines.append("")

    lines.append("## Run")
    lines.append("")
    lines.append("| field | value |")
    lines.append("|---|---|")
    lines.append(f"| dialect | `{pack.dialect}` |")
    lines.append(f"| table | `{pack.table}` |")
    lines.append(f"| source | `{pack.source}` |")
    for key, value in pack.invocation.items():
        lines.append(f"| {key} | `{value}` |")
    for key, value in pack.artifact.items():
        lines.append(f"| artifact.{key} | `{value}` |")
    lines.append("")

    lines.append("## Summary")
    lines.append("")
    lines.append("| # | check | verdict | detail |")
    lines.append("|---|---|---|---|")
    for i, check in enumerate(pack.checks, 1):
        lines.append(
            f"| {i} | `{check.name}` | {_STATUS_MARK.get(check.status, check.status)} | "
            f"{_cell(check.detail)} |"
        )
    lines.append("")

    for i, check in enumerate(pack.checks, 1):
        lines.append(f"## {i}. `{check.name}` — {_STATUS_MARK.get(check.status, check.status)}")
        lines.append("")
        if check.detail:
            lines.append(check.detail)
            lines.append("")
        if check.comparisons:
            lines.append("| comparison | actual | expected | ok | note |")
            lines.append("|---|---|---|:--:|---|")
            for c in check.comparisons:
                lines.append(
                    f"| {_cell(c.label)} | `{_cell(c.actual)}` | `{_cell(c.expected)}` | "
                    f"{'ok' if c.ok else '**MISMATCH**'} | {_cell(c.note)} |"
                )
            lines.append("")
        if check.queries:
            lines.append("Source queries issued:")
            lines.append("")
            lines.append("```sql")
            lines.extend(check.queries)
            lines.append("```")
            lines.append("")
        if check.observations:
            lines.append("<details><summary>observations</summary>")
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(check.observations, indent=2))
            lines.append("```")
            lines.append("")
            lines.append("</details>")
            lines.append("")

    lines.append("## Environment")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(pack.environment, indent=2))
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def _cell(value: Any) -> str:
    """Render a value for a markdown table cell without breaking the table."""
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = text.replace("|", "\\|").replace("\n", " ")
    return text if len(text) <= 300 else text[:297] + "..."


__all__ = [
    "TRACKED_PACKAGES",
    "EvidencePack",
    "build_pack",
    "collect_environment",
    "render_markdown",
    "write_pack",
]
