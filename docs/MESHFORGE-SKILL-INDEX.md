# MESHFORGE — Software Factory Skill Index
**Canonical registry of factory skills. Reference this document when scoping any build that adds, maintains, or promotes an integration/plane across meshroad, r64-db-engine, or Partical.**

Version: 1.0 (2026-08-16)
Provenance: Ramp "Integrations That Write Themselves" (builders.ramp.com/post/integrations-that-write-themselves) adapted to the MeshCave/Row64 substrate.

---

## Naming & location conventions

- **Umbrella name:** `MESHFORGE`. The factory as a system. Not a repo — a doctrine + skill set that lives across repos.
- **Skill naming:** domain prefix matches existing convention (`r64-*` for the ingestion-plane factory, `partical-*` for the platform factory, bare `meshforge` for cross-cutting doctrine). Kebab-case, no versions in names.
- **Locations:**
  - Cross-cutting core → `~/.claude/skills/meshforge/SKILL.md` (user-global; CC reads it in every project).
  - r64 lane → `/home/kos/builds/r64-db-engine/.claude/skills/<name>/SKILL.md` (travels with the repo, versioned with the contract it encodes).
  - Partical lane → `~/partical/.claude/skills/<name>/SKILL.md`.
- **Brief naming:** `CC-BRIEF-<scope>.md` at repo root (existing convention). Factory-generated briefs: `CC-BRIEF-driver-<dialect>.md`, repair briefs `REPAIR-BRIEF-<dialect>-<YYYYMMDD>.md`.
- **Evidence packs:** `factory/evidence/EVIDENCE-<dialect>-<YYYYMMDD>.md` + sibling `.json`. Evidence packs are the review artifact of record — reviewers verify evidence, not diffs.

---

## The Four Laws (bind every factory skill)

1. **Model at build time, never runtime.** Agents research, author, and test once; what ships is deterministic (a driver, or a compiled recipe book). Drift triggers re-research, never runtime interpretation.
2. **Trust the artifacts, not the code.** Every factory output ships an evidence pack: endpoints touched, real requests/responses, conformance results, counters. Review operates on the pack + cross-agent QA (Codex↔CC↔Claude — no reviewer catches its own errors).
3. **Credentials never enter model context.** Keys land in 0600 env files referenced by path. Agents are instructed to *use* the path, never read/echo contents. (MinIO rotation incident is the precedent; AWS ambient-env fence is the pattern.)
4. **Autonomy is bounded by verification.** A driver/recipe is admitted only through the conformance battery. If the battery can't check it, the factory can't ship it — extend the battery first.

---

## Skill registry

### `meshforge` (core doctrine) — `~/.claude/skills/meshforge/`
The umbrella skill. Encodes: the Four Laws, the two-tier model (first-party driver lane vs recipe lane), evidence-pack standard, credential law, cross-agent QA gate, deviation-disclosure protocol, commit-before-handoff, refuse-loudly doctrine.
**Triggers:** "software factory", "meshforge", "factory doctrine", "evidence pack", any request to add an agentic build/maintain workflow to a project.
**Composes with:** every skill below; `skill-creator` when authoring new lane skills.

### `r64-connector-factory` — r64-db-engine repo
Generates and drives first-party driver campaigns on the Driver ABC. Encodes the gate battery as a template: Phase 0 topology audit (gated on Kos ack) → **research phase producing `DRIVER-PLAN.md`** (fixed table: auth model, pagination, rate limits, type map with named trap rows — int32 ceiling / Decimal / timestamp-tz / null semantics / scan-order determinism — sandbox strategy) → Gate A repo-green → Gate B seed + ground-truth transfer (B-2 min/max boundary assertion mandatory) → Gate C conformance (via `r64-conformance`) → Gate D dev-serve zero-copy → Gate E bench (optional, full bench doctrine, kill conditions standing). Plan is ratified by Kos before code; agent runs to conformance-green; claims-register entries drafted fenced.
**Triggers:** "add a <X> driver", "new connector", "build the <X> integration", "driver campaign", "factory a driver", "DRIVER-PLAN".
**Outputs:** `CC-BRIEF-driver-<dialect>.md`, `DRIVER-PLAN.md`, evidence pack, claims entries.
**Composes with:** `r64-conformance`, `r64-db-engine` (contract facts), `meshforge`.

### `r64-conformance` — r64-db-engine repo
The machine-checkable oracle. Wraps `factory/conformance.py`: parameterized acceptance battery per registered dialect — schema exactness, aggregate parity vs ground-truth JSON (exact-int authority gates, float corroborates), RF-002 null discriminator (**dataset-declared discriminators; floor ≥1, exact null counts gate**), B-2 min/max tz-boundary, PG-011 watermark refusal, 65536 block-structure, lane-scoped checksum, optional dev-serve `copied_columns=0` gate. Emits evidence pack. Exit code is the verdict.

