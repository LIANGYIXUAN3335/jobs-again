"""ONE-TIME Google Calendar authorization. Run this once, follow the browser prompt, and it
saves a refreshable token to secrets/token_gcal.json so the headless triage can read your
free/busy slots. Read-only calendar scope. Nothing is sent or modified on your calendar.

Prereqs (see GMAIL_授权说明 / the setup checklist):
  1. Google Cloud Console → enable "Google Calendar API".
  2. OAuth consent screen → External → add yourself as a Test user.
  3. Create OAuth client → type "Desktop app" → download the JSON.
  4. Save that JSON as jobscan/secrets/gcal_client.json
Then: jobscan/.venv/bin/python tools/gcal_auth.py
"""
import sys

sys.path.insert(0, ".")
from app import config as cfgmod, gcal  # noqa: E402


def main():
    client = cfgmod.resolve_path(gcal.CLIENT_PATH)
    token = cfgmod.resolve_path(gcal.TOKEN_PATH)
    if not client.exists():
        print(f"✗ Missing {client}.\n"
              f"  Download a 'Desktop app' OAuth client JSON from Google Cloud Console and save it there.\n"
              f"  (Enable Google Calendar API + add yourself as a Test user on the consent screen first.)")
        sys.exit(1)
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("✗ google-auth-oauthlib not installed. Run:\n"
              "  jobscan/.venv/bin/pip install google-api-python-client google-auth-oauthlib")
        sys.exit(1)
    token.parent.mkdir(parents=True, exist_ok=True)
    flow = InstalledAppFlow.from_client_secrets_file(str(client), gcal.SCOPES)
    creds = flow.run_local_server(port=0)          # opens your browser for consent
    token.write_text(creds.to_json())
    print(f"✓ Saved {token}. Calendar is connected — availability auto-replies are now live.")


if __name__ == "__main__":
    main()
