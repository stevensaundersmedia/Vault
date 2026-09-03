"""Run state and the approval gates.

Safety rules 3, 4 and 5 are enforced here. The gates are deliberately built so
that approval attaches to a *specific artefact by checksum*, not to a stage
name: approving one 720p review can never authorise a master of a different,
later render.
"""

from __future__ import annotations

import json
import os
import platform
from datetime import datetime, timezone
from pathlib import Path

from .errors import GateRefusal
from .guards import sha256_file, write_json

REVIEW_APPROVAL = "review_approved.json"
UPLOAD_APPROVAL = "upload_approved.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RunState:
    """A durable record of which stages have run and what they produced."""

    def __init__(self, paths):
        self.paths = paths
        self.data = self._read()

    def _read(self) -> dict:
        if self.paths.state_file.exists():
            return json.loads(self.paths.state_file.read_text(encoding="utf-8"))
        return {
            "schema": "vpipe.runstate/1",
            "run_id": self.paths.run_id,
            "created": _now(),
            "host": platform.node(),
            "stages": {},
        }

    def save(self) -> None:
        write_json(self.paths.state_file, self.data, self.paths.root)

    def record(self, stage: str, **fields) -> None:
        self.data["stages"][stage] = {"completed": _now(), **fields}
        self.save()

    def get(self, stage: str) -> dict | None:
        return self.data["stages"].get(stage)

    def require(self, stage: str, human_name: str) -> dict:
        entry = self.get(stage)
        if not entry:
            raise GateRefusal(
                f"Stage `{stage}` ({human_name}) has not run for this run id. "
                f"Run the stages in order; nothing here back-fills a missing step."
            )
        return entry


def write_review_approval(paths, review_path: Path, note: str = "") -> Path:
    """Record approval of one exact review file.

    Deliberately a separate, explicit action. The pipeline never calls this
    itself: a human runs it after watching the review.
    """
    digest = sha256_file(review_path)
    payload = {
        "schema": "vpipe.approval/1",
        "kind": "review",
        "approved_file": str(review_path.resolve()),
        "sha256": digest,
        "approved_at": _now(),
        "approved_by": os.environ.get("USER") or os.environ.get("USERNAME") or "unknown",
        "note": note,
    }
    target = paths.approvals / REVIEW_APPROVAL
    write_json(target, payload, paths.root)
    return target


def check_review_approval(paths, review_path: Path) -> dict:
    """Gate for the master render (safety rule 3).

    Refuses unless an approval exists whose checksum matches the review file
    on disk right now. If the review has been re-rendered since approval, the
    hashes diverge and the master is refused until the new render is approved.
    """
    approval_file = paths.approvals / REVIEW_APPROVAL
    if not approval_file.exists():
        raise GateRefusal(
            "No review approval on record, so the master will not be rendered.\n"
            f"  expected: {approval_file}\n"
            "  Watch the 720p review, then approve it explicitly with:\n"
            "      bin/approve_review.py --run-id <id>\n"
            "  Nothing renders at source resolution before that."
        )
    approval = json.loads(approval_file.read_text(encoding="utf-8"))

    if not review_path.exists():
        raise GateRefusal(
            f"The approved review file is missing from disk: {review_path}"
        )
    current = sha256_file(review_path)
    if current != approval.get("sha256"):
        raise GateRefusal(
            "The review file on disk is not the file that was approved.\n"
            f"  approved sha256: {approval.get('sha256')}\n"
            f"  on-disk sha256:  {current}\n"
            "  The review has been re-rendered since approval. Watch the new "
            "review and approve it before any master is produced."
        )
    return approval


def check_upload_approval(paths) -> dict:
    """Gate for upload (safety rules 4 and 5).

    A separate approval from the review gate, because approving an edit is not
    the same act as authorising it to leave the machine.
    """
    approval_file = paths.approvals / UPLOAD_APPROVAL
    if not approval_file.exists():
        raise GateRefusal(
            "No upload approval on record. Nothing will be uploaded.\n"
            f"  expected: {approval_file}\n"
            "  Approving the review authorises a master render and nothing more; "
            "upload is a separate, explicit instruction."
        )
    approval = json.loads(approval_file.read_text(encoding="utf-8"))

    # Belt and braces: even a hand-edited approval file cannot authorise
    # anything but a private draft. There is no code path to public.
    requested = str(approval.get("privacy", "private")).lower()
    if requested != "private":
        raise GateRefusal(
            f"The upload approval requests privacy `{requested}`. This pipeline "
            f"only ever creates private drafts and has no publish, schedule or "
            f"visibility-change capability. Refusing."
        )
    if approval.get("publish_at"):
        raise GateRefusal(
            "The upload approval carries a `publish_at` schedule. Scheduling a "
            "video is publishing it later; this pipeline will not do that."
        )
    return approval
