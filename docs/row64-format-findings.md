# Row64 `.ramdb` format findings — questions for the Row64 team

**Status: QUESTIONS, NOT VERDICTS.** Everything here is a fidelity property of
the `.ramdb` format as observed through `row64tools==1.0.11` from outside. Each
entry states what was measured, what the engine currently does about it, and
what we are asking. **None of these are bugs filed against row64tools, and none
should be "fixed" in row64tools by this project.**

Bucket-A convention: we report what the format does, we accommodate it
explicitly at our own boundary, and we ask whether the behaviour is intended and
whether a supported convention exists.

---

## RF-001 — int64 values above 2^31 do not survive the codec

**Measured.** A `BIGINT` of `3548933426` written via
`row64tools.ramdb.save_from_df` and read back with `load_to_df` returns
`-746033870` — a signed-int32 truncation. Reproduced from a real PostgreSQL
`BIGINT` column and from a hand-built DataFrame. The same happens for `INTERVAL`
converted to microseconds.

**What the engine does.** `core/ramdb_writer._raise_on_codec_unsafe_int64`
refuses the write with `Row64CodecOverflowError` rather than emitting a
corrupted file. Loud failure beats silent corruption, but it also means these
values cannot be delivered to Row64 at all through this path.

**Questions.**

1. Is the int32 lane an intentional format constraint, or a codec defect?
2. If intentional — is there a supported convention for wide integers
   (documented split-column, string encoding, decimal lane)?
3. If a defect — is a fix planned, and on what version boundary? The engine
   pins `row64tools==1.0.11` and would need to move deliberately.

**Cross-reference.** PG-001 in `REVIEW-postgres-2026-05-27.md`; Gate A WIDTH
class in `docs/conformance/gate-a-proof.md`.

---

## RF-002 — NULL is indistinguishable from a legitimate zero

**Measured.** `.ramdb` has no null representation for integer, string, or
boolean columns. A SQL `NULL` in a `BIGINT` must be resolved to a value before
`save_from_df`, and the engine's long-standing choice was `0`. Verified with a
real PostgreSQL row `(2, NULL, NULL, NULL)`: the integer column arrives in Row64
as `0`, the text column as `""`.

**Why this matters, concretely.** In Row64, a `NULL` order quantity and a real
quantity of `0` are now the same value. Any `COUNT`, `AVG`, or "missing data"
report computed over that column is wrong in a way nothing downstream can
detect — the information is gone before it reaches the file. `AVG` is the sharp
case: `NULL` should be excluded from the denominator, `0` should be included,
and the two answers differ.

This is the same **class** as RF-001 — one format's representational limit
silently rewriting values — but it is arguably worse to detect, because `0` is a
plausible value where `-746033870` is obviously wrong.

**What the engine does now.** As of the null-fidelity change, the fill happens
**only** at the ramdb boundary
(`core/ramdb_writer.apply_ramdb_null_fill`), not in the source-agnostic coercion
layer. `.ramdb` bytes are unchanged — asserted byte-for-byte in
`tests/core/test_ramdb_golden.py` — so Row64 sees exactly what it always saw.
Sinks whose format carries nulls (the Arrow IPC sink) now preserve them, and
`float`/`datetime` columns were never affected because `.ramdb` represents NaN
and NaT.

**Questions.**

1. Is null-erasure for int/string/bool an intentional format constraint?
2. Is there a **sentinel convention** Row64 already recognises — a reserved
   value, a companion null-mask column, a per-column "missing" marker — that we
   should be emitting instead of `0` / `""`?
3. Does any Row64-side aggregate (`AVG`, `COUNT`, dashboard "missing data"
   panels) attempt to distinguish missing from zero today? If so, on what
   signal?
4. If a null representation is on the roadmap, is there a target version we
   should write against?

**What we are NOT proposing.** We are not asking for a change to row64tools, and
we have not attempted one. The engine accommodates the format as it stands; the
question is whether a better-supported accommodation already exists that we
should be using.

---

## RF-003 — codec is ASCII-only (previously recorded)

Carried forward from `CHANGELOG.md` "Known Limitations" so the format findings
live in one place: ~50 hardcoded `encode('ascii')` call sites in `bytestream.py`
and `ramdb.py` mean non-ASCII text crashes the codec. The engine defaults
`ascii_sanitize: true`, replacing non-ASCII characters with `?` — lossy, and
applied to every string column.

**Question.** Is UTF-8 support on the roadmap, and is there a version boundary
we should target? The Arrow IPC sink carries UTF-8 natively and needs no
sanitization, so this loss is now specific to the `.ramdb` path.

---

## How these were found

RF-001 came out of the 2026-05-27 adversarial review. RF-002 was surfaced by the
Postgres → Arrow end-to-end suite: the same `(2, NULL, NULL, NULL)` row that
round-trips correctly into Arrow was landing as `(2, 0, "", null)` in the
`.ramdb` path, which made the format's constraint visible by contrast. RF-003
predates both.

The pattern is consistent enough to state: **wherever a second output format was
added, a `.ramdb` representational limit that had been invisible became
measurable.** That is the main practical argument for keeping format policy at
sink boundaries rather than in shared code.
