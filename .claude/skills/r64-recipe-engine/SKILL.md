---
name: r64-recipe-engine
description: >
  Author recipe books for the generic `rest` dialect — the long-tail ingestion
  lane of the r64-db-engine factory. A recipe is one call (method, URL PINNED
  at creation, auth as a 0600 env-file path, params/response schemas,
  pagination spec); a recipe book is ordered recipes plus threading
  (output→input) compiled once into deterministic config that a hand-written
  engine executes — no model in the pull path. Security invariants are enforced
  in code and test-proven: HTTPS-only, hostname fixed per recipe with proper
  subdomain matching (evil-checkr.com is not checkr.com), private/loopback/
  link-local address space rejected, response size and time caps. Per-pull
  response-schema validation; a validation failure emits a structured repair
  event plus ntfy and exits non-zero, never an auto-retry-with-reinterpretation.
  Use when connecting a REST/HTTP API without writing a first-party driver.
  Trigger on: recipe book, rest source, rest dialect, long-tail integration,
  pull from <some API>, free API connector, recipe lane, open-meteo, JSONPath
  extract, cursor pagination, link-header pagination, SSRF, destination
  pinning, promotion path. Composes with r64-conformance (reduced battery),
  r64-connector-factory (promotion: the recipe book IS the driver spec), and
  meshforge (the Four Laws).
---

# r64-recipe-engine

Repo: `/home/kos/builds/r64-db-engine`. Implementation: `factory/rest_driver.py`,
registering dialect `rest`. Recipe books live in `factory/recipes/<source>.yaml`.

**The whole point of this lane: the recipe book is DATA and the engine is the
only code.** An agent researches an API once, at build time, and compiles what
it learned into a declarative book. At pull time there is no model, no
inference, and no reinterpretation — just an engine executing a fixed plan
(Law 1). That is what makes a long-tail connector reviewable and reproducible
instead of merely convenient.

Zero core edits, exactly as the driver lane: `rest` is one registry entry.
Prove it — `git grep -rniE "\brest\b" src/r64_db_engine/core/` returns nothing.

---

## Recipe-book schema (the contract)

The `rest:` block of a normal engine config.

```yaml
dialect: rest

rest:
  recipes:
    - name: forecast                    # referenced by threading + bindings
      method: GET
      # URL IS PINNED AT CREATION. Runtime inputs may populate declared body or
      # query parameters ONLY — the template admits NO host or path
      # substitution. This is the destination-pinning invariant: a recipe can
      # never be steered somewhere else by its own inputs.
      url: https://api.open-meteo.com/v1/forecast
      auth:
        type: none                      # none | header | query
        env_file: /etc/r64/secrets/<source>.env   # 0600, read BY THE ENGINE at
        key_name: X-Api-Key                       # call time; never logged,
                                                  # never a config value
      params_schema:                    # declared inputs; anything else refused
        type: object
        properties:
          latitude:  { type: number }
          longitude: { type: number }
        required: [latitude, longitude]
      response_schema:                  # jsonschema — the PER-PULL validator
        type: object
        required: [hourly]
      pagination:
        type: none                      # none | cursor | page | link-header
        # cursor:      cursor_path, cursor_param, max_pages
        # page:        page_param, size_param, page_size, max_pages
        # link-header: rel (default "next"), max_pages, allowed_next_paths
        #
        # allowed_next_paths (link-header ONLY): paths a PROVIDER-SUPPLIED next
        # URL may move to. Default empty = the pinned path only. A provider that
        # genuinely paginates across paths must be declared here at authoring
        # time; absent the declaration the next URL is REFUSED, not followed.
      extract: hourly                   # dotted path / JSONPath to the records

  threading:                            # ordered; output → input bindings
    - recipe: geocode
    - recipe: forecast
      bind:
        latitude:  geocode.results[0].latitude
        longitude: geocode.results[0].longitude

  output:                               # column mapping → Arrow types
    columns:
      - { name: time,        from: time,           type: timestamp[us], tz: UTC }
      - { name: temperature, from: temperature_2m, type: double }

  limits:
    max_response_bytes: 33554432        # sane defaults, configurable
    timeout_s: 30
```

Notes that are contract, not style:

- **`int64`-native.** Never narrow an integer to fit.
- **Timestamps normalized to UTC.** B-2 applies to APIs exactly as it does to
  databases: aggregate parity is blind to a uniform shift, so a boundary
  assertion is mandatory in the battery.
- **`params_schema` is closed.** An input the schema does not declare is
  refused, not passed through.

---

## Engine invariants (each enforced in code, each with a test)

These are security properties, so each one gets a **failing fixture** — a test
that proves the engine REFUSES the malicious shape, not merely that it accepts
the benign one.

1. **HTTPS-only.** An `http://` URL is refused at load, and a recipe mutated
   from https to http must be refused — test it literally.
