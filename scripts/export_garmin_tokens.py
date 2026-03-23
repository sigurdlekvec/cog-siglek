"""
Export Garmin / Garth session as one line for GARMIN_TOKENS (Cognite Function env).

Uses **garth** login + `client.dumps()` — not `garminconnect.Garmin().login()`.
Reason: `Garmin.login()` can fail after SSO with
`OAuth1 token is required for OAuth2 refresh` when it fetches profile (known issue;
see python-garminconnect#312). Raw garth login sets OAuth1+2 on the client without
that extra step.

Run locally after: poetry install
  poetry run python scripts/export_garmin_tokens.py

Or set GARMIN_EMAIL and GARMIN_PASSWORD in the environment (e.g. from .env).

Alternative CLI (standalone garth):  uvx garth login
Then paste the printed token line similarly.

Paste the printed line into your .env as:
  GARMIN_TOKENS=<paste entire single line>

Then: cdf build --env=dev && cdf deploy --env=dev

Tokens expire eventually; re-export when the function fails auth.
"""
from __future__ import annotations

import getpass
import os
import sys


def main() -> None:
    try:
        import garth
    except ImportError:
        print("Install deps: poetry install", file=sys.stderr)
        raise SystemExit(1)

    email = (os.environ.get("GARMIN_EMAIL") or "").strip()
    password = os.environ.get("GARMIN_PASSWORD") or ""
    if not email:
        email = input("Garmin email: ").strip()
    if not password:
        password = getpass.getpass("Garmin password: ")

    # MFA: default prompts "MFA code: " when Garmin requires 2FA
    try:
        garth.login(email, password)
    except Exception as e:
        print(f"Login failed: {e}", file=sys.stderr)
        print(
            "\nTips: confirm email/password; if Garmin emailed a code, enter it when "
            "prompted for MFA. Or try:  uvx garth login",
            file=sys.stderr,
        )
        raise SystemExit(1)

    blob = garth.client.dumps()
    if not blob or len(blob) < 64:
        print("Token export failed (empty blob).", file=sys.stderr)
        raise SystemExit(1)

    print("\n--- Add this to .env (single line) ---\n")
    print(f"GARMIN_TOKENS={blob}\n")
    print("--- Then redeploy the Toolkit so the function picks up the env var ---\n")


if __name__ == "__main__":
    main()
