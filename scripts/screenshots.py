"""Capture README screenshots from a locally running app (streamlit run app/streamlit_app.py).

Usage: python scripts/screenshots.py [url]. Needs `pip install playwright` and
`python -m playwright install chromium`.
"""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8599"
OUT = Path(__file__).resolve().parents[1] / "docs" / "screenshots"
PAGES = {"Overview": "overview", "Anomalies": "anomalies", "Optimize": "optimize",
         "QBR Memo": "qbr_memo", "Methodology": "methodology"}

OUT.mkdir(parents=True, exist_ok=True)
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
    page.goto(URL, wait_until="networkidle")
    page.wait_for_selector("text=Spend Desk", timeout=60000)
    for label, name in PAGES.items():
        page.get_by_text(label, exact=True).first.click()
        page.wait_for_timeout(4000)
        if label == "Anomalies":
            page.mouse.wheel(0, 0)
        page.screenshot(path=str(OUT / f"{name}.png"), full_page=False)
        print(f"saved {name}.png")
    browser.close()
