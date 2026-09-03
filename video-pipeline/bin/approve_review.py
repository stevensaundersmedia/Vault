#!/usr/bin/env python3
"""Record approval of one exact 720p review file.

Run this only after watching the review in full. Approval attaches to the
file's SHA-256, so it authorises a master of that edit and nothing else: if the
review is re-rendered, the approval no longer matches and the master is
refused until you approve the new file.

Approving a review does NOT authorise an upload. That is a separate act.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vpipe.cli import banner, base_parser, main_guard, report
from vpipe.errors import GateRefusal
from vpipe.guards import RunPaths
from vpipe.runstate import RunState, write_review_approval

REVIEW_NAME = "review_720p.mp4"


@main_guard
def main() -> int:
    parser = base_parser(__doc__)
    parser.add_argument("--note", default="", help="Why you are approving.")
    parser.add_argument("--accept-qa-failure", action="store_true",
                        help="Approve despite a failing QA verdict. Requires a "
                             "note explaining the decision.")
    args = parser.parse_args()

    paths = RunPaths(args.runs_root, args.run_id)
    state = RunState(paths)
    state.require("07_qa", "QA on the review")

    review = paths.review / REVIEW_NAME
    if not review.exists():
        raise GateRefusal(f"No review render at {review}.")

    qa = state.get("07_qa") or {}
    if qa.get("verdict") == "FAIL" and not args.accept_qa_failure:
        raise GateRefusal(
            "The QA verdict for this review is FAIL:\n"
            + "".join(f"  - {name}\n" for name in qa.get("blocking", []))
            + f"  See {paths.qa / 'qa_report.md'}\n"
            "Fix the run, or re-approve with --accept-qa-failure and a --note "
            "recording why the failure is acceptable."
        )
    if args.accept_qa_failure and not args.note.strip():
        raise GateRefusal(
            "--accept-qa-failure requires a --note explaining the decision."
        )

    banner(f"Approving review [{args.run_id}]")
    approval = write_review_approval(paths, review, args.note)
    state.record("approve_review", file=str(review), note=args.note,
                 accepted_qa_failure=args.accept_qa_failure)

    report("approved file", review)
    report("approval record", approval)
    print(f"""
  A master render at source resolution is now authorised FOR THIS FILE ONLY.
      bin/09_master.py --run-id {args.run_id}

  Upload remains unauthorised and requires a separate instruction.
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
