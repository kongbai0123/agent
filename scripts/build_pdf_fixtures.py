"""Build the three PDFs the parser regression needs, reproducibly.

The fixtures are committed, but they are *generated* rather than found, so the
next person can see exactly what each one is supposed to exercise instead of
guessing from a binary:

* ``chinese_text.pdf``  -- a CJK text layer. pypdf can read it; the point is
  that Chinese does not come out as mojibake or empty.
* ``chinese_table.pdf`` -- a ruled table. pypdf flattens it into a stream of
  cells with no row structure; Docling is supposed to keep the table. This is
  where a silent downgrade to pypdf actually costs the user something.
* ``scanned_page.pdf``  -- an image of text with **no text layer at all**.
  pypdf extracts nothing. Any pipeline claiming OCR has to prove it here.

Run:  python scripts/build_pdf_fixtures.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = REPO_ROOT / "tests" / "fixtures" / "pdf"

TITLE = "斑馬魚養殖實驗紀錄"
MARKER = "QX-4471"
BODY = [
    f"實驗編號：{MARKER}",
    "負責人：林維安",
    "樣本溫度維持在攝氏 28.5 度，日照週期為 14 小時。",
    "本頁用於驗證中文文字層是否能被正確擷取，不含表格。",
]

TABLE_ROWS = [
    ["批次", "樣本數", "溫度(°C)", "存活率"],
    ["A-01", "120", "28.5", "97.5%"],
    ["A-02", "118", "29.0", "95.8%"],
    ["B-01", "96", "27.5", "98.9%"],
    ["B-02", "104", "30.0", "88.4%"],
]

SCAN_LINES = [
    "掃描件測試頁",
    f"實驗編號 {MARKER}",
    "此頁沒有文字層，只有影像。",
    "沒有 OCR 的解析器會得到空字串。",
]


def _cjk_font():
    """A CJK font that is actually installed, or a clear failure."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    for name in ("STSong-Light", "MSung-Light", "HeiseiMin-W3"):
        try:
            pdfmetrics.registerFont(UnicodeCIDFont(name))
            return name
        except Exception:  # noqa: BLE001 - try the next candidate
            continue
    raise RuntimeError("no CJK CID font available in this reportlab install")


def build_text_pdf(path: Path) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    font = _cjk_font()
    page = canvas.Canvas(str(path), pagesize=A4)
    page.setFont(font, 18)
    page.drawString(60, 780, TITLE)
    page.setFont(font, 12)
    for index, line in enumerate(BODY):
        page.drawString(60, 740 - index * 22, line)
    page.showPage()
    page.save()


def build_table_pdf(path: Path) -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    font = _cjk_font()
    document = SimpleDocTemplate(str(path), pagesize=A4)
    heading = ParagraphStyle("cjk-heading", fontName=font, fontSize=16, leading=20)
    table = Table(TABLE_ROWS, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), font),
                ("FONTSIZE", (0, 0), (-1, -1), 11),
                ("GRID", (0, 0), (-1, -1), 0.75, colors.black),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dddddd")),
                ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
            ]
        )
    )
    document.build([Paragraph(f"{TITLE}（表格版）", heading), Spacer(1, 16), table])


def build_scanned_pdf(path: Path) -> None:
    """Text rendered to an image, then wrapped in a PDF: no text layer."""
    from PIL import Image, ImageDraw, ImageFont

    font_path = _find_system_cjk_font()
    image = Image.new("RGB", (1240, 1754), "white")  # A4 at 150 dpi
    draw = ImageDraw.Draw(image)
    title_font = ImageFont.truetype(font_path, 56)
    body_font = ImageFont.truetype(font_path, 38)
    draw.text((90, 120), SCAN_LINES[0], fill="black", font=title_font)
    for index, line in enumerate(SCAN_LINES[1:]):
        draw.text((90, 240 + index * 70), line, fill="black", font=body_font)
    # A little grey noise so it reads as a scan rather than a clean render.
    for offset in range(0, 1240, 7):
        draw.point((offset, 1700 + (offset % 5)), fill=(200, 200, 200))
    image.save(path, "PDF", resolution=150.0)


def _find_system_cjk_font() -> str:
    candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
        "C:/Windows/Fonts/msjh.ttc",
        "C:/Windows/Fonts/mingliu.ttc",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    found = subprocess.run(
        ["fc-match", "-f", "%{file}", "sans-serif:lang=zh"],
        capture_output=True,
        text=True,
        check=False,
    )
    if found.returncode == 0 and found.stdout.strip():
        return found.stdout.strip()
    raise RuntimeError("no CJK TrueType font found for the scanned fixture")


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    builders = {
        "chinese_text.pdf": build_text_pdf,
        "chinese_table.pdf": build_table_pdf,
        "scanned_page.pdf": build_scanned_pdf,
    }
    for name, builder in builders.items():
        target = OUTPUT_DIR / name
        builder(target)
        print(f"wrote {target} ({target.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