> **Note on the RF-002 rule (corrected 2026-08-16).** This entry previously read "≥2 actively discriminating columns". That number came from the Supabase `intel_logs` claim and does not generalize: meshbench has exactly one nullable column, so a floor of 2 is unsatisfiable there, while a bare floor of 1 is vacuous on a wide dataset. The rule is therefore dataset-relative and strictly stronger than either: the conformance spec must **declare** the expected discriminating columns with their **exact** null counts (meshbench: `score`, 20,039 at 1M), and the gate is that every declared discriminator discriminates with exact counts, with at least one declared. Declaring zero is permitted only with an explicit `discriminators_absent_reason`, which yields SKIPPED-with-reason; omitting the declaration is a FAIL.
**Triggers:** "run conformance", "admit the driver", "conformance battery", "evidence pack for <dialect>", "is <dialect> reference-grade".
**Composes with:** `r64-connector-factory` (Gate C), `r64-factory-maintenance` (re-runs).

### `r64-recipe-engine` — r64-db-engine repo
Long-tail lane. Authors **recipe books** for the generic `rest` dialect: a recipe = one authenticated call (method, URL pinned at creation, auth = env-file path ref, input/output schemas, pagination spec); a recipe book = ordered recipes + threading (output→input) compiled once into deterministic config the engine executes. Security invariants: HTTPS-only, hostname fixed per recipe (runtime inputs fill body/query only), private-address-space rejected. Per-pull response-schema validation; validation failure emits a repair signal.
**Triggers:** "recipe book", "rest source", "long-tail integration", "pull from <some API>", "free API connector", "recipe lane".
**Outputs:** recipe-book config under `factory/recipes/<source>.yaml`, per-recipe isolation test results, evidence pack.
**Composes with:** `r64-conformance` (reduced battery), `r64-connector-factory` (promotion path: recipe book = the spec for a first-party driver — "the workaround is the spec").

### `r64-factory-maintenance` — r64-db-engine repo
The "maintains" half. Weekly conformance re-runs per registered dialect against live sources (systemd timer); recipe-lane per-pull schema validation. Any failure → ntfy + auto-instantiated `REPAIR-BRIEF-<dialect>-<date>.md` from the factory template (re-research docs → re-author → re-admit through the battery → evidence pack).
**Triggers:** "driver drift", "conformance failed", "repair brief", "connector broke", "provider changed".
**Composes with:** `r64-conformance`, `meshcave-ops` (fleet/ntfy facts).

### `partical-plane-factory` — Partical repo *(phase 2 of MESHFORGE rollout)*
Adds a platform plane by the proven recipe: fail-soft `collect_X()` → `crossing()` alert-state file (in `/var/lib/partical-status/`, never the app StateDirectory — separation doctrine) → env overrides → `renderX()` Platform card (badge idiom: `.badge` shell wrapping `tteal/tred/tval`) → tests extracting renderer verbatim from app.html → asset-swap deploy (no caddy restart, `.bak` off the served path, deploy-lib.sh owns chown/validate/reload-vs-restart). Prerequisite before first use: STR-03 topology.env single-source.
**Triggers:** "new plane", "collector block", "Platform card for <X>", "surface <X> in the console".

### `partical-tenant-factory` — Partical repo *(phase 2)*
Codifies provision-tenant.sh as skill law: the script is the carve record (layout changes go in script first, never by hand), export guard before/after block-layer ops, chown fix, indexer-timer decision, Caddy tenant vhost + loopback bind, StateDirectory pattern.
**Triggers:** "new tenant", "provision <name>", "carve an LV", "tenant vhost".

---

## Intake & promotion (not skills — wiring)

- **Intake trigger:** meshroad cockpit Sources tab gains "Request connector" → instantiates `CC-BRIEF-driver-<dialect>.md` from the factory template. (meshroad repo work; separate session.)
- **Promotion path:** recipe-lane usage telemetry identifies which sources earn first-party drivers; the recipe book rides into the driver campaign as the spec.
- **Cross-agent QA gate:** mandatory between conformance-green and merge. Builder ≠ auditor.

## Rollout order (ratified 2026-08-16)
1. r64 lane in full (this session's CC brief). 2. Snowflake = first driver admitted *through* the factory (Phase 3 opener). 3. Sources-tab intake. 4. Partical lane (after STR-03).
