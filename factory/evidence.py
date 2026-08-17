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

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
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
    provenance: dict[str, Any] = field(default_factory=dict)

    @property
    def allow_dirty(self) -> bool:
        return bool(self.provenance.get("allow_dirty"))

    @property
    def ratifies_head(self) -> bool:
        """True only when this pack pins a commit whose content actually ran."""
        return bool(self.provenance.get("git", {}).get("commit")) and not self.provenance.get(
            "git", {}
        ).get("dirty", True)

    @property
    def verdict(self) -> str:
        if any(c.status == FAIL for c in self.checks):
            return FAIL
        return PASS

    @property
    def verdict_line(self) -> str:
        """The verdict, stamped ALLOW-DIRTY when it ratifies no commit."""
        return f"{self.verdict} (ALLOW-DIRTY)" if self.allow_dirty else self.verdict

    @property
    def tally(self) -> dict[str, int]:
        return {
            PASS: sum(1 for c in self.checks if c.status == PASS),
            FAIL: sum(1 for c in self.checks if c.status == FAIL),
            SKIPPED: sum(1 for c in self.checks if c.status == SKIPPED),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict_line,
            "verdict_status": self.verdict,
            "ratifies_head": self.ratifies_head,
            "tally": self.tally,
            "dialect": self.dialect,
            "table": self.table,
            "source": self.source,
            "generated_utc": self.generated_utc,
            "invocation": self.invocation,
            "provenance": self.provenance,
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


# Distribution names differ from import names for some packages, and the
# version is read from installed metadata rather than from a `__version__`
# attribute: `jsonschema` deprecated its attribute, and reading metadata does
# not require importing the package at all.
_DISTRIBUTION_NAMES = {"clickhouse_connect": "clickhouse-connect"}


def _package_version(name: str) -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(_DISTRIBUTION_NAMES.get(name, name))
    except PackageNotFoundError:
        return "<not installed>"


def _git_facts(never_exempt: set[str] | None = None) -> dict[str, Any]:
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
        "dirty": bool(_dirty_paths(never_exempt)),
        "dirty_exemption": EVIDENCE_SUBTREE,
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


ARTIFACT_COPY_LIMIT_BYTES = 8 * 1024 * 1024


class DirtyTreeError(RuntimeError):
    """The working tree is dirty, so a pack could not ratify a known commit."""


# The pack's own output directory. Changes here do NOT make the tree dirty for
# the purposes of ratification: a pack cannot be invalidated by the fact that it
# is being written, and a sweep across several targets would otherwise refuse
# every target after the first — the first pack dirties the tree for the rest.
#
# This is an exemption for the PRODUCT, never for an INPUT. Code, configs, specs
# and ground truth are all outside it and all still count.
EVIDENCE_SUBTREE = "factory/evidence/"


def assert_clean_tree(
    allow_dirty: bool = False, never_exempt: set[str] | None = None
) -> dict[str, Any]:
    """A pack must ratify a HEAD that exists, or say loudly that it does not.

    An evidence pack generated from a dirty tree names a commit whose content
    is not what actually ran. Someone re-running that commit gets different
    code and possibly a different verdict, and nothing in the pack said so —
    which makes the pack an assertion about a state that never existed.

    Hard refusal by default. `--allow-dirty` exists for iterating locally, and
    it does not merely permit the run: it stamps ALLOW-DIRTY into the verdict
    line and the pack header, so a pack produced that way can never be mistaken
    for one that ratifies a commit.

    Changes under `factory/evidence/` are excluded — see `EVIDENCE_SUBTREE`.
    """
    facts = _git_facts(never_exempt=never_exempt)
    if not facts.get("commit"):
        raise DirtyTreeError(
            "no git commit could be determined, so this pack cannot ratify anything. "
            "Evidence packs are generated from a checked-out repository."
        )
    if facts["dirty"] and not allow_dirty:
        raise DirtyTreeError(
            "refusing to emit an evidence pack from a DIRTY working tree.\n"
            f"HEAD is {facts['commit'][:12]}, but the tree does not match it, so the pack "
            f"would name a commit whose content is not what ran.\n"
            "Commit the tree and re-run, or pass --allow-dirty to produce a pack stamped "
            "ALLOW-DIRTY that explicitly does NOT ratify a commit.\n"
            "Uncommitted paths:\n  "
            + "\n  ".join(_dirty_paths(never_exempt)[:40])
        )
    return facts


class LaunderedInputError(RuntimeError):
    """A pinned input resolves inside the evidence tree's dirty-file exemption."""


def assert_inputs_outside_evidence(paths: dict[str, Path | None], repo_root: Path) -> None:
    """No pinned INPUT may live where the dirty-tree exemption applies.

    The exemption exists so a pack's own output cannot invalidate it. But an
    input placed under `factory/evidence/` would inherit that exemption and
    become editable without ever making the tree dirty — a target config, a
    schema spec or a ground-truth file could be swapped between runs and every
    pack would still report `ratifies_head: true`. That is provenance
    laundering, and the exemption is what would have been laundering it.

    So the rule is two-sided: the exemption covers outputs only, AND it applies
    only to paths that are not among the resolved input set. Refused loudly,
    naming the offending path, because a config that arranges this is wrong the
    moment it is written.
    """
    guarded = (repo_root / EVIDENCE_SUBTREE.rstrip("/")).resolve()
    for name, path in paths.items():
        if path is None:
            continue
        resolved = Path(path).resolve()
        if resolved == guarded or guarded in resolved.parents:
            raise LaunderedInputError(
                f"pinned input {name!r} resolves to {resolved}, which is inside "
                f"{guarded}.\n"
                f"That directory carries the dirty-tree exemption for the pack's own OUTPUT, "
                f"so an input placed there could be edited between runs without ever making "
                f"the tree dirty — every pack would keep reporting ratifies_head: true while "
                f"the inputs moved underneath it.\n"
                f"Move the input outside {guarded} and re-run."
            )


def _dirty_paths(exempt_except: set[str] | None = None) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True,
            timeout=15, check=False,
        ).stdout
    except Exception:  # noqa: BLE001
        return []
    paths: list[str] = []
    for line in out.splitlines():
        entry = line.strip()
        if not entry:
            continue
        # Porcelain is "XY path"; the path may be quoted or a rename pair. Only
        # the evidence subtree is exempt, so a simple prefix test suffices.
        target = entry.split(maxsplit=1)[-1].strip('"')
        # The exemption applies to the evidence subtree AND ONLY to paths that
        # are not pinned inputs. A file that is both is never exempt — see
        # `assert_inputs_outside_evidence`, which refuses that arrangement
        # outright; this is the second line of the same defence.
        if target.startswith(EVIDENCE_SUBTREE) and target not in (exempt_except or set()):
            continue
        paths.append(entry)
    return paths


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def implementation_digest() -> dict[str, Any]:
    """A content address for the code that actually executed.

    The git SHA alone is not enough: it identifies a commit, not an install. A
    pack generated against an editable install of a dirty tree, or against a
    different checkout than the one on PATH, would carry a plausible SHA and
    the wrong implementation. So the engine's and the battery's source files
    are hashed directly, in sorted order, and recorded alongside the installed
    distribution version.
    """
    roots = [
        Path(__file__).resolve().parents[1] / "src" / "r64_db_engine",
        Path(__file__).resolve().parent,
    ]
    h = hashlib.sha256()
    files: list[str] = []
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(root.parent)
            files.append(str(rel))
            h.update(str(rel).encode())
            h.update(path.read_bytes())
    return {
        "distribution_version": _package_version("r64_db_engine"),
        "source_sha256": h.hexdigest(),
        "source_files": len(files),
    }


