#!/usr/bin/env python3
"""Export the Markdown guides in docs/ to PDF.

    pip install markdown
    python docs/export_pdf.py

Renders each guide to HTML, then prints it with a locally installed Chrome,
Chromium or Edge in headless mode. Nothing is uploaded anywhere.
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import tempfile

DOCS = pathlib.Path(__file__).resolve().parent
GUIDES = {"MANDATE_SYSTEM_GUIDE.md": "Mandate · System Guide for Judges", "SPRINTS.md": "Mandate · Sprint Blueprint"}
BROWSERS = ["google-chrome", "chromium", "chromium-browser", "chrome", "msedge",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
CSS = """
@page { size: Letter; margin: 18mm 16mm 18mm 16mm; }
body { font: 10.5pt/1.5 -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; color: #0f172a; }
h1 { font-size: 22pt; letter-spacing: -0.01em; margin: 0 0 4pt; }
h2 { font-size: 14pt; margin: 20pt 0 6pt; padding-top: 10pt; border-top: 1px solid #cbd5e1; break-after: avoid; }
h3 { font-size: 11.5pt; margin: 16pt 0 4pt; break-after: avoid; }
p, li { orphans: 3; widows: 3; }
blockquote { margin: 12pt 0; padding: 8pt 14pt; border-left: 3px solid #10b981; background: #f1f5f9; font-size: 12pt; }
blockquote p { margin: 0; }
table { border-collapse: collapse; width: 100%; margin: 8pt 0; font-size: 9pt; }
th, td { border: 1px solid #cbd5e1; padding: 5pt 6pt; text-align: left; vertical-align: top; }
th { background: #0f172a; color: #f1f5f9; font-weight: 600; }
tr { break-inside: avoid; }
code { font: 8.6pt "JetBrains Mono", Consolas, Menlo, monospace; background: #f1f5f9; padding: 0 2pt; border-radius: 2pt; }
pre { background: #0f172a; color: #e2e8f0; padding: 8pt 10pt; border-radius: 4pt; white-space: pre-wrap; word-break: break-word; break-inside: avoid; }
pre code { background: none; color: inherit; padding: 0; }
ul { padding-left: 16pt; }
"""


def main() -> int:
    try:
        import markdown
    except ImportError:
        print("Install the converter first:  pip install markdown", file=sys.stderr)
        return 2
    browser = next((p for b in BROWSERS if (p := shutil.which(b) or (b if pathlib.Path(b).exists() else None))), None)
    if not browser:
        print("No Chrome, Chromium or Edge found to print with.", file=sys.stderr)
        return 2
    for name, title in GUIDES.items():
        src = DOCS / name
        body = markdown.markdown(src.read_text(encoding="utf-8"), extensions=["tables", "fenced_code", "sane_lists"])
        body = body.replace("<li>[ ] ", "<li>☐ ")
        html = f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>{title}</title><style>{CSS}</style></head><body>{body}</body></html>"
        out = src.with_suffix(".pdf")
        with tempfile.TemporaryDirectory() as tmp:
            page = pathlib.Path(tmp) / "page.html"
            page.write_text(html, encoding="utf-8")
            subprocess.run([browser, "--headless=new", "--no-sandbox", "--disable-gpu", "--no-pdf-header-footer",
                            f"--print-to-pdf={out}", page.as_uri()], check=True, capture_output=True, timeout=120)
        print(f"wrote {out.relative_to(DOCS.parent)}  ({out.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
