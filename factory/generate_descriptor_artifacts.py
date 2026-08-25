"""Generate every connector artifact from the descriptors. One source, three outputs.

    python -m factory.generate_descriptor_artifacts [--check]

Doctrine source: Apache Superset's `db_engine_specs`, where one declarative
`metadata` block per engine spec feeds the registry, the UI, the capability
matrix and all of its documentation pages — nobody hand-writes a connector doc.
(https://superset.apache.org/user-docs/databases/ ,
 https://deepwiki.com/apache/superset/4.2-database-engine-abstraction)

This script is the "generated" half of that. It reads `descriptor()` from every
registered driver and emits:

  A. `factory/artifacts/connector-roster.json` — the cockpit's chip list. Every
     registered driver becomes a chip automatically. No connector is ever again
     a hand-maintained roadmap entry somebody has to remember to flip.
  B. `docs/connectors/<dialect>.md` plus an index — the per-source prose that
     used to live, and go stale, in SKILL.md.
  C. `factory/artifacts/factory-status.json` — the per-source facts the
     FORGE-VIEW cards render, derived rather than hand-assembled.

Three properties this file exists to hold:

**Determinism (Law 1).** Running twice produces byte-identical output. There is
no wall clock anywhere in the emitted artifacts and every collection is sorted
or emitted in declaration order, because these files are committed and diffed:
a timestamp would make every regeneration a spurious diff, and a spurious diff
is how a real one gets waved through. `--check` regenerates into memory and
compares, so CI can fail on stale artifacts without writing anything.

**A descriptor is not a verdict.** The conformance state is joined in from the
evidence packs and the repair briefs, never inferred from the descriptor. A
driver that declares beautifully and has never been run against a real source
renders `pending`, and there is no code path by which it can render `passing`.
Letting the existence of a declaration read as green would be a proxy for the
thing actually measured, and a green that quietly rescopes what it measures is
worse than no green at all.

**Names, never values.** `required_env_keys` reaches the roster and the docs.
Those files are committed to git and served to a browser. `_assert_no_values`
re-checks the Law-3 shape at the emit boundary — the descriptor already
validated it at authoring time, and this is the belt to that suspenders,
because the cost of being wrong here is a credential in a public artifact.

**Projection-only (FV-1).** These files are the whole contract with the
cockpit. The browser reads the projection; it never imports the driver registry
and never touches the live control plane. Emitting stays here in r64-db-engine;
consuming stays in meshroad.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from factory.battery import FAIL, PASS, SKIPPED
from factory.evidence import atomic_write_text
from r64_db_engine.core.descriptor import DriverMetadata
from r64_db_engine.core.scrub import Scrubber
from r64_db_engine.drivers import descriptors

REPO_ROOT = Path(__file__).resolve().parents[1]

ARTIFACT_DIR = REPO_ROOT / "factory" / "artifacts"
DOCS_DIR = REPO_ROOT / "docs" / "connectors"
EVIDENCE_DIR = REPO_ROOT / "factory" / "evidence"
LAST_GREEN_DIR = EVIDENCE_DIR / "last-green"

ROSTER_PATH = ARTIFACT_DIR / "connector-roster.json"
STATUS_PATH = ARTIFACT_DIR / "factory-status.json"
DOC_INDEX_PATH = DOCS_DIR / "README.md"

#: Stamped at the top of every generated Markdown page. Present-tense and
#: specific about where the edit belongs, because the failure mode is somebody
#: fixing a typo here and watching it vanish on the next run.
DOC_BANNER = (
    "<!-- GENERATED FILE — DO NOT EDIT.\n"
    "     Emitted by factory/generate_descriptor_artifacts.py from the driver's\n"
    "     descriptor(). Edit the descriptor in\n"
    "     src/r64_db_engine/drivers/<dialect>/descriptor.py and regenerate:\n"
    "         python -m factory.generate_descriptor_artifacts\n"
    "     Hand edits here are overwritten and are how per-source prose went\n"
    "     stale in the first place. -->"
)

#: Conformance states. Three, not two, and the third is the load-bearing one.
#: FV-2's could-not-observe discipline: a driver nobody has run is neither
#: passing nor failing, and rendering it blank or green would be a claim we have
#: not earned. It renders as its own explicit state.
PASSING = "passing"
DRIFTED = "drifted"
PENDING = "pending"

STATE_LABEL = {
    PASSING: "conformance-passing",
    DRIFTED: "drifted — repair brief open",
    PENDING: "declared, pending conformance",
}


class GeneratorError(RuntimeError):
    """The generator refused to emit. Loud, never a partial write."""


# ---- the verdict join ------------------------------------------------


def _repair_briefs(repo_root: Path) -> dict[str, list[str]]:
    """Open repair briefs by dialect, from the filenames at the repo root.

    A brief exists only while a drift is unresolved — closing one deletes it —
    so presence is the signal and no parsing is required.
    """
    briefs: dict[str, list[str]] = {}
    pattern = re.compile(r"^REPAIR-BRIEF-(?P<dialect>[a-z0-9_]+)-(?P<date>\d{8})\.md$")
    for path in sorted(repo_root.glob("REPAIR-BRIEF-*.md")):
        m = pattern.match(path.name)
        if m:
            briefs.setdefault(m.group("dialect"), []).append(path.name)
    return briefs


def _last_green(dialect: str, last_green_dir: Path) -> dict[str, Any] | None:
    """The most recent evidence pack that passed for this dialect, if any."""
    path = last_green_dir / f"EVIDENCE-{dialect}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())  # type: ignore[no-any-return]
    except (OSError, json.JSONDecodeError) as exc:
        raise GeneratorError(
            f"last-green evidence pack for '{dialect}' is unreadable ({exc}). Refusing to emit: "
            f"an unreadable pack is not the same as an absent one, and quietly treating it as "
            f"absent would downgrade a real verdict to 'pending' without saying so"
        ) from exc


#: The battery check that computes the lane-scoped checksum and the two-pull
#: byte identity behind it. Its PASS is the one that makes a verdict
#: CHECKSUM-BACKED rather than merely asserted, which is the property the emit
#: boundary authenticates against.
_CHECKSUM_CHECK = "checksum"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _unauthenticated(pack: dict[str, Any]) -> str | None:
    """Why this pack is not validated oracle output, or None when it is.

    # The hole this closes

    The emit boundary read `pack["verdict"]` and rendered green on the strength
    of it. That made GREEN A FUNCTION OF A WRITABLE FILE: anybody who could
    place `{"verdict": "PASS"}` into `last-green/` — a well-meaning hand edit, a
    half-finished sweep, a generated fixture that escaped its test — created a
    passing state for a driver nothing had ever run. The whole consolidation
    rests on descriptor-existence never reading as conformance, and this was the
    same proxy pattern one level out: EVIDENCE-existence reading as conformance.

    # What is checked instead

    Not a richer SHAPE — a richer shape is the same mistake with more fields.
    The verdict is RE-DERIVED from the oracle's own per-check output, using the
    oracle's own rule (`EvidencePack.verdict`: FAIL if any check failed), and
    the pack's claims must AGREE with that re-derivation:

      * `checks` must be the battery's records, each naming a check and a status;
      * the re-derived verdict must be PASS, and must equal the recorded one;
      * the re-derived tally must equal the recorded tally;
      * the `checksum` check must be present and PASS — this is what makes the
        verdict checksum-backed rather than asserted;
      * the artifact must carry two pull digests that are well-formed sha256 and
        EQUAL, which is the byte-identity that check actually proved.

    A pack that satisfies all of that had a battery run behind it. A file
    somebody wrote does not, and cannot be made to without running one.

    # Why this returns a reason instead of raising

    An unreadable pack still raises — it is a corrupted verdict, and treating it
    as absent would silently downgrade a real one. But a pack that simply does
    not carry oracle evidence is a COULD-NOT-OBSERVE: nothing has been proven
    about this dialect, which is precisely what `pending` means and exactly the
    state a driver with no pack at all renders. Refusing to emit the whole
    roster because one dialect's evidence is unauthenticated would take the
    cockpit down over a state the cockpit is designed to display. The reason is
    carried into the rendered note, so it is declared, not hidden.
    """
    checks = pack.get("checks")
    if not isinstance(checks, list) or not checks:
        return (
            "the pack carries no `checks` array, so there is no oracle output to authenticate "
            "against — its verdict is asserted rather than earned"
        )
    if not all(isinstance(c, dict) and isinstance(c.get("status"), str) for c in checks):
        return "the pack's `checks` are not battery check records (each needs a `status`)"

    statuses = [c["status"] for c in checks]
    derived_verdict = FAIL if FAIL in statuses else PASS
    if derived_verdict != PASS:
        return "re-deriving the verdict from the pack's own checks gives FAIL, not PASS"

    recorded_status = pack.get("verdict_status")
    if recorded_status != derived_verdict:
        return (
            f"the pack records verdict_status {recorded_status!r} but its checks derive "
            f"{derived_verdict!r} — the claim and the evidence disagree"
        )

    derived_tally = {s: statuses.count(s) for s in (PASS, FAIL, SKIPPED)}
    if pack.get("tally") != derived_tally:
        return (
            f"the pack's recorded tally {pack.get('tally')!r} is not the tally of its own "
            f"checks {derived_tally!r}"
        )

    by_name = {c.get("name"): c["status"] for c in checks}
    if by_name.get(_CHECKSUM_CHECK) != PASS:
        return (
            f"the pack carries no passing '{_CHECKSUM_CHECK}' check, so its verdict is not "
            f"checksum-backed"
        )

    artifact = pack.get("artifact")
    if not isinstance(artifact, dict):
        return "the pack records no artifact, so the checksum check has nothing behind it"
    first, second = artifact.get("sha256_pull1"), artifact.get("sha256_pull2")
    if not (isinstance(first, str) and _SHA256.match(first)):
        return "the pack's artifact carries no well-formed sha256 for the first pull"
    if first != second:
        return (
            "the pack's two pull digests differ, so the byte-identity its checksum check "
            "claims to have proved does not hold in the pack itself"
        )
    return None


def conformance_state(
    dialect: str,
    last_green_dir: Path,
    briefs: dict[str, list[str]],
) -> dict[str, Any]:
    """Join the oracle's verdict onto a dialect. Never reads the descriptor.

    That this function takes a dialect string and not a `DriverMetadata` is the
    anti-proxy guard expressed in the signature: there is nothing about the
    declaration it *could* consult, so there is no way for a well-authored
    descriptor to talk its way into a green.
    """
    pack = _last_green(dialect, last_green_dir)
    open_briefs = briefs.get(dialect, [])

    if pack is None:
        return {
            "state": PENDING,
            "label": STATE_LABEL[PENDING],
            "evidence": None,
            "open_repair_briefs": open_briefs,
            "note": (
                "No conformance evidence pack has been committed for this dialect. The driver "
                "declares its shape; nothing has yet checked that shape against a real source."
            ),
        }

    verdict = pack.get("verdict")
    if verdict != "PASS":
        raise GeneratorError(
            f"the last-green pack for '{dialect}' carries verdict {verdict!r}, not PASS. "
            f"Refusing to emit: last-green is by definition the last PASSING run, so a "
            f"non-PASS pack in that directory means the sweep wrote somewhere it should not "
            f"have, and emitting around it would launder a failure into a status page"
        )

    unauthenticated = _unauthenticated(pack)
    if unauthenticated is not None:
        return {
            "state": PENDING,
            "label": STATE_LABEL[PENDING],
            "evidence": None,
            "open_repair_briefs": open_briefs,
            "note": (
                f"An evidence pack is present for this dialect but is NOT validated oracle "
                f"output: {unauthenticated}. Green originates from a battery run against a "
                f"real source, never from the presence or the shape of a file, so this renders "
                f"as pending — the same state a dialect with no pack at all renders, because "
                f"the same amount has been proven."
            ),
        }

    evidence = {
        "verdict": verdict,
        "generated_utc": pack.get("generated_utc"),
        "table": pack.get("table"),
        "tally": pack.get("tally"),
        "ratifies_head": pack.get("ratifies_head"),
        "commit": pack.get("provenance", {}).get("git", {}).get("commit"),
    }

    if open_briefs:
        return {
            "state": DRIFTED,
            "label": STATE_LABEL[DRIFTED],
            "evidence": evidence,
            "open_repair_briefs": open_briefs,
            "note": (
                "This dialect has passed conformance before, but a repair brief is open against "
                "it, so the green shown is stale. The last green is reported rather than hidden "
                "— the useful question during a drift is what changed since it."
            ),
        }

    return {
        "state": PASSING,
        "label": STATE_LABEL[PASSING],
        "evidence": evidence,
        "open_repair_briefs": [],
        "note": "Checksum-backed verdict from the conformance battery against a live source.",
    }


# ---- Law 3 at the emit boundary --------------------------------------

_VALUE_SHAPED = re.compile(r"[=:/@\s]")


def _assert_no_values(meta: DriverMetadata) -> None:
    """Refuse to emit a descriptor whose env keys look like values.

    `DriverMetadata.validate()` already enforced this when the descriptor was
    constructed. Re-checking here is deliberate duplication: this is the last
    point before a credential would be written into a committed, served file,
    and a check at the boundary you actually care about is worth more than an
    appeal to one upstream that a refactor could route around.
    """
    for key in meta.required_env_keys:
        if _VALUE_SHAPED.search(key) or not key.isupper():
            raise GeneratorError(
                f"descriptor '{meta.dialect}' required_env_keys entry {key!r} is not a bare "
                f"env-var NAME. Refusing to emit — these artifacts are committed and served, "
                f"and a value here is a credential in public (Law 3)"
            )


#: A field declared as PROSE must BE prose: two whitespace-separated words at
#: the very least. The check is deliberately the same species as
#: `_ENV_KEY_NAME` in `core.descriptor` — it requires the POSITIVE FORM of the
#: thing, rather than hunting for known-bad content. A denylist finds only what
#: it already knows, and the credential nobody has seen yet is the one that
#: matters. A credential is a single opaque token; an operator message is a
#: sentence written for a human to read. A bare token sitting in a prose slot is
#: not a message, it is a value wearing a message's clothes.
_PROSE = re.compile(r"\S+\s+\S+")


def _prose_of(meta: DriverMetadata) -> Iterator[str]:
    """Every free-prose string this descriptor contributes to an artifact.

    Enumerated from the descriptor rather than from the rendered text, because
    the shape question ("is this field prose?") is only answerable while the
    field still knows which field it is.
    """
    yield meta.doc_summary
    yield from meta.notes
    for tm in meta.type_mappings:
        yield tm.note
    for em in meta.custom_errors:
        yield em.operator_message


def emit_scrubber(metas: dict[str, DriverMetadata]) -> Scrubber:
    """Build THE boundary for this generation run. Registers, never renders.

    # Why one boundary and not two guards

    Two value-leak paths were found in round 1, and they are the same defect
    seen from two sides:

      * `required_env_keys` was checked for name SYNTAX only, and a live
        credential value is syntactically indistinguishable from a key —
        `ULTRASECRET2026` matches `^[A-Z][A-Z0-9_]*$` exactly as well as
        `PGPASSWORD` does. Shape cannot separate them; only the environment can.
      * authored PROSE — `doc_summary`, `notes`, type-map notes, operator
        messages — was emitted with no value scan at all. `ErrorMap` rejects
        interpolation PLACEHOLDERS, which stops a provider value being spliced
        in at runtime; it says nothing about a secret already sitting in the
        authored string.

    A guard per path closes one and leaves the other bleeding, and then leaves
    the NEXT field somebody adds unguarded too. So this is one boundary over the
    whole post-descriptor-load emit path, and it covers every field of
    `connector-roster.json`, `factory-status.json` and every generated doc page
    alike — projection fields and free prose, no distinction. That is round 6's
    ruling applied here: one rule everywhere beats a rule that depends on which
    kind of field the caller happens to be holding, because the second kind is
    the kind that drifts.

    # What it registers

    Two mechanisms, mirroring the `Scrubber`'s own two:

    1. **Every live environment VALUE.** Names reach these artifacts by design;
       values must not, and the only thing that can tell one from the other is
       the environment itself. Consulted to SUBTRACT from the output, never to
       add to it — no environment value is ever a source of emitted content, so
       Law 1 stands and the clean case is a byte-for-byte no-op.
    2. **Any prose field that is not prose.** See `_PROSE`.

    Registration is separated from scrubbing on purpose: this function decides
    what must not appear, and `generate()` applies it to the FINAL SERIALIZED
    TEXT of every artifact — the same belt-and-braces shape `emit_drift` uses,
    for the same reason. Scrubbing fields one at a time depends on every current
    and future field being remembered; scrubbing the serialized output is a
    property of the output.

    # The declared limit

    `Scrubber.MIN_SCRUBBABLE_LENGTH` still applies, so a credential under eight
    characters is not registered, and a secret embedded INSIDE an otherwise
    well-formed authored sentence is not detectable by shape. Both are stated
    rather than papered over. Neither is what this boundary is for: it is
    defence in depth behind descriptor-time validation and behind review of
    authored descriptors, exactly as round 6 settled for the recipe lane.
    """
    scrubber = Scrubber()
    for value in os.environ.values():
        scrubber.register_secret(value)
    for meta in metas.values():
        for prose in _prose_of(meta):
            if prose.strip() and not _PROSE.search(prose):
                scrubber.register_secret(prose)
    return scrubber



# ---- Output A: the cockpit roster projection -------------------------


def build_roster(
    metas: dict[str, DriverMetadata],
    states: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """The chip list. Every registered driver, automatically.

    What this replaces is a hardcoded list in the cockpit where a connector the
    factory had already landed still had to be flipped by hand — so the UI could
    be wrong about the system for as long as it took somebody to notice.
    """
    return {
        "schema": "connector-roster/v1",
        "generator": "factory/generate_descriptor_artifacts.py",
        "note": (
            "Projection, not an API. The cockpit reads this file and nothing else: it does not "
            "import the driver registry and does not reach the live control plane. Emitted by "
            "r64-db-engine, consumed by meshroad."
        ),
        "connectors": [
            {
                "dialect": dialect,
                "engine_name": meta.engine_name,
                "auth_mode": meta.auth_mode.value,
                "config_profile": meta.config_profile,
                "extras_package": meta.extras_package,
                "capabilities": meta.as_dict()["capabilities"],
                "doc": f"docs/connectors/{dialect}.md",
                "conformance": states[dialect],
            }
            for dialect, meta in metas.items()
        ],
    }


# ---- Output C: FORGE-VIEW per-source facts ---------------------------


def build_status(
    metas: dict[str, DriverMetadata],
    states: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """The FORGE-VIEW card facts, derived from descriptors rather than authored.

    Two fields deliberately kept apart, because collapsing them is the trap:
    `declared` is everything the driver says about itself, and `conformance` is
    everything the oracle proved. A card renders both. Nothing in `declared`
    can move `conformance`.
    """
    payload = {
        "schema": "factory-status/v1",
        "generator": "factory/generate_descriptor_artifacts.py",
        "note": (
            "`declared` is what the driver says about itself; `conformance` is what the "
            "checksum-backed battery proved against a real source. They are separate fields "
            "because a declaration is not evidence, and a card that let the first colour the "
            "second would be measuring the wrong thing."
        ),
        "counts": {
            "registered": len(metas),
            PASSING: sum(1 for s in states.values() if s["state"] == PASSING),
            DRIFTED: sum(1 for s in states.values() if s["state"] == DRIFTED),
            PENDING: sum(1 for s in states.values() if s["state"] == PENDING),
        },
        "sources": {
            dialect: {
                "declared": meta.as_dict(),
                "conformance": states[dialect],
            }
            for dialect, meta in metas.items()
        },
    }
    payload["descriptor_digest"] = hashlib.sha256(
        json.dumps({d: m.as_dict() for d, m in metas.items()}, indent=2, sort_keys=True).encode()
    ).hexdigest()
    return payload


# ---- Output B: the generated connector docs --------------------------


def _yesno(value: bool) -> str:
    return "yes" if value else "no"


def render_doc(meta: DriverMetadata, state: dict[str, Any]) -> str:
    """One connector's documentation page, entirely from its descriptor."""
    caps = meta.capabilities
    lines: list[str] = [
        DOC_BANNER,
        "",
        f"# {meta.engine_name}",
        "",
        f"**Dialect key:** `{meta.dialect}` — the identity this connector is selected by, "
        f"in config and in the registry.",
        "",
        f"**Conformance:** {state['label']}.",
        "",
        f"> {state['note']}",
        "",
    ]

    ev = state.get("evidence")
    if ev:
        lines += [
            f"Last green run `{ev.get('generated_utc')}` against `{ev.get('table')}`, "
            f"tally `{json.dumps(ev.get('tally'), sort_keys=True)}`, ratifying commit "
            f"`{(ev.get('commit') or '')[:12]}`.",
            "",
        ]
    for brief in state.get("open_repair_briefs", []):
        lines += [f"Open repair brief: `{brief}`.", ""]

    lines += ["## What it is", "", meta.doc_summary, "", "## Connecting", ""]
    lines += [f"**Auth mode:** `{meta.auth_mode.value}`", ""]
    lines += [f"**Config profile:** `{meta.config_profile}`", ""]

    if meta.extras_package:
        lines += [
            f"**Install extra:** `pip install 'r64-db-engine[{meta.extras_package}]'`",
            "",
        ]
    else:
        lines += ["**Install extra:** none — dependencies are in the base set.", ""]

    if meta.required_env_keys:
        lines += [
            "**Required environment variables.** Names only — this page is generated and "
            "committed, so no value from your environment appears here or in any other "
            "generated artifact.",
            "",
        ]
        lines += [f"- `{key}`" for key in meta.required_env_keys]
        lines.append("")
    else:
        lines += ["**Required environment variables:** none. This source needs no credential.", ""]

    lines += [
        "## Capabilities",
        "",
        "| capability | supported | what it means |",
        "|---|---|---|",
        f"| `supports_arrow` | {_yesno(caps.supports_arrow)} | hands back Arrow natively, "
        "without a pandas round-trip |",
        f"| `supports_streaming` | {_yesno(caps.supports_streaming)} | produces the table in "
        "chunks, re-blocked to the 65536-row Arrow IPC layout |",
        f"| `supports_incremental` | {_yesno(caps.supports_incremental)} | watermark mode; "
        "without it a config requesting one is refused, never silently downgraded |",
        f"| `supports_catalog` | {_yesno(caps.supports_catalog)} | a catalog layer above schema |",
        f"| `stable_scan_order` | {_yesno(caps.stable_scan_order)} | row order repeats across "
        "pulls without an ORDER BY — an observation, not a guarantee |",
        f"| `tz_sensitive` | {_yesno(caps.tz_sensitive)} | session timezone can shift returned "
        "timestamps; aggregate parity is blind to a uniform shift, which is why min/max "
        "boundaries are asserted |",
        "",
    ]

    if meta.type_mappings:
        lines += [
            "## Type representability",
            "",
            "What happens to a source type on the way into the ramdb. `refused` is a feature: "
            "the writer fails loudly rather than landing a value that is quietly wrong.",
            "",
            "| source type | lands as | verdict | note |",
            "|---|---|---|---|",
        ]
        for tm in meta.type_mappings:
            note = tm.note.replace("|", "\\|") or "—"
            lines.append(
                f"| `{tm.source_type}` | `{tm.arrow_type}` | **{tm.verdict.value}** | {note} |"
            )
        lines.append("")

    if meta.custom_errors:
        lines += [
            "## Failure modes",
            "",
            "Operator messages are value-free by construction: they name the configured side "
            "only and never echo bytes from the source's own error text.",
            "",
            "| reason code | what to do |",
            "|---|---|",
        ]
        for em in meta.custom_errors:
            msg = em.operator_message.replace("|", "\\|")
            lines.append(f"| `{em.reason_code}` | {msg} |")
        lines.append("")

    if meta.notes:
        lines += ["## Notes", ""]
        lines += [f"- {note}" for note in meta.notes]
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_index(
    metas: dict[str, DriverMetadata],
    states: dict[str, dict[str, Any]],
) -> str:
    lines = [
        DOC_BANNER,
        "",
        "# Connectors",
        "",
        "One page per registered driver, generated from that driver's `descriptor()`.",
        "This directory replaces the hand-written per-source prose that used to live in "
        "SKILL.md and drift against the code.",
        "",
        "| connector | dialect | auth | conformance |",
        "|---|---|---|---|",
    ]
    for dialect, meta in metas.items():
        lines.append(
            f"| [{meta.engine_name}]({dialect}.md) | `{dialect}` | "
            f"`{meta.auth_mode.value}` | {states[dialect]['label']} |"
        )
    lines += [
        "",
        "`declared, pending conformance` means exactly what it says: the driver describes its "
        "shape, and no evidence pack proves that shape against a real source yet. It is a "
        "distinct state from passing and from drifted, and it is never rendered as green.",
        "",
    ]
    return "\n".join(lines).rstrip() + "\n"


