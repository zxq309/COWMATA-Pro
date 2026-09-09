"""Build a versioned PDF from reviewed text and fresh real Qt screenshots.

Editable content: docs/quick-start-illustrated.source.json. Equivalent Markdown
and a SHA-256 manifest are emitted next to the PDF. Missing captures are errors.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from reportlab.lib.colors import HexColor
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parents[1]
WIDTH, HEIGHT = 1200, 1050


def lines(text, font, size, width):
    result, current = [], ""
    for char in text:
        if char == "\n" or pdfmetrics.stringWidth(current + char, font, size) > width:
            result.append(current)
            current = "" if char == "\n" else char
        else:
            current += char
    if current:
        result.append(current)
    return result


def fit_image(pdf, path, rect):
    x, y, width, height = rect
    image = ImageReader(str(path))
    iw, ih = image.getSize()
    factor = min(width / iw, height / ih, 1.55)
    dw, dh = iw * factor, ih * factor
    pdf.drawImage(image, x+(width-dw)/2, y+(height-dh)/2, dw, dh, mask="auto")


def pictures(pdf, paths, rect):
    x, y, width, height = rect
    if len(paths) == 1:
        fit_image(pdf, paths[0], rect)
        return
    sizes = [ImageReader(str(p)).getSize() for p in paths]
    if sizes[0][0] > 700 and sizes[1][0] < 420:
        gap, main_width = 22, width*.77
        fit_image(pdf, paths[0], (x, y, main_width-gap, height))
        fit_image(pdf, paths[1], (x+main_width, y, width-main_width, height))
        return
    if all(w/h < 1.8 for w, h in sizes):
        gap = 22
        for index, path in enumerate(paths):
            fit_image(pdf, path, (x+index*(width+gap)/2, y, (width-gap)/2, height))
    else:
        gap = 16
        natural = [h/w for w, h in sizes]
        available, top = height-gap*(len(paths)-1), y+height
        for path, aspect in zip(paths, natural):
            band = available*aspect/sum(natural)
            top -= band
            fit_image(pdf, path, (x, top, width, band))
            top -= gap


def build(source, screenshots, output, font, bold_font):
    document = json.loads(source.read_text(encoding="utf-8"))
    version, pages = document["version"], document["pages"]
    pdfmetrics.registerFont(TTFont("CJK", font))
    pdfmetrics.registerFont(TTFont("CJKBold", bold_font))
    missing = [name for p in pages for name in p["images"] if not (screenshots/name).is_file()]
    if missing:
        raise FileNotFoundError("Missing actual UI captures: " + ", ".join(missing))
    output.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(output), pagesize=(WIDTH, HEIGHT), pageCompression=1)
    pdf.setTitle(f"COWMATA Annotator {version} - 新手图文操作手册")
    pdf.setAuthor("COWMATA / 杨凌园上园智能科技有限公司")
    pdf.setSubject("准备数据、审查归类、同步标注、保存复核与导出")
    markdown = [f"# {document['title']} {version}", "",
        "操作顺序：准备数据 → 审查归类 → 标注保存 → 复核导出。", "",
        "截图由当前 3.3.0 软件实际运行取得。牛舍录像为授权短片；九轴、绑定、类别和标签为隔离演示数据，不作研究真值。", ""]
    used = {}
    for index, page in enumerate(pages, 1):
        pdf.bookmarkPage(f"page-{index}")
        pdf.addOutlineEntry(f"{index:02d} {page['title']}", f"page-{index}")
        pdf.setFillColor(HexColor("#f4f8ef"))
        pdf.rect(0, 0, WIDTH, HEIGHT, stroke=0, fill=1)
        pdf.setFillColor(HexColor("#8cbd40"))
        pdf.roundRect(30, HEIGHT-79, 55, 43, 8, stroke=0, fill=1)
        pdf.setFillColor(HexColor("#23382e"))
        pdf.setFont("CJKBold", 23)
        pdf.drawCentredString(57.5, HEIGHT-66, f"{index:02d}")
        pdf.setFont("CJKBold", 27)
        pdf.drawString(100, HEIGHT-67, page["title"])
        pdf.setFont("CJK", 12)
        pdf.setFillColor(HexColor("#4b6858"))
        pdf.drawRightString(WIDTH-32, HEIGHT-44, "COWMATA Annotator " + version)
        pdf.drawRightString(WIDTH-32, HEIGHT-65, page["stage"])
        pdf.setFillColor(HexColor("#ffffff"))
        pdf.roundRect(28, 228, WIDTH-56, HEIGHT-326, 9, stroke=0, fill=1)
        pictures(pdf, [screenshots/name for name in page["images"]], (37, 237, WIDTH-74, HEIGHT-344))
        pdf.setFillColor(HexColor("#23382e"))
        pdf.setFont("CJK", 15)
        y = 196
        for number, step in enumerate(page["steps"], 1):
            wrapped = lines(f"{number}.  {step}", "CJK", 15, WIDTH-74)
            if len(wrapped)>2:
                raise ValueError("Shorten manual step: " + step)
            for line in wrapped:
                pdf.drawString(38, y, line)
                y -= 23
            y -= 7
        if y<84:
            raise ValueError("Steps exceed space: " + page["title"])
        pdf.setStrokeColor(HexColor("#d8e3ce"))
        pdf.line(32, 84, WIDTH-32, 84)
        pdf.setFillColor(HexColor("#4b6858"))
        pdf.setFont("CJK", 12)
        note_lines = lines(page["note"], "CJK", 12, WIDTH-70)
        if len(note_lines)>2:
            raise ValueError("Shorten note: " + page["title"])
        for number, line in enumerate(note_lines):
            pdf.drawString(35, 62-number*17, line)
        pdf.setFont("CJK", 10)
        pdf.drawString(35, 19, f"实际程序截图  /  {document['date']}  /  演示标签不作研究真值")
        pdf.drawRightString(WIDTH-35, 19, f"{index:02d} / {len(pages):02d}")
        pdf.showPage()
        markdown.extend([f"## {index:02d} {page['title']}", "", f"阶段：{page['stage']}", ""])
        for name in page["images"]:
            markdown.extend([f"![{page['title']}](../assets/screenshots/manual-330/{name})", ""])
            used[name] = hashlib.sha256((screenshots/name).read_bytes()).hexdigest()
        markdown.extend([f"{n}. {step}" for n, step in enumerate(page["steps"], 1)])
        markdown.extend(["", page["note"], ""])
    pdf.save()
    output.with_suffix(".md").write_text("\n".join(markdown), encoding="utf-8")
    manifest = {"version": version, "date": document["date"], "pages": len(pages),
        "screenshots_sha256": used, "pdf_sha256": hashlib.sha256(output.read_bytes()).hexdigest()}
    output.with_suffix(".manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output":str(output), "pages":len(pages), "screenshots":len(used)}, ensure_ascii=False))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", type=Path, default=ROOT/"docs/quick-start-illustrated.source.json")
    p.add_argument("--screenshots", type=Path, default=ROOT/"assets/screenshots/manual-330")
    p.add_argument("--out", type=Path, default=ROOT/"docs/quick-start-illustrated.pdf")
    p.add_argument("--font", default="C:/Windows/Fonts/msyh.ttc")
    p.add_argument("--bold-font", default="C:/Windows/Fonts/msyhbd.ttc")
    a = p.parse_args()
    build(a.source, a.screenshots, a.out, a.font, a.bold_font)


if __name__ == "__main__":
    main()
