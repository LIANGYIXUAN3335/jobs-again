"""ONE-TIME Google Sheets authorization for partner application sync.

Prereqs:
  1. Google Cloud Console -> enable "Google Sheets API".
  2. OAuth consent screen -> External -> add yourself as a Test user.
  3. Create OAuth client -> type "Desktop app" -> download the JSON.
  4. Save that JSON as /path/to/.config/jobscan-secrets/gsheets_client.json

Then:
  jobscan/.venv/bin/python tools/gsheets_auth.py
"""
from __future__ import annotations

import sys
from pathlib import Path


SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SECRET_DIR = Path("/path/to/.config/jobscan-secrets")
CLIENT_PATH = SECRET_DIR / "gsheets_client.json"
TOKEN_PATH = SECRET_DIR / "token_gsheets.json"


def main() -> None:
    if not CLIENT_PATH.exists():
        print(
            f"✗ Missing {CLIENT_PATH}.\n"
            "  Download a Desktop app OAuth client JSON from Google Cloud Console and save it there.\n"
            "  Enable Google Sheets API and add yourself as a Test user first."
        )
        sys.exit(1)
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print(
            "✗ google-auth-oauthlib not installed. Run:\n"
            "  jobscan/.venv/bin/pip install google-api-python-client google-auth-oauthlib"
        )
        sys.exit(1)

    SECRET_DIR.mkdir(parents=True, exist_ok=True)
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_PATH), SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN_PATH.write_text(creds.to_json())
    print(f"✓ Saved {TOKEN_PATH}. Google Sheets sync is authorized.")


if __name__ == "__main__":
    main()
