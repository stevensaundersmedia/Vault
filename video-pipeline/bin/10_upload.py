#!/usr/bin/env python3
"""Stage 10 — private YouTube draft upload. GATED, and never automatic.

Runs only when all of the following hold:
  - a master exists for this run
  - a separate upload approval exists (bin/approve_upload.py)
  - --execute is passed explicitly

The upload is created with privacyStatus=private and no publishAt. There is no
code path in this file that publishes, schedules, unlists, or changes the
visibility of anything. Changing that is not a configuration change; it is a
change to what this tool is.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vpipe.cli import StageContext, banner, base_parser, main_guard, report
from vpipe.errors import GateRefusal, MissingDependency
from vpipe.guards import sha256_file, write_json
from vpipe.runstate import check_upload_approval

STAGE = "10_upload"
MASTER_NAME = "master.mp4"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def build_client(client_secrets: Path, token_path: Path):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise MissingDependency(
            "The YouTube client libraries are not installed. They are only "
            "needed for this stage:\n"
            "  pip install google-api-python-client google-auth-oauthlib\n"
            f"({exc})"
        ) from exc

    credentials = None
    if token_path.exists():
        credentials = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not credentials or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        else:
            if not client_secrets.exists():
                raise MissingDependency(
                    f"No OAuth client secrets at {client_secrets}. Create an "
                    f"OAuth client in Google Cloud Console and download the JSON."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(client_secrets), SCOPES)
            credentials = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(credentials.to_json(), encoding="utf-8")
    return build("youtube", "v3", credentials=credentials)


@main_guard
def main() -> int:
    parser = base_parser(__doc__)
    parser.add_argument("--execute", action="store_true",
                        help="Actually upload. Without it this stage only "
                             "reports what it would do.")
    parser.add_argument("--client-secrets", type=Path,
                        default=Path.home() / ".config" / "vpipe" / "client_secrets.json")
    parser.add_argument("--token", type=Path,
                        default=Path.home() / ".config" / "vpipe" / "youtube_token.json")
    args = parser.parse_args()

    ctx = StageContext(args)
    ctx.state.require("09_master", "master render")

    banner(f"Stage 10 — private draft upload [{args.run_id}]")

    # Safety rules 4 and 5.
    approval = check_upload_approval(ctx.paths)
    master = ctx.paths.master / MASTER_NAME
    if not master.exists():
        raise GateRefusal(f"No master render at {master}.")

    digest = sha256_file(master)
    if approval.get("sha256") and approval["sha256"] != digest:
        raise GateRefusal(
            "The master on disk is not the file that was authorised for "
            f"upload.\n  authorised: {approval['sha256']}\n  on disk:    {digest}"
        )

    report("file", master)
    report("title", approval["title"])
    report("privacy", "private")
    report("scheduled", "no (this pipeline cannot schedule)")
    report("sha256", digest)

    if not args.execute:
        print("""
  Dry run. Nothing was uploaded.
  Re-run with --execute to upload this file as a private draft.
""")
        return 0

    body = {
        "snippet": {
            "title": approval["title"],
            "description": approval.get("description", ""),
            "tags": approval.get("tags", []),
        },
        "status": {
            "privacyStatus": "private",   # fixed; not read from config
            "selfDeclaredMadeForKids": False,
        },
    }

    from googleapiclient.http import MediaFileUpload

    youtube = build_client(args.client_secrets, args.token)
    media = MediaFileUpload(str(master), chunksize=8 * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    print("  uploading...", flush=True)
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"    {int(status.progress() * 100):3d}%", flush=True)

    video_id = response["id"]
    result = {
        "schema": "vpipe.upload/1",
        "video_id": video_id,
        "privacy": response.get("status", {}).get("privacyStatus"),
        "title": approval["title"],
        "file_sha256": digest,
        "url": f"https://studio.youtube.com/video/{video_id}/edit",
    }
    write_json(ctx.paths.out("upload", "upload_result.json"), result, ctx.paths.root)

    ctx.sentinel.verify()
    ctx.state.record(STAGE, video_id=video_id,
                     privacy=result["privacy"], title=approval["title"])

    report("video id", video_id)
    report("privacy", result["privacy"])
    report("studio link", result["url"])
    print("\n  Uploaded as a PRIVATE draft. Not published, not scheduled.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
