# Round-2 audit — feat/connector-descriptor

## Verdict: **BLOCK** (2 findings confirmed)

Branch under audit: `feat/connector-descriptor` @ `08240fa`.
Reproducers on `audit/connector-descriptor-round2`: `d1591c6`, `10f88d3`.
Suite with reproducers applied: **911 passed / 66 skipped / 2 xfailed**.

---

## Provenance — read this before weighing the findings

This round has a weaker auditor lineage than round 1, and the weakness is
structural rather than incidental. Stated plainly so it can be discounted
correctly:

- **P3 (import-aware AST firewall walk): PASS, observed by Codex.** The walk's
  failing branch fired RED with correct file and line against an injected
  module-scope `from r64_db_engine.drivers import X`; the concrete-driver arm
  correctly stayed green. The scratch probe was reverted. Not re-run here.
- **Codex was then hard-blocked** by its provider's content filter — every
  model turn suppressed — mid-round. It had stated that P1 and P2 met the BLOCK
  bar, but **its observed mechanisms are lost** and it committed no
  reproducers. `audit/connector-descriptor-round2` existed at `08240fa` with
  zero commits of its own.
- **P1 and P2 below were specified by a third agent (Claude) and executed by
  Claude Code on the host.** Auditor and fixer are separated in the sense that
  the probe contracts were authored before execution and were not weakened to
  produce hits — but the auditor-of-record is a spec-plus-execution pair, **not
  an independent second model**. Codex's prior BLOCK claim was treated as a
  hypothesis to be tested, not as a result to be confirmed.

Both findings below reproduce from a clean checkout and stand on their own
evidence regardless of what Codex saw.

---

## P1 — provider-shaped content survives emit-path exceptions

**Status: CONFIRMED.** Commit `d1591c6`. Under test: `c43bfaf`.

### Root cause

`c43bfaf` claims "ONE boundary over the entire post-descriptor-load emit path."
The boundary is the last line of `generate()`:

```python
scrubber = emit_scrubber(metas)
return {path: scrubber.scrub(text) for path, text in out.items()}
```

That wraps the **return value**. It does not wrap the function. Every `raise`
above it — `_assert_no_values`, the dialect-identity check, the empty-registry
refusal, `_last_green` — is outside the boundary, and those refusals
interpolate descriptor content verbatim.

The sharpest instance is `_assert_no_values` itself
(`factory/generate_descriptor_artifacts.py:346`):

```python
raise GeneratorError(
    f"descriptor '{meta.dialect}' required_env_keys entry {key!r} is not a bare "
    f"env-var NAME. Refusing to emit — these artifacts are committed and served, "
    f"and a value here is a credential in public (Law 3)"
)
```

`key` is the value that made the guard fire. When that guard catches a
credential sitting in a name slot — the one situation it exists for — it quotes
the credential into its own refusal. `main()` then prints it to stderr
unscrubbed:

```python
except GeneratorError as exc:
    print(f"generator refused: {exc}", file=sys.stderr)
```

The boundary holds for the path that succeeds. The path that fails has no
boundary at all.

### Observed output (arm a) — verbatim

Probe: descriptor with `required_env_keys = ("postgresql://svc_user:FAKEPASS-8b21d7f4@db.internal:5432/appdb",)`,
driven through `gen.generate(tmp_path)`, exception rendered with
`traceback.format_exception(..., chain=True)`.

```
factory.generate_descriptor_artifacts.GeneratorError: descriptor 'postgres'
required_env_keys entry
'postgresql://svc_user:FAKEPASS-8b21d7f4@db.internal:5432/appdb' is not a bare
env-var NAME. Refusing to emit — these artifacts are committed and served, and
a value here is a credential in public (Law 3)

URL present in rendered chain : True
TOKEN present in rendered chain: False
```

Under pytest, without the xfail marker:

```
E  AssertionError: connection URL survived the emit path exception
E  assert 'postgresql:...l:5432/appdb' not in 'Traceback (...ic (Law 3)\n'
```

