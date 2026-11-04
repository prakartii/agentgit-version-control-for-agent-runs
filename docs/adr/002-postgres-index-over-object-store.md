# ADR-002: Postgres as an index, object store as source of truth

Status: accepted

## Context

Commands like `log` need to list runs, filter by metadata and walk parent
links quickly. Scanning an object store for that is slow.

## Decision

Objects (blobs, steps, runs) live in the content-addressed store and are the
source of truth. Postgres holds a derived index: one row per run and step,
the parent edges, and the refs. Refs live only in Postgres because they are
mutable and need atomic updates. The index can be rebuilt from the store.

Until an S3-compatible store is chosen (the MinIO images are no longer
published), the filesystem backend is the default blob store.

## Alternatives rejected

- Store everything in Postgres: simple, but large payloads bloat the
  database and lose the git-like object model.
- Index in SQLite: fewer moving parts, but the job queue in a later phase
  needs `FOR UPDATE SKIP LOCKED`, which is Postgres.

## Consequences

- Index and store can drift if a write fails between them. Writes go to the
  store first, then the index, so the worst case is an unindexed object,
  which is harmless and fixed by a re-index.