def record_artifact(artifact_path: Path, evidence_dir: Path) -> dict[str, Any]:
    """Content-address the produced artifact under `factory/evidence/artifacts/`.

    A pack that referenced `/tmp/...` pointed at something already deleted by
    the time anyone read it, which is the opposite of evidence.

    Small artifacts are COPIED so the pack is self-contained and the bytes can
    be re-verified. Large ones (the meshbench artifact is ~150 MB) are recorded
    as a MANIFEST — the content address plus its measured properties — because
    committing them would bloat the repository without adding a check that the
    sha256 does not already provide. Which of the two happened is recorded, so
    a reader never has to guess whether bytes are present.
    """
    artifacts_dir = evidence_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    digest = sha256_file(artifact_path)
    size = artifact_path.stat().st_size
    record: dict[str, Any] = {
        "sha256": digest,
        "bytes": size,
        "produced_at": str(artifact_path),
        "suffix": artifact_path.suffix,
    }

    if size <= ARTIFACT_COPY_LIMIT_BYTES:
        stored = artifacts_dir / f"{digest}{artifact_path.suffix}"
        if not stored.exists():
            shutil.copy2(artifact_path, stored)
        record["storage"] = "copied"
        record["path"] = str(stored.relative_to(evidence_dir.parent.parent))
    else:
        manifest = artifacts_dir / f"{digest}.manifest.json"
        manifest.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        record["storage"] = "content-addressed manifest"
        record["path"] = str(manifest.relative_to(evidence_dir.parent.parent))
        record["note"] = (
            f"artifact is {size} bytes, over the {ARTIFACT_COPY_LIMIT_BYTES}-byte copy "
            f"limit; the sha256 above is the content address and the bytes are not "
            f"committed"
        )
    return record


