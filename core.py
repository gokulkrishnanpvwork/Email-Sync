"""
Core logic for Heyzine Reader Sync - shared by the Flask backend.
No GUI/web dependencies here; only requests + stdlib + openpyxl.
"""

import csv
import io
import json
import os
import re
import shlex
import sys
from urllib.parse import parse_qsl

import requests

try:
    import openpyxl
except ImportError:
    openpyxl = None


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DROP_HEADERS = {"content-length", "cookie", "host"}


def get_app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = get_app_dir()
CONFIG_PATH = os.path.join(APP_DIR, "heyzine_config.json")
STATE_PATH = os.path.join(APP_DIR, "heyzine_state.json")
SOURCE_PATH = os.path.join(APP_DIR, "heyzine_source.json")
UPLOAD_DIR = os.path.join(APP_DIR, "uploads")


# ---------------------------------------------------------------------------
# curl parsing
# ---------------------------------------------------------------------------

def parse_curl(curl_command: str) -> dict:
    normalized = curl_command.replace("\\\r\n", " ").replace("\\\n", " ")
    tokens = shlex.split(normalized, posix=True)

    url = None
    headers = {}
    cookies = {}
    data_raw = None

    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in ("-H", "--header"):
            i += 1
            if i < len(tokens) and ":" in tokens[i]:
                k, v = tokens[i].split(":", 1)
                headers[k.strip()] = v.strip()
        elif t in ("-b", "--cookie"):
            i += 1
            if i < len(tokens):
                for part in tokens[i].split(";"):
                    if "=" in part:
                        k, v = part.split("=", 1)
                        cookies[k.strip()] = v.strip()
        elif t in ("--data-raw", "--data", "--data-binary", "-d"):
            i += 1
            if i < len(tokens):
                data_raw = tokens[i]
        elif t in ("-X", "--request"):
            i += 1
        elif t.startswith("http://") or t.startswith("https://"):
            url = t
        i += 1

    if not url:
        raise ValueError("Could not find a URL in the curl command.")

    headers = {k: v for k, v in headers.items() if k.lower() not in DROP_HEADERS}

    fields = {}
    if data_raw:
        for k, v in parse_qsl(data_raw, keep_blank_values=True):
            if k.startswith("flipbook[readers]"):
                continue
            fields[k] = v

    return {
        "url": url,
        "headers": headers,
        "cookies": cookies,
        "fields": fields,
        "reader_type_default": "google",
    }


# ---------------------------------------------------------------------------
# email sources
# ---------------------------------------------------------------------------

def extract_emails_from_rows(rows: list, column_name: str = None) -> list:
    if not rows:
        return []
    header_idx = None
    target = (column_name or "email").strip().lower()
    for i, cell in enumerate(rows[0]):
        if cell is not None and str(cell).strip().lower() == target:
            header_idx = i
            break
    data_rows = rows[1:] if header_idx is not None else rows
    col = header_idx if header_idx is not None else 0
    emails = []
    for row in data_rows:
        if col < len(row) and row[col] is not None:
            val = str(row[col]).strip()
            if val and EMAIL_RE.match(val) and val not in emails:
                emails.append(val)
    return emails


def read_emails_from_file(path: str, column_name: str = None) -> list:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        with open(path, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
    elif ext in (".xlsx", ".xlsm"):
        if openpyxl is None:
            raise RuntimeError("openpyxl is required to read Excel files.")
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        rows = [list(r) for r in wb.active.iter_rows(values_only=True)]
    else:
        raise ValueError(f"Unsupported file type: {ext}")
    return extract_emails_from_rows(rows, column_name)


def sheet_url_to_csv_url(url: str) -> str:
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
    if not m:
        raise ValueError("Could not find a Google Sheet ID in that URL.")
    sheet_id = m.group(1)
    gid_match = re.search(r"[?#&]gid=([0-9]+)", url)
    gid = gid_match.group(1) if gid_match else "0"
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"


def read_emails_from_google_sheet(url: str, column_name: str = None) -> list:
    csv_url = sheet_url_to_csv_url(url)
    resp = requests.get(csv_url, timeout=15)
    resp.raise_for_status()
    if resp.text.lstrip().startswith("<!DOCTYPE"):
        raise RuntimeError(
            "That sheet isn't publicly viewable. In Google Sheets: Share -> "
            "General access -> 'Anyone with the link' (Viewer)."
        )
    rows = list(csv.reader(io.StringIO(resp.text)))
    return extract_emails_from_rows(rows, column_name)


def sheet_url_to_id(url: str) -> str:
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
    if not m:
        raise ValueError("Could not find a Google Sheet ID in that URL.")
    return m.group(1)


def read_emails_from_google_sheet_api(
    url: str, api_key: str, column_name: str = None, sheet_range: str = "A:Z"
) -> list:
    """
    Reads emails via the Sheets API v4 with a plain API key (no OAuth).
    Note: an API key does NOT grant access to a private sheet - Google still
    enforces the sheet's own sharing setting. This still requires the sheet
    to be shared as "Anyone with the link" (Viewer); a key only replaces the
    CSV export as the fetch method, it doesn't make a private sheet readable.
    For an actually-private sheet, use a service account instead.
    """
    sheet_id = sheet_url_to_id(url)
    api_url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{sheet_range}"
    resp = requests.get(api_url, params={"key": api_key}, timeout=15)
    if resp.status_code == 403:
        raise RuntimeError(
            "Sheets API returned 403. Either the API key is invalid/restricted, "
            "the Sheets API isn't enabled on that Google Cloud project, or the "
            "sheet isn't shared as 'Anyone with the link' (Viewer)."
        )
    resp.raise_for_status()
    rows = resp.json().get("values", [])
    return extract_emails_from_rows(rows, column_name)


# ---------------------------------------------------------------------------
# payload + send
# ---------------------------------------------------------------------------

def build_payload(config: dict, emails: list) -> dict:
    payload = dict(config.get("fields", {}))
    reader_type = config.get("reader_type_default", "google")
    for i, email in enumerate(emails):
        payload[f"flipbook[readers][{i}][email]"] = email
        payload[f"flipbook[readers][{i}][type]"] = reader_type
        payload[f"flipbook[readers][{i}][password]"] = ""
    payload.setdefault("confirm", "0")
    return payload


def send_update(config: dict, payload: dict) -> requests.Response:
    return requests.post(
        config["url"],
        headers=config.get("headers", {}),
        cookies=config.get("cookies", {}),
        data=payload,
        timeout=30,
    )


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------

def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def load_config() -> dict:
    return load_json(CONFIG_PATH, {"url": "", "headers": {}, "cookies": {}, "fields": {}, "reader_type_default": "google"})


def save_config(config: dict):
    save_json(CONFIG_PATH, config)


def load_state() -> dict:
    return load_json(STATE_PATH, {"last_synced_emails": []})


def save_state(state: dict):
    save_json(STATE_PATH, state)


def load_source() -> dict:
    return load_json(SOURCE_PATH, {"mode": "file", "sheet_url": "", "file_name": "", "file_path": ""})


def save_source(source: dict):
    save_json(SOURCE_PATH, source)
