# Troubleshooting

> **Status:** Draft. Full troubleshooting matrix migrating from README.

## Common issues

| Symptom | Cause | Fix |
|---|---|---|
| `.ramdb` file written but Row64 Server doesn't serve it | Database not registered in `Connections[].DATABASES` | Add to `config.json`, restart `row64server.service` |
| `'ascii' codec can't encode character` | `ascii_sanitize: false` + non-ASCII source data | Set `ascii_sanitize: true` (lossy) or wait for UTF-8 codec support |
| em-dashes appear as `?` in served data | `ascii_sanitize: true` default; codec is ASCII-only | Known limitation — engine-side workaround active |
| `dev_postgres.sh` "kills shell" error | Old bug, fixed | Use `source scripts/dev_postgres.sh env` to export vars safely |
| Postgres container won't start | Stale container | `docker rm -f r64-db-engine-pg`, retry |

Full troubleshooting matrix forthcoming.