### Mechanism discrepancy — the spec's hypothesis was WRONG

The probe specification named `raise ... from exc` re-chaining as "the known
weak edge," on the theory that the original unscrubbed exception rides along in
the traceback chain. **That route was tested and does not leak.** An unreadable
`last-green` pack whose bytes carried both synthetic values produced:

```
json.decoder.JSONDecodeError: Expecting property name enclosed in double quotes:
line 1 column 133 (char 132)
The above exception was the direct cause of the following exception:
...
URL   in chain: False
TOKEN in chain: False
```

`JSONDecodeError.__str__` reports position only, never document content. The
`from exc` chaining in `_last_green` is clean. The real mechanism is simpler and
broader than the hypothesised one: **the boundary wraps a return value rather
than a scope.** The fix therefore is not `from None` re-raise discipline — it is
extending the boundary to cover the whole of `generate()`.

### Arm (b) — scrubber-internal failure: observed CLEAN

Recorded rather than dropped. An exception raised inside `emit_scrubber` (prose
whose `.strip()` fails, reached at
`factory/generate_descriptor_artifacts.py:438`) did **not** carry descriptor
content out; Python tracebacks do not render locals, and the injected message
deliberately quoted no synthetic value. An earlier attempt that *did* quote the
token into its own injected exception was discarded as circular — it would have
proved only that the probe put it there.

Arm (b) is asserted in the committed reproducer alongside arm (a) so it stays
observed; it is arm (a) that is red.

---

## P2 — green can be authored by hand, and survives tampering

**Status: CONFIRMED, both arms.** Commit `10f88d3`. Under test: `460ee1a`.

### Root cause

`_unauthenticated()` re-derives the verdict from the pack's own `checks` and
requires the pack's claims to agree with that re-derivation. Its docstring names
the trap it intends to avoid:

> Not a richer SHAPE — a richer shape is the same mistake with more fields.

It is a richer shape. **Every input to the agreement is a field of the same
writable file**, so the pack is authenticated against itself:

| What is checked | What it actually proves |
|---|---|
| `checks` is a list of records with `status` | the writer typed a list |
| re-derived verdict == recorded verdict | the writer was self-consistent |
| re-derived tally == recorded tally | the writer counted their own list |
| a `checksum` check with `status == "PASS"` | the writer typed `"PASS"` beside the string `"checksum"` — not a checksum |
| `sha256_pull1 == sha256_pull2`, both well-formed | the writer typed the same 64 hex chars twice |

Nothing is bound to bytes that were ever pulled, to a key, or to a run. There is
no oracle-side mark of any kind — no signature, no MAC, no digest over the pack.
The commit's claim, "A pack that satisfies all of that had a battery run behind
it. A file somebody wrote does not, and cannot be made to without running one,"
is false as written.

### Observed output — arm 1 (forgery)

A pack for `forgedsql`, a dialect that has never existed, with pull digests set
to `sha256(b"nothing was ever pulled")`:

```
unauthenticated reason: None
state : passing
label : conformance-passing
PASSING? True
evidence: {"verdict": "PASS", "generated_utc": "2026-08-26T09:00:00Z",
           "table": "public.orders", "tally": {"PASS": 9, "FAIL": 0, "SKIPPED": 1},
           "ratifies_head": true, "commit": "ffff...ffff"}
```

### Observed output — arm 2 (tampering)

The **real** `factory/evidence/last-green/EVIDENCE-clickhouse.json`, tampered
post-production — digests re-pointed at `sha256(b"different bytes entirely")`,
table renamed, commit zeroed, timestamp moved to 2099 — with no mark left
untouched because there is no mark:

```
untampered state: passing
tampered  state: passing -> PASSING? True
tampered evidence rendered: {"verdict": "PASS",
  "generated_utc": "2099-01-01T00:00:00Z",
  "table": "public.attacker_controlled", ...,
  "commit": "0000000000000000000000000000000000000000"}
```

