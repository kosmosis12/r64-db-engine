# Operator Guide: Row64 Server Deployment

> **Status:** Draft. Field-validated deployment steps migrating from README.

## Audience

Operators deploying r64-db-engine against a real Row64 Server install (not the `make demo` sandbox).

## Steps

1. Add producer user to the `row64` group
2. Pre-create the target group directory with `setgid` bit
3. Register the database in Row64 Server's `Connections[].DATABASES` config
4. Write the engine config (see `examples/cachyos-demo.yaml`)
5. Validate with `r64-db-engine validate --config <path>`

Full step-by-step content forthcoming.
