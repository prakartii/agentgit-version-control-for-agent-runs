# ADR-012: Deployment scope: containers yes, cloud infrastructure not yet

Status: accepted

## Decision

- Ship **Docker images and a compose file** for the database, API and UI. CI
  builds them and smoke-tests `/health` and the UI on every push, so they are
  known to work, not just written.
- **Do not add Terraform yet.** The object store is a local filesystem
  directory. Terraform for a bucket, database and container service would
  provision things the code cannot use, and would only look like a deployment.
  The order is: an S3-backed `ObjectStore` first (the interface already exists,
  and the original plan names MinIO/S3), then infrastructure that matches it.
- Creating cloud resources costs money and is hard to undo, so it is left to the
  owner of the account.

## Consequences

- The API container keeps objects in a volume at `/data`. Runs recorded by the
  CLI on a host are visible in the UI only if that directory is mounted
  (`AGENTGIT_DATA`).
- The API has no authentication. It is read-only and meant for a trusted network
  or localhost; put it behind an authenticating proxy before exposing it.