2. **Hostname fixed per recipe.** For the AUTHORED url, the resolved host must
   exactly match the recipe's recorded allowlist host, or be a **proper
   subdomain** of it. `api.checkr.com` matches `checkr.com`; **`evil-checkr.com`
   does NOT** — test that exact pair, because naive suffix matching passes it.
3. **Private address space rejected.** After resolution: loopback, link-local,
   private ranges, unique-local and unspecified addresses are refused. This is
   the SSRF fence; resolution-time checking is what makes it real rather than
   cosmetic.
3b. **Pagination is confined, default-deny.** For a provider-supplied
   next-URL: **scheme, port, and canonicalized host (case + trailing-dot
   normalized) must match exactly; the candidate URL never reaches the client —
   requests are rebuilt from pinned parts.** The subdomain latitude of rule 2 is
   deliberately unavailable here, because this URL comes from the server, not
   the author. Only the query string is carried over. Cross-path pagination
   requires `allowed_next_paths`. Test the subdomain case explicitly: it is the
   one a reasonable implementation gets wrong by reusing rule 2.
3c. **DNS rebinding closed at response time.** The peer actually connected to
   must be public AND in the set validated moments earlier, asserted
   fail-closed BEFORE any body is read. Residual window: the request has
   already reached the socket, so a rebound peer sees the request and its
   credentials — what is prevented is the response being trusted. State that
   window; do not claim it is closed.
4. **Destination pinning.** Runtime inputs populate declared body/query
   parameters only. No input reaches host or path.
5. **Per-pull `response_schema` validation.** On failure: a structured repair
   event as one JSON line to `factory/evidence/drift/<source>-<ts>.json`, an
   ntfy alert via the fleet's existing `ntfy-fail@` conventions, and a
   **non-zero exit**. **No auto-retry-with-reinterpretation** — that is a Law 1
   violation and would turn a provider change into silent data corruption.

   **No retry of any kind is implemented today.** The engine makes one attempt
   per page and fails. An earlier version of this document said "retry the
   REQUEST on transport failure; never retry the MEANING" — the second half is
   doctrine and holds, but the first half described code that does not exist.
   Bounded transport retry is future work; the claim returns when the code
   does.
6. **Response size and time caps**, configurable, defaulted sanely. An
   unbounded read is a memory bug waiting for a bad day.
7. **Credentials — exactly what is enforced.** Each of these is tested:

   - **Never authored in a recipe schema.** The `auth` block accepts a PATH and
     a key name and nothing else, so a book cannot carry a secret even by
     mistake.
   - **Read at call time from a 0600 path.** A group- or world-readable file is
     refused before any call is made.
   - **Header auth never reaches the query string** (and vice versa).
   - **Scrubbed from engine-raised errors and from drift/repair events.** Every
     secret loaded during a call is removed from message text, and for query
     auth the parameter's value is redacted in any URL that appears in an
     error — by parameter NAME as well as by literal, since a client may
     re-encode the value. Scrubbed re-raises use `from None`, because chaining
     would print the unscrubbed original in the traceback.
   - **Never in evidence packs.** Packs record a secret file's path, size,
     mtime and mode — never a digest of its contents, which for a low-entropy
     key would be offline-guessable.

   **And, plainly: the wire request necessarily carries the credential.** That
   is what authentication is. The guarantee is about where the secret can be
   OBSERVED afterwards — context, logs, errors, repair records, packs — not
   about the request itself.

   *Rationale of record:* drift events and repair briefs are **agent-read**.
   The next agent opens the repair record to fix the connector, so a credential
   landing there is a credential in model context. The scrubber is Law 3
   enforcement, not cosmetics.

   > An earlier version of this clause said secrets are "never in a log line,
   > an error message, a recorded request". Unqualified, that was false on two
   > counts — nothing scrubbed errors at the time, and "recorded request" reads
   > as a claim about the wire. Replaced with the enumerated, tested list above.

---

## Admission — the reduced battery

Recipe books go through `r64-conformance` with the checks that apply:

| check | recipe lane |
|---|---|
| registry admission | yes — `rest` resolves; unregistered refused listing the registry |
| schema exactness | yes — against a spec you write for the source |
| RF-002 discriminator | if a nullable column exists; else `discriminators: []` **with** `discriminators_absent_reason` → SKIPPED-with-reason |
| B-2 boundary | yes — UTC boundary on the time column |
| block structure | yes — 65536 discipline |
| checksum | yes — two same-lane pulls byte-identical |
| aggregate parity | only where a stable ground truth exists; a live API usually has none — say so rather than inventing one |
| PG-011 refusal | yes |
| security invariants | https→http mutation and hostname mutation both refused |

Evidence pack emitted the same as any driver.

---

## Promotion path

Recipe-lane usage telemetry identifies which sources earn a first-party driver.
When one does, **the recipe book rides into the driver campaign as the spec** —
it already records the auth model, the pagination shape, the response schema
and the type mapping, which is most of `DRIVER-PLAN.md` rows 1, 2, 9 filled in
from observed behaviour rather than from documentation. The workaround is the
spec. Hand it to `r64-connector-factory`.
