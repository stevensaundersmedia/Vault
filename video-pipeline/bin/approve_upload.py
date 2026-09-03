#!/usr/bin/env python3
"""Record authorisation to upload a master as a PRIVATE YouTube draft.

This is deliberately separate from review approval. Approving an edit says the
cut is right; this says the file may leave the machine.

There is no option here for public, unlisted or scheduled. This pipeline
creates private drafts only.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vpipe.cli import banner, base_parser, main_guard, report
from vpipe.errors import GateRefusal
from vpipe.guards import RunPaths, sha256_file, write_json
from vpipe.runstate import RunState

MASTER_NAME = "master.mp4"


@main_guard
def main() -> int:
    parser = base_parser(__doc__)
    parser.add_argument("--title", required=True)
    parser.add_argument("--description", default="")
    parser.add_argument("--tags", default="", help="Comma-separated.")
    parser.add_argument("--note", default="")
    args = parser.parse_args()

    paths = RunPaths(args.runs_root, args.run_id)
    state = RunState(paths)
    state.require("09_master", "master render")

    master = paths.master / MASTER_NAME
    if not master.exists():
        raise GateRefusal(f"No master render at {master}.")

    banner(f"Authorising private upload [{args.run_id}]")
    payload = {
        "schema": "vpipe.approval/1",
        "kind": "upload",
        "file": str(master.resolve()),
        "sha256": sha256_file(master),
        "privacy": "private",          # the only value this pipeline accepts
        "publish_at": None,            # scheduling is publishing; never set
        "title": args.title,
        "description": args.description,
        "tags": [t.strip() for t in args.tags.split(",") if t.strip()],
        "approved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "approved_by": os.environ.get("USER") or os.environ.get("USERNAME") or "unknown",
        "note": args.note,
    }
    target = write_json(paths.approvals / "upload_approved.json", payload, paths.root)
    state.record("approve_upload", file=str(master), title=args.title)

    report("file", master)
    report("title", args.title)
    report("privacy", "private (fixed)")
    report("approval record", target)
    print(f"""
  A PRIVATE draft upload is now authorised.
      bin/10_upload.py --run-id {args.run_id} --execute

  The video will not be published, scheduled or made visible to anyone.
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