# Env vars that change WHERE a request actually goes. A pack that recorded a
# hostname while a proxy silently rerouted every call would be describing a run
# that did not happen. Values are recorded, not just presence: `HTTPS_PROXY`
# pointing somewhere unexpected is the finding, and these are routing
# configuration rather than credentials.
PROXY_ENV_VARS = (
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
)


def proxy_environment() -> dict[str, Any]:
    return {name: os.environ.get(name) for name in PROXY_ENV_VARS if name in os.environ} or {
        "_note": "no proxy-related environment variables were set"
    }


def toolchain_pins(repo_root: Path, meshroad_binary: str | None = None) -> dict[str, Any]:
    """Pins that are cheap to take and expensive to be wrong about.

    `pyproject.toml` and `uv.lock` fix the declared and resolved dependency
    sets; the meshroad binary is the consumer whose counters the serve gate
    reports, and it is a build artifact outside this repo entirely, so a
    content address is the only thing that identifies which one ran.
    """
    pins: dict[str, Any] = {
        "python": sys.version.split()[0],
        "python_implementation": platform.python_implementation(),
        "platform_triple": f"{platform.system()}-{platform.machine()}-{platform.libc_ver()[0] or 'n/a'}",
        "pyproject_toml": None,
        "uv_lock": None,
        "meshroad_binary": None,
    }
    for key, rel in (("pyproject_toml", "pyproject.toml"), ("uv_lock", "uv.lock")):
        path = repo_root / rel
        if path.exists():
            pins[key] = {"path": rel, "sha256": sha256_file(path)}
    if meshroad_binary:
        binary = Path(meshroad_binary)
        if binary.exists():
            pins["meshroad_binary"] = {
                "path": str(binary),
                "sha256": sha256_file(binary),
                "bytes": binary.stat().st_size,
            }
        else:
            pins["meshroad_binary"] = {"path": str(binary), "sha256": "<not present>"}
    return pins


def secret_references(env_files: list[str]) -> list[dict[str, Any]]:
    """Describe secret files WITHOUT hashing their contents.

    A sha256 of a low-entropy API key is offline-guessable: an evidence pack is
    a review artifact that travels, and publishing a digest of a short or
    structured credential hands an attacker an oracle they can grind against.
    The pack therefore records only what identifies the FILE — path, size,
    mtime — which is enough to say "the same secret file was in place" without
    saying anything about the secret.

    Recorded in the CLOSURE BOUNDARY section as deliberately unpinned.
    """
    out: list[dict[str, Any]] = []
    for env_file in env_files:
        path = Path(env_file).expanduser()
        record: dict[str, Any] = {"path": str(path), "contents_hashed": False}
        if path.exists():
            stat = path.stat()
            record.update({
                "bytes": stat.st_size,
                "mtime_utc": datetime.fromtimestamp(stat.st_mtime, UTC).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"),
                "mode": oct(stat.st_mode & 0o777),
            })
        else:
            record["present"] = False
        out.append(record)
    return out


# What a pack deliberately does NOT pin, and why. Emitted into every pack so a
# reader never has to infer the boundary from what happens to be absent.
CLOSURE_BOUNDARY = [
    {
        "item": "secret contents",
        "pinned": False,
        "why": (
            "a sha256 of a low-entropy API key is offline-guessable, and an evidence pack "
            "travels. Only path, size, mtime and mode are recorded — enough to say the same "
            "secret file was in place, nothing about the secret."
        ),
    },
    {
        "item": "native and runtime dependencies beyond the lockfiles",
        "pinned": False,
        "why": (
            "pyproject.toml and uv.lock fix the declared and resolved Python sets, and the "
            "meshroad binary is content-addressed. Shared libraries, the OS package set and "
            "the container's own contents are NOT pinned; the container image digest is "
            "recorded, which identifies the image but does not reconstruct it."
        ),
    },
    {
        "item": "live source state",
        "pinned": False,
        "measured": True,
        "why": (
            "a live database or API cannot be pinned by a pack — it is not ours and it "
            "moves. What the pack carries is MEASUREMENT of it at run time: row counts, "
            "aggregates, min/max bounds, session timezone, and the artifact's content "
            "address. Those values are already in this pack; they establish what the source "
            "held during this run, not that it will hold it again."
        ),
    },
    {
        "item": "the machine's wall clock and scheduling",
        "pinned": False,
        "why": (
            "no timing claim is made by any check in this battery, so clock and load are "
            "deliberately outside the boundary rather than silently assumed."
        ),
    },
]


