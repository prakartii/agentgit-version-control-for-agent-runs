"""Portable run bundles: one JSON file holding a run and everything it needs.

A bundle lets a recording be committed to a git repository and replayed in CI
on a machine that has never seen the original object store. It contains the run,
its ancestors (a fork needs its parent), and every step and blob reachable from
them. Object ids are content hashes, so import re-derives each id from the
bytes and rejects a bundle whose declared id does not match: a tampered or
corrupted bundle cannot smuggle in different content under a known id.
"""

import base64
import json
from typing import Any

from agentgit_core.repo import Repository
from agentgit_core.store import ObjectStore

BUNDLE_VERSION = 1


class BundleError(ValueError):
    """The bundle is malformed or fails its integrity check."""


def export_bundle(repo: Repository, run_id: str) -> dict[str, Any]:
    """Collect ``run_id``, its ancestors, and all their steps and blobs."""
    repo.verify_run(run_id)  # refuses to export a run with dangling references
    objects: dict[tuple[str, str], bytes] = {}
    pending = [run_id]
    while pending:
        current = pending.pop()
        if ("run", current) in objects:
            continue
        run = repo.get_run(current)
        objects[("run", current)] = repo.raw("run", current)
        for step_id in run.steps:
            if ("step", step_id) in objects:
                continue
            objects[("step", step_id)] = repo.raw("step", step_id)
            step = repo.get_step(step_id)
            for blob in (step.arguments, step.result, step.events):
                if blob is not None:
                    objects[("blob", blob)] = repo.get_blob(blob)
        pending.extend(run.parents)
    return {
        "bundle_version": BUNDLE_VERSION,
        "run": run_id,
        # Sorted so exporting the same run twice gives byte-identical files.
        "objects": [
            {"type": t, "id": i, "data": base64.b64encode(data).decode("ascii")}
            for (t, i), data in sorted(objects.items())
        ],
    }


def dumps(bundle: dict[str, Any]) -> str:
    return json.dumps(bundle, indent=1, sort_keys=True) + "\n"


def import_bundle(store: ObjectStore, bundle: Any) -> str:
    """Store every object of ``bundle`` and return its run id."""
    if not isinstance(bundle, dict) or bundle.get("bundle_version") != BUNDLE_VERSION:
        raise BundleError(f"not an AgentGit bundle (expected bundle_version {BUNDLE_VERSION})")
    run_id, entries = bundle.get("run"), bundle.get("objects")
    if not isinstance(run_id, str) or not isinstance(entries, list):
        raise BundleError("bundle is missing 'run' or 'objects'")
    seen_run = False
    for entry in entries:
        try:
            kind, declared, data = entry["type"], entry["id"], base64.b64decode(entry["data"])
        except (KeyError, TypeError, ValueError):
            raise BundleError("malformed object entry") from None
        if kind not in ("blob", "step", "run"):
            raise BundleError(f"unknown object type '{kind}'")
        actual = store.put(kind, data)
        if actual != declared:
            raise BundleError(f"{kind} {declared} does not match its content (got {actual})")
        seen_run = seen_run or (kind == "run" and declared == run_id)
    if not seen_run:
        raise BundleError("the bundle does not contain its own run")
    return run_id