# ---- driving ---------------------------------------------------------


def generate(repo_root: Path = REPO_ROOT) -> dict[Path, str]:
    """Build every artifact in memory. Pure: writes nothing, reads no clock."""
    metas = descriptors()
    if not metas:
        raise GeneratorError(
            "the driver registry is empty. Refusing to emit: overwriting the roster with zero "
            "connectors would render the cockpit blank, which reads as 'nothing is wrong' "
            "rather than 'nothing was found'"
        )

    for dialect, meta in metas.items():
        _assert_no_values(meta)
        if meta.dialect != dialect:
            raise GeneratorError(
                f"driver registered under '{dialect}' declares dialect '{meta.dialect}'. The "
                f"registry key and the descriptor's identity must be the same string, or the "
                f"chip and the config select different things"
            )

    briefs = _repair_briefs(repo_root)
    last_green_dir = repo_root / "factory" / "evidence" / "last-green"
    states = {d: conformance_state(d, last_green_dir, briefs) for d in metas}

    out: dict[Path, str] = {
        repo_root / "factory" / "artifacts" / "connector-roster.json": json.dumps(
            build_roster(metas, states), indent=2, sort_keys=True
        )
        + "\n",
        repo_root / "factory" / "artifacts" / "factory-status.json": json.dumps(
            build_status(metas, states), indent=2, sort_keys=True
        )
        + "\n",
        repo_root / "docs" / "connectors" / "README.md": render_index(metas, states),
    }
    for dialect, meta in metas.items():
        out[repo_root / "docs" / "connectors" / f"{dialect}.md"] = render_doc(meta, states[dialect])

    # THE emit boundary. Everything above renders; nothing above is trusted to
    # have rendered only names. Applied to the finished text of every artifact
    # rather than field by field, so a field added later is covered without
    # anybody remembering to cover it.
    scrubber = emit_scrubber(metas)
    return {path: scrubber.scrub(text) for path, text in out.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="generate_descriptor_artifacts",
        description="Emit the connector roster, the FORGE-VIEW status projection, and the "
        "generated connector docs from every registered driver's descriptor().",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit non-zero if any artifact on disk differs from what would "
        "be generated (for CI and the sweep)",
    )
    args = parser.parse_args(argv)

    try:
        artifacts = generate()
    except GeneratorError as exc:
        print(f"generator refused: {exc}", file=sys.stderr)
        return 2

    if args.check:
        stale = [
            path
            for path, text in sorted(artifacts.items())
            if not path.is_file() or path.read_text() != text
        ]
        for path in stale:
            print(f"STALE {path.relative_to(REPO_ROOT)}", file=sys.stderr)
        if stale:
            print(
                f"\n{len(stale)} generated artifact(s) do not match the descriptors. "
                f"Run: python -m factory.generate_descriptor_artifacts",
                file=sys.stderr,
            )
            return 1
        print(f"{len(artifacts)} generated artifact(s) match the descriptors.")
        return 0

    for path, text in sorted(artifacts.items()):
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, text)
        print(f"wrote {path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