def digest_inputs(paths: dict[str, Path | None]) -> dict[str, Any]:
    """sha256 every input the run consumed, so the pack pins what it read."""
    out: dict[str, Any] = {}
    for name, path in paths.items():
        if path is None:
            out[name] = None
        elif Path(path).exists():
            out[name] = {"path": str(path), "sha256": sha256_file(Path(path))}
        else:
            out[name] = {"path": str(path), "sha256": "<missing>"}
    return out


def build_pack(
    *,
    dialect: str,
    table: str,
    source: str,
    checks: list[CheckResult],
    artifact: dict[str, Any],
    invocation: dict[str, Any],
    container: str | None = None,
    provenance: dict[str, Any] | None = None,
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
        provenance=provenance or {},
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
        f"**VERDICT: {pack.verdict_line}** — {t[PASS]} passed, {t[FAIL]} failed, "
        f"{t[SKIPPED]} skipped. Generated {pack.generated_utc}."
    )
    lines.append("")
    if pack.allow_dirty:
        lines.append(
            "> ⚠️ **ALLOW-DIRTY — THIS PACK RATIFIES NO COMMIT.** It was generated from a "
            "working tree that did not match HEAD, so the git SHA below names a commit whose "
            "content is NOT what ran. Do not use it to admit a driver."
        )
        lines.append("")
    elif pack.ratifies_head:
        commit = pack.provenance.get("git", {}).get("commit", "")
        lines.append(
            f"> Ratifies `{commit[:12]}` from a clean tree: the code that ran is the code at "
            f"that commit, and every input below is pinned by sha256."
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
    lines.append("| # | check | verdict | reason | detail |")
    lines.append("|---|---|---|---|---|")
    for i, check in enumerate(pack.checks, 1):
        lines.append(
            f"| {i} | `{check.name}` | {_STATUS_MARK.get(check.status, check.status)} | "
            f"{('`' + check.reason_code + '`') if check.reason_code else ''} | "
            f"{_cell(check.detail)} |"
        )
    lines.append("")

    for i, check in enumerate(pack.checks, 1):
        lines.append(f"## {i}. `{check.name}` — {_STATUS_MARK.get(check.status, check.status)}")
        lines.append("")
        if check.reason_code:
            # The machine-checkable reason the check failed. Fixtures assert on
            # this exact string, so a reviewer reading the pack and a test
            # asserting the mechanism are looking at the same identifier.
            lines.append(f"**Reason code:** `{check.reason_code}`")
            lines.append("")
        if check.detail:
            lines.append(check.detail)
            lines.append("")
        if check.comparisons:
            lines.append("| comparison | actual | expected | ok | code | note |")
            lines.append("|---|---|---|:--:|---|---|")
            for c in check.comparisons:
                code = f"`{c.code}`" if (c.code and not c.ok) else ""
                lines.append(
                    f"| {_cell(c.label)} | `{_cell(c.actual)}` | `{_cell(c.expected)}` | "
                    f"{'ok' if c.ok else '**MISMATCH**'} | {code} | {_cell(c.note)} |"
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

    # MANDATORY in every pack: what is deliberately NOT pinned, and why. A
    # reader must never have to infer the boundary from what happens to be
    # absent — an omission and a decision look identical from outside.
    lines.append("## CLOSURE BOUNDARY — what this pack does NOT establish")
    lines.append("")
    lines.append("| item | pinned | why |")
    lines.append("|---|---|---|")
    for entry in CLOSURE_BOUNDARY:
        pinned = "measured, not pinned" if entry.get("measured") else "**no**"
        lines.append(f"| {_cell(entry['item'])} | {pinned} | {_cell(entry['why'])} |")
    lines.append("")

    if pack.provenance:
        lines.append("## Provenance — what this pack ratifies")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(pack.provenance, indent=2, sort_keys=True))
        lines.append("```")
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
    "CLOSURE_BOUNDARY",
    "PROXY_ENV_VARS",
    "TRACKED_PACKAGES",
    "EvidencePack",
    "LaunderedInputError",
    "assert_inputs_outside_evidence",
    "build_pack",
    "collect_environment",
    "proxy_environment",
    "render_markdown",
    "secret_references",
    "toolchain_pins",
    "write_pack",
]
