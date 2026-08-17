"""Repair-brief generation — the template from the `r64-factory-maintenance` skill.

When the sweep goes red, this writes `REPAIR-BRIEF-<dialect>-<YYYYMMDD>.md`:
symptom, an evidence-pack diff against the last green run, a re-research
directive, and a re-admission checklist.

The brief is deliberately a WORK ORDER rather than a report. It ends in
checkboxes because the failure mode of automated drift detection is a notice
nobody can act on — an alert that says "conformance failed" and stops has moved
the problem from the pipeline to the reader's memory.

Two rules the template carries and this module must not soften:

- **Drift triggers re-research, never runtime interpretation** (Law 1). The fix
  is a rebuild, never an engine that adapts to what it now sees.
- **Never edit ground truth to match a failing pipeline.** That converts an
  oracle into a mirror, and every subsequent run passes by construction.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

ENV_KEYS = ("python", "platform")
PACKAGE_KEYS = ("pyarrow", "pandas", "pydantic", "clickhouse_connect", "httpx", "jsonschema")


def _checks(pack: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not pack:
        return {}
    return {c["name"]: c for c in pack.get("checks", [])}


def _comparisons(check: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not check:
        return {}
    return {c["label"]: c for c in check.get("comparisons", [])}


def _cell(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = text.replace("|", "\\|").replace("\n", " ")
    return text if len(text) <= 200 else text[:197] + "..."


def render_repair_brief(
    *,
    dialect: str,
    target: str,
    table: str,
    pack: dict[str, Any] | None,
    previous: dict[str, Any] | None,
    date: str,
    error: str | None = None,
) -> str:
    now = _checks(pack)
    then = _checks(previous)
    failing = [name for name, check in now.items() if check.get("status") == "FAIL"]

    lines: list[str] = []
    lines.append(f"# REPAIR BRIEF — {dialect} — {date[:4]}-{date[4:6]}-{date[6:]}")
    lines.append("")
    lines.append(
        "Auto-instantiated by `factory-conformance-sweep`. Status: **OPEN**."
    )
    lines.append(f"Target: `factory/targets/{target}.yaml` · Table: `{table}`")
    lines.append("")

    # 1. Symptom
    lines.append("## 1. Symptom")
    lines.append("")
    lines.append("| field | value |")
    lines.append("|---|---|")
    lines.append(f"| Failing check(s) | {', '.join(f'`{n}`' for n in failing) or '(run errored)'} |")
    lines.append(f"| This run | {_cell(pack.get('generated_utc') if pack else 'n/a')} |")
    lines.append(
        f"| Last green run | {_cell(previous.get('generated_utc') if previous else 'none recorded')} |"
    )
    if pack:
        tally = pack.get("tally", {})
        lines.append(
            f"| Verdict | {tally.get('PASS', 0)} passed / {tally.get('FAIL', 0)} failed / "
            f"{tally.get('SKIPPED', 0)} skipped |"
        )
    if error:
        lines.append(f"| Run error | {_cell(error)} |")
    lines.append("")

    if error:
        lines.append(
            "> The battery did not complete. An environmental prerequisite (source down, "
            "container stopped, network unreachable) is a RED sweep and never a skip — "
            "the week the sweep quietly checks nothing is the week it was needed."
        )
        lines.append("")

    for name in failing:
        lines.append(f"Failure detail for `{name}`, verbatim from the pack:")
        lines.append("")
        lines.append("> " + str(now[name].get("detail", "")).replace("\n", " "))
        lines.append("")

    # 2. Evidence diff
    lines.append("## 2. Evidence-pack diff vs last green")
    lines.append("")
    if not previous:
        lines.append(
            "_No last-green pack was recorded for this dialect, so no diff is possible._ "
            "The first green sweep after this one will establish the baseline."
        )
        lines.append("")
    else:
        lines.append("| check | last green | now |")
        lines.append("|---|---|---|")
        for name in sorted(set(now) | set(then)):
            was = then.get(name, {}).get("status", "(absent)")
            is_now = now.get(name, {}).get("status", "(absent)")
            marker = "" if was == is_now else "  **<-- moved**"
            lines.append(f"| `{name}` | {was} | {is_now}{marker} |")
        lines.append("")

        for name in failing:
            before = _comparisons(then.get(name))
            after = _comparisons(now.get(name))
            # EVERY failing comparison is listed, and both sides of each are
            # shown from both runs. Filtering to "the actual value moved" was
            # wrong and hid the most common case outright: when the EXPECTED
            # side moves — a re-captured ground truth, a re-declared spec — the
            # pipeline's own value is unchanged, so an actual-only filter
            # produced a repair brief with an empty diff under a FAIL verdict.
            failing_comparisons = [
                label for label, comparison in after.items() if not comparison.get("ok")
            ]
            if not failing_comparisons:
                continue
            lines.append(f"Failing comparisons in `{name}`:")
            lines.append("")
            lines.append(
                "| comparison | actual (last green) | actual (now) "
                "| expected (last green) | expected (now) |"
            )
            lines.append("|---|---|---|---|---|")
            for label in failing_comparisons:
                was = before.get(label, {})
                is_now = after[label]
                lines.append(
                    f"| {_cell(label)} "
                    f"| `{_cell(was.get('actual', '(absent)'))}` "
                    f"| `{_cell(is_now.get('actual'))}` "
                    f"| `{_cell(was.get('expected', '(absent)'))}` "
                    f"| `{_cell(is_now.get('expected'))}` |"
                )
            lines.append("")
            lines.append(
                "> Read the two *expected* columns first. If they differ, the EXPECTATION "
                "moved (a re-captured ground truth or an edited spec) and the pipeline may "
                "be fine. If only the *actual* columns differ, the SOURCE or the pipeline "
                "moved — that is real drift."
            )
            lines.append("")

    # Environment delta — where "the source changed" and "we changed" separate
    lines.append("Environment delta — this is where *the source changed* and *we changed* separate:")
    lines.append("")
    lines.append("| | last green | now |")
    lines.append("|---|---|---|")
    now_env = (pack or {}).get("environment", {})
    then_env = (previous or {}).get("environment", {})
    for key in ENV_KEYS:
        lines.append(f"| {key} | {_cell(then_env.get(key, '?'))} | {_cell(now_env.get(key, '?'))} |")
    for key in PACKAGE_KEYS:
        lines.append(
            f"| {key} | {_cell(then_env.get('packages', {}).get(key, '?'))} "
            f"| {_cell(now_env.get('packages', {}).get(key, '?'))} |"
        )
    for label, key in (("container image", "image"), ("container digest", "image_id")):
        lines.append(
            f"| {label} | {_cell(then_env.get('container', {}).get(key, '?'))} "
            f"| {_cell(now_env.get('container', {}).get(key, '?'))} |"
        )
    for label, key in (("git commit", "commit"), ("git branch", "branch")):
        lines.append(
            f"| {label} | {_cell(then_env.get('git', {}).get(key, '?'))} "
            f"| {_cell(now_env.get('git', {}).get(key, '?'))} |"
        )
    lines.append("")
    lines.append(
        "> `pyarrow` owns the Arrow IPC block layout and `pandas` decides `string` vs "
        "`large_string`. If either moved, suspect environment drift before source drift — "
        "and **pin**, do not widen the check."
    )
    lines.append("")

    # 3. Re-research
    lines.append("## 3. Re-research directive")
    lines.append("")
    lines.append("Law 1 — the fix is re-research at BUILD time, not a runtime adaptation.")
    lines.append("")
    lines.append("- [ ] Re-read the provider's current documentation for the affected surface.")
    lines.append(
        "- [ ] Probe the live source directly, WITHOUT the driver. Verifying the driver "
        "with the driver hides faults in both directions at once."
    )
    lines.append("- [ ] Re-fill the affected `DRIVER-PLAN.md` rows, especially the trap rows:")
    lines.append(
        "      int32/int64 ceiling · Decimal · timestamp + session timezone · "
        "null vs NaN · scan-order determinism."
    )
    lines.append("- [ ] State what CHANGED at the source, in one sentence, with evidence.")
    lines.append("")

    # 4. Re-admission
    lines.append("## 4. Re-admission requirement")
    lines.append("")
    lines.append(
        "A repaired driver re-enters through the **full** battery — not the failed check "
        "alone. A source change that moved one property has usually moved others, and a "
        "targeted re-run would confirm only what you already suspected."
    )
    lines.append("")
    lines.append("- [ ] `DRIVER-PLAN.md` rows updated and **ratified by Kos** before any code.")
    lines.append(
        f"- [ ] Fix authored; zero core edits "
        f"(`git grep -rniE \"\\b{dialect}\\b\" src/r64_db_engine/core/` empty)."
    )
    lines.append(
        "- [ ] If the battery could not have caught this earlier: **extend the battery "
        "first** (Law 4), with a failing fixture proving the new check can fail. "
        "Never widen a tolerance to make a run green."
    )
    lines.append(
        f"- [ ] `.venv/bin/python -m factory.conformance --dialect {dialect} "
        f"--config factory/targets/{target}.yaml --table {table} --serve-gate` → exit 0."
    )
    lines.append("- [ ] New evidence pack committed; suite green via `.venv/bin/pytest`.")
    lines.append("- [ ] Cross-agent QA before merge. Builder ≠ auditor.")
    lines.append("")

    # 5. Disposition
    lines.append("## 5. Disposition")
    lines.append("")
    lines.append("- [ ] Repaired and re-admitted — closing pack: `________`")
    lines.append(
        "- [ ] Ground truth legitimately changed — new capture committed, with the reason "
        "recorded. **NEVER edit ground truth to match a failing pipeline**: that converts "
        "an oracle into a mirror and every later run passes by construction."
    )
    lines.append("- [ ] Accepted as a known limit — fenced, with scope stated.")
    lines.append("")
    return "\n".join(lines)


def write_repair_brief(
    *,
    dialect: str,
    target: str,
    table: str,
    pack_path: Path | None,
    previous: dict[str, Any] | None,
    brief_dir: Path,
    date: str | None = None,
    error: str | None = None,
) -> Path:
    pack = json.loads(pack_path.read_text()) if pack_path and pack_path.exists() else None
    stamp = date or datetime.now().strftime("%Y%m%d")
    brief_dir.mkdir(parents=True, exist_ok=True)
    path = brief_dir / f"REPAIR-BRIEF-{dialect}-{stamp}.md"
    path.write_text(
        render_repair_brief(
            dialect=dialect, target=target, table=table, pack=pack,
            previous=previous, date=stamp, error=error,
        )
    )
    return path


__all__ = ["render_repair_brief", "write_repair_brief"]