The tampered values are rendered verbatim onto the cockpit evidence card.

Arm 2 is the load-bearing one. Arm 1 can be waved off as requiring an author who
knows the shape. Arm 2 requires only write access to a file the gate already
trusts, and leaves nothing behind that the gate could detect.

### Corroboration from the branch's own test suite

`tests/factory/test_gate_mf_desc.py::test_9_fixture_is_red`, as strengthened by
`460ee1a`, hand-writes an oracle-shaped pack and asserts it produces
`gen.PASSING`. The fix pass's own fixture is a working forgery. It is the
finding, committed as a passing test.

---

## FORWARD-FILED — new finding class, not fixed in this round

**A live environment value silently corrupts committed artifacts.**

`emit_scrubber` registers *every* value in `os.environ` as a secret to subtract.
Any env var holding a common string of ≥ 8 characters (`MIN_SCRUBBABLE_LENGTH`)
that legitimately appears in an artifact is replaced with `«redacted»`. The
string `postgres` is exactly 8 characters and is the value of `config_profile`.

`PGDATABASE=postgres` is a stock libpq default and is set on many developer and
CI hosts. Observed on this host:

```
$ factory/generate_descriptor_artifacts.py --check                # clean env
6 generated artifact(s) match the descriptors.                    exit=0

$ PGDATABASE=postgres factory/generate_descriptor_artifacts.py --check
STALE docs/connectors/README.md
STALE docs/connectors/postgres.md
STALE factory/artifacts/connector-roster.json
STALE factory/artifacts/factory-status.json                       exit=1
```

Diff of the corruption:

```
-      "config_profile": "postgres",
+      "config_profile": "«redacted»",
```

This is a distinct class from P1 and P2 — over-scrubbing rather than
under-scrubbing — and it breaks Law 1 in a way the commit message explicitly
believed it had preserved ("the clean case is a byte-for-byte no-op"). It is
a no-op only for environments that happen not to collide. Left untouched here
per the audit's scope; it needs an operator scope call, because the plausible
fixes (raise the floor, register only values of vars named in
`required_env_keys`, scrub by key rather than by value) each change what the
boundary is for.

---

## Disposition

Both findings are edge-completions of guards the branch already carries, not
requests for new abstractions. P1 needs the boundary to wrap a scope instead of
a return value. P2 needs the verdict to be bound to something the pack's writer
does not control.

Proceeding to remediation on `feat/connector-descriptor` against the contract:
final suite must be **913 passed / 66 skipped / 0 xfailed**, with the 911
unchanged.

---

# Round-2 close-out

Remediation ran on `feat/connector-descriptor` from `08240fa`, with both
reproducers cherry-picked (`f924333`, `f836bbb`).

```
913 passed, 66 skipped, 0 xfailed
```

**Read that 913 correctly.** It is 911 baseline + one fixed guard (P1) + **one
test that DOCUMENTS A LIMIT (P2), not a second fixed guard.** P2 was confirmed
reproducible and then ruled a declared limit rather than a defect; its test
asserts the forged and tampered packs ARE accepted. Counting it as a closed
finding would be the proxy pattern this whole effort exists to end.

The 911 is intact — no previously-passing test was modified, loosened, or
skipped — and `--check` still reports all 6 generated artifacts byte-identical.

## P1 — CONFIRMED, FIXED

**Commit:** `f3fdbac`.

**Root cause.** "One outer boundary over the whole emit path" was implemented as
a boundary over `generate()`'s return value, so the path that raises had no
boundary at all and the refusals quoted descriptor content verbatim.

**Mechanism of the fix.** The boundary now wraps the emit path's *scope*
(`_scrubbed_failures`), scrubbing the exception on the way out and re-raising
with `from None` so the chain cannot carry the unscrubbed original underneath.
`emit_scrubber` moved ahead of the first guard and gained a third registration —
an env key that is not a NAME — so the values the guards refuse over are
unspeakable before a guard composes a message quoting one. `_not_a_name()` is
now one predicate shared by the guard that refuses and the boundary that
redacts, so the two cannot drift.

