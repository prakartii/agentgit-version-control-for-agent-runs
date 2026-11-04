# ADR-001: Object format and hashing

Status: accepted

## Context

Runs must be stored so identical content is stored once, and so an object's
id proves its content. Ids must be stable across machines and Python versions.

## Decision

- Every object is hashed as SHA-256 over `b"<type> <length>\0" + payload`,
  the same framing git uses. The type prefix stops a blob and a step with
  identical bytes from sharing an id.
- Structured objects (step, run) are serialized as canonical JSON: keys
  sorted, no whitespace, UTF-8, no NaN/Infinity. The same data always gives
  the same bytes.
- Objects reference each other by hex id. Blobs hold raw payload bytes
  (tool arguments, tool results). Steps point at blobs, runs point at steps
  and parent runs, forming a DAG.
- Refs are the only mutable thing: a name pointing at a run id.

## Alternatives rejected

- Hashing Python `repr` or `pickle`: unstable across versions.
- SHA-1 like git: SHA-256 avoids collision concerns and was required by the
  project goals.
- Hashing JSON without a type prefix: allows cross-type id collisions.

## Consequences

- Floats and key order are normalised by the canonicalizer, so producers
  cannot cause accidental id drift.
- Changing the format later changes every id, so the format is versioned
  by the type prefix and any change needs a new ADR.
