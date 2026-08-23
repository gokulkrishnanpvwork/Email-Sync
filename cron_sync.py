#!/usr/bin/env python3
"""
cron_sync.py - headless, non-interactive runner for Heyzine Reader Sync.

Reads its config entirely from environment variables (no browser, no saved
JSON files) so it can run unattended from a GitHub Actions schedule or any
other cron. Heyzine's save-basic endpoint replaces the *entire* reader list
on every call, so this always re-sends the full current list from the sheet
- there is no "only send new emails" state to track, and no dedupe file to
persist between runs.

Required environment variables:
  HEYZINE_CURL      - a curl command copied from the browser's dev tools for
                       the Heyzine "save readers" request (gives the URL,
                       auth cookies, and the non-reader form fields). This
                       is the piece that goes stale when the session
                       cookie expires - if a run fails with a 401/403 or an
                       HTML login page in the response body, re-capture the
                       curl command and update the secret.
  GOOGLE_API_KEY    - Google Cloud API key with the Sheets API enabled.
                       Note: this does NOT make the sheet private - Google
                       still enforces the sheet's own sharing setting, so it
                       must stay shared as "Anyone with the link" (Viewer)
                       either way. A key only replaces the CSV export as the
                       fetch method; for an actually-private sheet you need
                       a service account instead.

Optional:
  SHEET_COLUMN      - header name of the email column (default: "email").

Exit code is 0 only if emails were found and Heyzine responded 2xx.
"""

import os
import sys

import core

GOOGLE_SHEET_URL = "https://docs.google.com/spreadsheets/d/12M1HpTQNTFOlkYNu_8xQOL2jqNT2yYHhnKsi709t2KI/edit?usp=sharing"


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    curl_text = os.environ.get("HEYZINE_CURL", "").strip()
    api_key = os.environ.get("GOOGLE_API_KEY", "").strip()
    sheet_column = os.environ.get("SHEET_COLUMN") or None

    if not curl_text:
        fail("HEYZINE_CURL environment variable is not set.")
    if not api_key:
        fail("GOOGLE_API_KEY environment variable is not set.")

    try:
        config = core.parse_curl(curl_text)
    except Exception as e:
        fail(f"Could not parse HEYZINE_CURL: {e}")

    try:
        emails = core.read_emails_from_google_sheet_api(GOOGLE_SHEET_URL, api_key, sheet_column)
    except Exception as e:
        fail(f"Could not read the Google Sheet: {e}")

    if not emails:
        fail("No valid email addresses found in the sheet.")

    print(f"Found {len(emails)} email(s) in the sheet.")

    payload = core.build_payload(config, emails)
    try:
        resp = core.send_update(config, payload)
    except Exception as e:
        fail(f"Request to Heyzine failed: {e}")

    print(f"Heyzine responded: {resp.status_code}")
    body_snippet = resp.text[:500]
    print(f"Response body (first 500 chars): {body_snippet}")

    if resp.text.lstrip().startswith("<!DOCTYPE") or "login" in resp.text[:2000].lower():
        fail("Response looks like a login page - the session cookie in HEYZINE_CURL has probably expired.")

    if not (200 <= resp.status_code < 300):
        fail(f"Heyzine returned a non-2xx status: {resp.status_code}")

    print("Sync OK.")


if __name__ == "__main__":
    main()