The boundary is invisible unless it redacts: where scrubbing changes nothing the
original exception is re-raised untouched, same type and traceback. That is why
the 911 did not move.

**Failing branch demonstrable:** backing the fix out returns the reproducer to
red (verified by stashing the fix).

## P2 — CONFIRMED REPRODUCIBLE, RULED A DECLARED LIMIT

**Operator decision (2026-08-26): the declared limit stands. Evidence packs are
attestation, not authentication.**

The reproduction is not disputed and is recorded in full above: a hand-authored
pack for `forgedsql` renders green, and a tampered real pack renders green with
its tampered fields on the cockpit card. What changed is the ruling on what that
means.

**The doctrine.** Authentication's trust anchor is **operator merge provenance**
— a human merging from a fresh shell. It is not a mark inside the repo, because
any key the sweep can sign with is writable by the same process that writes the
packs; signing would add ceremony without separation. That is the identity
dominance test, decided the same way as PARTFORGE v0. The limit is already
declared in `factory/evidence.py` under "concurrent local mutation of the store
or of the pack itself": packs *"attest generation-time state; they are unsigned
and do not defend against… mutation of the pack itself."*

**So the correction is owed to the overclaim, not to the guard.** `460ee1a`'s
commit message said:

> "A pack that satisfies all of that had a battery run behind it. A file
> somebody wrote does not, and cannot be made to without running one."

That is false and **the claim is withdrawn**. The guard itself is sound and
stays: it closed round 1's real hole — a bare `{"verdict": "PASS"}` no longer
creates green — and it raises a forgery's cost from one key to the oracle's
whole output shape, which is the difference between an accident and an act. It
simply never delivered unforgeability, and no longer says it does.

**Where the correction was applied.** The pushed commit message is immutable
history; this report is the record of correction. Beyond it:

| Location | Change |
|---|---|
| `factory/generate_descriptor_artifacts.py` — `_unauthenticated()` docstring | the overclaim quoted and explicitly withdrawn; replaced with what the guard does establish (a floor) and does not (unforgeability) |
| `docs/conformance/gate-mf-desc.md` — check 9 | "with evidence" defined precisely; attestation-vs-authentication stated with the trust anchor |
| `tests/audit/test_connector_descriptor_round2.py` | reproducer converted to `test_packs_are_attestation_not_authentication` |

**The test is positive, deliberately.** It asserts the forged and tampered packs
ARE accepted, and it carries the limit, the declaring line, and the compensating
control in its docstring. It is not `xfail(strict=False)`, because a silent
expected-failure is exactly how a declared limit rots back into an assumed
guarantee. It also re-asserts that the round-1 hole stays closed, so the floor
is guarded rather than merely described.

**If that test ever goes RED, that is the desired signal** — it means somebody
added real authentication to the evidence path. The instruction, carried in the
test itself: verify the new mechanism, then update the test docstring, the
limits table in `factory/evidence.py`, and this report. Do not "fix" the test.

## FORWARD-FILED, untouched

**A live environment value silently corrupts committed artifacts** —
`PGDATABASE=postgres` makes `--check` fail with 4 STALE artifacts and rewrites
`"config_profile": "postgres"` to `"config_profile": "«redacted»"`. Full
evidence in the section above. Distinct class from P1 and P2 (over-scrubbing,
not under-scrubbing), untouched by decision, and it needs its own scope call
because the plausible fixes each change what the boundary is for.

## Branch state

- `audit/connector-descriptor-round2` — `b20e005`: the two reproducers as
  found, plus this report's Phase A sections. Preserved as the audit record.
- `feat/connector-descriptor` — reproducers, the P1 fix, the P2 limit test, the
  overclaim correction, this report. Green at 913/66/0. **Not merged** — merge
  is the operator's, from a fresh shell, which is precisely the trust anchor
  P2's ruling rests on.
