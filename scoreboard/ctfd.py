import os
import json
import re
from pathlib import Path
from urllib.parse import urljoin

import requests
from dotenv import load_dotenv

load_dotenv()

base = os.getenv("CTFD_BASE_URL")
out = os.getenv("CTFD_OUT")
if not base or not out:
    raise SystemExit("Missing .env: requires CTFD_BASE_URL and CTFD_OUT (see .env.example)")
BASE: str = base.rstrip("/") + "/"
OUT: Path = Path(out)

# Common endpoints to try
CANDIDATES = [
    "/api/v1/scoreboard",
    "/api/v1/scoreboard?count=1000",
    "/scoreboard?format=json",
    "/scores?format=json",
]

def looks_like_ctfd_scoreboard(obj) -> bool:
    """
    Heuristic to detect CTFd scoreboard JSON.
    Usually: {"success": true, "data": [...]}
    Each entry often has name/score/(pos|place|rank) ...
    """
    if not isinstance(obj, dict):
        return False
    if "data" not in obj:
        return False
    data = obj["data"]
    if not isinstance(data, list) or not data:
        return False
    if not isinstance(data[0], dict):
        return False

    keys = set(data[0].keys())
    # name + score is the most common combo
    return ("name" in keys or "team" in keys) and ("score" in keys or "points" in keys)

def main():
    s = requests.Session()
    s.headers.update({"User-Agent": "scoreboard-fetch/1.0"})

    found = None
    for path in CANDIDATES:
        url = urljoin(BASE, path)
        try:
            r = s.get(url, timeout=20)
            ct = (r.headers.get("content-type") or "").lower()
            if "application/json" not in ct:
                # some servers return text/json
                if not re.search(r"json", ct):
                    continue
            obj = r.json()
            if looks_like_ctfd_scoreboard(obj):
                found = {"url": url, "data": obj}
                print("[+] Found scoreboard endpoint:", url)
                break
        except Exception:
            continue

    if not found:
        print("[-] Could not find a JSON scoreboard endpoint in the candidate list.")
        print("   Tip: open /scoreboard in the browser and check Network/XHR for the real endpoint.")
        return

    OUT.write_text(json.dumps(found, ensure_ascii=False, indent=2), encoding="utf-8")
    print("[+] Saved:", OUT.resolve())

if __name__ == "__main__":
    main()
