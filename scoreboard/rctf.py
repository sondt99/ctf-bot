# pip install playwright python-dotenv
# playwright install

import os
import re
import json
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

url = os.getenv("RCTF_URL")
out = os.getenv("RCTF_OUT")
if not url or not out:
    raise SystemExit("Missing .env: requires RCTF_URL and RCTF_OUT (see .env.example)")
URL: str = url
OUT: Path = Path(out)

# Heuristic: capture requests related to leaderboard/score
PAT = re.compile(r"(score|scores|leader|leaderboard|standing|rank)", re.I)

def main():
    captured = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        def on_response(resp):
            try:
                if resp.request.resource_type not in ("xhr", "fetch"):
                    return
                if not PAT.search(resp.url):
                    return
                ct = (resp.headers.get("content-type") or "").lower()
                if "application/json" in ct:
                    data = resp.json()
                    captured.append({"url": resp.url, "data": data})
                    print("[+] captured JSON:", resp.url)
            except Exception:
                pass

        page.on("response", on_response)
        page.goto(URL, wait_until="networkidle", timeout=60_000)

        browser.close()

    if not captured:
        print("[-] No JSON captured. API may return HTML/WS or be blocked.")
        return

    # save the most likely one (usually the last response contains the table)
    OUT.write_text(json.dumps(captured[-1], ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[+] Saved to {OUT.resolve()}")

if __name__ == "__main__":
    main()
