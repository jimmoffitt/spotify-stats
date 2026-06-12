"""
gen_guide_screenshots.py — dev tool: capture full-UI screenshots for the
user's guide (docs/USER_GUIDE.md).

Unlike gen_screenshots.py (which renders bare Plotly figures for the README),
this drives the real Streamlit app in a headless browser, so the shots include
the sidebar nav, filters, and controls a user actually sees.

It launches the app on a temporary port, captures each page via Playwright
(using your installed Chrome — no browser download), and tears the server down.
Output goes to docs/screenshots/guide/.

Requires: playwright (`pip install playwright`) and Google Chrome.

Usage: python gen_guide_screenshots.py
"""
import os
import socket
import subprocess
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

OUT_DIR = os.path.join('docs', 'screenshots', 'guide')
VIEWPORT = {"width": 1500, "height": 1000}


def _free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_ready(port, timeout=60):
    url = f"http://localhost:{port}/healthz"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if urllib.request.urlopen(url, timeout=2).status == 200:
                return True
        except Exception:
            time.sleep(1)
    return False


def _capture(page, base, path, out, *, wait_text, interact=None):
    """Navigate to base+path, wait for the page's content, optionally interact,
    then write a full-page screenshot to OUT_DIR/out."""
    page.goto(f"{base}{path}", wait_until="domcontentloaded")
    page.wait_for_selector(f"text={wait_text}", timeout=30000)
    if interact:
        interact(page)
    page.wait_for_timeout(2500)  # let charts/tables settle
    dest = os.path.join(OUT_DIR, out)
    page.screenshot(path=dest, full_page=True)
    print(f"  ✓ {out}")


def _open_groups(page):
    """On the Bands page: switch to Groups mode and select the New Zealand group."""
    # Streamlit hides the real radio <input> and styles the label, so click text.
    page.get_by_text("Groups", exact=True).click()
    page.wait_for_timeout(1500)
    # Open the group selectbox (main area) and pick New Zealand.
    page.get_by_text("➕ New group…").first.click()
    page.get_by_role("option", name="New Zealand").click()
    page.wait_for_selector("text=Share of all plays", timeout=20000)


# (url_path, output_file, text-to-wait-for, optional interaction)
# Wrapped is the default page, served at "/" — requesting "/wrapped" would hit
# Streamlit's "page not found" fallback, so capture it at the root.
SHOTS = [
    ("/",         "wrapped.png",      "All-time"),
    ("/artists",  "artists.png",      "Top artists"),
    ("/rankings", "rankings.png",     "Top artists per year"),
    ("/bands",    "bands_single.png", "Search artist"),
    ("/bands",    "bands_groups.png", "Single band", _open_groups),
    ("/patterns", "patterns.png",     "When do I listen?"),
    ("/artist-filters", "artist_filters.png", "Artist filters"),
    ("/explore",  "explore.png",      "Explore"),
    ("/export",   "export.png",       "Export"),
    ("/settings", "settings.png",     "Settings"),
]


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "streamlit", "run", "app.py",
         "--server.headless", "true", "--server.port", str(port),
         "--browser.gatherUsageStats", "false"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not _wait_ready(port):
            raise RuntimeError("Streamlit did not become ready in time.")
        base = f"http://localhost:{port}"
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page(viewport=VIEWPORT)
            print(f"Capturing {len(SHOTS)} screenshots -> {OUT_DIR}/")
            for path, out, wait_text, *rest in SHOTS:
                _capture(page, base, path, out,
                         wait_text=wait_text, interact=rest[0] if rest else None)
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    print("Done.")


if __name__ == '__main__':
    main()
