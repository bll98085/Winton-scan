#!/usr/bin/env python3
"""OCR a scanned bill PDF page on Windows.

Windows port of ocr.swift — CLI and output format are byte-compatible so
parse_bills.py consumes either. Prints one line per recognized text line with
normalized coordinates (origin bottom-left):

    [y0.123 x0.45] text

Hybrid engine: Windows.Media.Ocr (fast) probes the page orientation, then
RapidOCR (PP-OCRv4) does the actual recognition — the built-in OCR silently
drops trailing digits next to table rules on low-quality scans, RapidOCR
doesn't. Output is normalized to Traditional Chinese via a selective character map because
RapidOCR occasionally emits simplified variants (税/缴/号) that would break
the parser's label regexes. If rapidocr is not installed, Windows OCR is
used for everything.

Usage:
    ocr_page.py <pdf> --count                 print page count
    ocr_page.py <pdf> <page>                  OCR one page (coordinate output; probes
                                              rotations 0/90/270)
    ocr_page.py <pdf> <first>-<last> --out-dir <dir>
                                              OCR a page range in ONE process
                                              (the OCR models load only once);
                                              writes <dir>\\<pdf-base>_pN.txt
                                              per page and prints
                                              "  page N done" to stderr after
                                              each page. Consecutive pages
                                              reuse the previous page's
                                              orientation when unambiguous.
    ocr_page.py <pdf> <page> --force-ocr      compatibility flag (already default)
    ocr_page.py <pdf> <page> --rot 270 --region 0.5,0.6,1.0,0.75 --scale 10
                                              re-OCR a small region at high
                                              DPI; region is x0,y0,x1,y1 in
                                              the rotated page's normalized
                                              coordinates (bottom-left origin,
                                              same space as the full-page
                                              output). Printed coordinates are
                                              mapped back to full-page space.
    --out <file>                              write output as UTF-8 to <file>
                                              (avoids PS 5.1 pipeline
                                              encoding issues)
    --save-png <file>                         also save the rendered image
                                              (region or forced-rot mode) so
                                              it can be inspected visually when the
                                              OCR text is ambiguous
"""

import os
import re
import sys
from contextlib import closing
from dataclasses import dataclass

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")


def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


# Lazy initialization lets the batch runner reuse one model for all PDFs and crops.
_HAVE_RAPID = None
winocr = None
RapidOCR = None


def load_engines():
    global _HAVE_RAPID, winocr, RapidOCR
    if _HAVE_RAPID is not None:
        return
    # ONNX must load before WinRT DLLs on this Windows installation.
    try:
        from rapidocr_onnxruntime import RapidOCR as engine
        RapidOCR = engine
        available = True
    except Exception as error:
        available = False
        print(f"RapidOCR 無法載入，改用 Windows OCR：{error}", file=sys.stderr)
    import winocr as windows_engine
    winocr = windows_engine
    _HAVE_RAPID = available


# RapidOCR occasionally emits simplified variants of these characters.
# A whole-text OpenCC pass is NOT safe here: it also rewrites valid
# traditional text (台→臺, 村里的里→裡) and breaks address comparisons.
_S2T = str.maketrans("税缴号额东数别电费单证处记请载时计门点头"
                     "楼龙层间湾区县镇乡兴华宁云桥岛陈刘编统",
                     "稅繳號額東數別電費單證處記請載時計門點頭"
                     "樓龍層間灣區縣鎮鄉興華寧雲橋島陳劉編統")




WINOCR_LANG = "zh-Hant-TW"
# Windows.Media.Ocr rejects bitmaps beyond OcrEngine.MaxImageDimension
# (2600 px on Windows 10); oversized renders are OCRed in overlapping tiles.
MAX_OCR_DIM = 2500

# The orientation probe never needs recognition-grade resolution: scale 2
# keeps an A4 render under MAX_OCR_DIM (no tiling) with less than half the
# pixels of the scale-3 recognition render.
PROBE_SCALE = 2.0
# Pages of one scan batch almost always share an orientation. When the
# previous page's rotation scores at least this much, the other rotations
# are skipped (a wrong-by-90 render scores near 0, and a 180 flip is caught
# by the FLIP_ANCHORS check regardless).
PROBE_MIN_SCORE = 100

_rapid = None


def rapid():
    global _rapid
    load_engines()
    if _rapid is None:
        _rapid = RapidOCR()
    return _rapid


def render(page, rotation_degrees, render_scale, crop_norm=None):
    """Render the page rotated; crop_norm (x0, y0, x1, y1) selects a
    sub-rectangle in the rotated page's normalized bottom-left-origin frame,
    so high --scale values never allocate a full-page bitmap."""
    w_pt, h_pt = page.get_size()
    w = int(w_pt * render_scale) if rotation_degrees % 180 == 0 else int(h_pt * render_scale)
    h = int(h_pt * render_scale) if rotation_degrees % 180 == 0 else int(w_pt * render_scale)
    crop = (0, 0, 0, 0)
    if crop_norm is not None:
        x0, y0, x1, y1 = crop_norm
        # pypdfium2 crop = amount cut from (left, bottom, right, top) of the
        # rotated page in points; the normalized frame is bottom-left origin
        rw_pt = w / render_scale
        rh_pt = h / render_scale
        crop = (x0 * rw_pt, y0 * rh_pt, (1 - x1) * rw_pt, (1 - y1) * rh_pt)
    bitmap = page.render(scale=render_scale, rotation=rotation_degrees % 360, crop=crop)
    try:
        return bitmap.to_pil().convert("RGB")
    finally:
        bitmap.close()


CJK_RE = re.compile(
    "[⺀-鿿豈-﫿！-｠　-〿]")


def join_words(words):
    """Windows OCR treats every CJK char as a word; joining with spaces would
    break the parser's Chinese label regexes. Drop the space when either
    neighbor is CJK, keep it between latin/digit words."""
    text = ""
    for w in words:
        t = w["text"]
        if not text:
            text = t
        elif CJK_RE.search(text[-1]) or CJK_RE.search(t[:1]):
            text += t
        else:
            text += " " + t
    return normalize_text(text)


def normalize_text(t):
    """Undo OCR quirks Vision doesn't have, so parse_bills.py's regexes
    (amounts, 電號, dates) match: interpunct decimal points, CJK dashes
    inside serial numbers, and stray spaces inside digit runs."""
    t = re.sub(r"(?<=\d)\s*[·•]\s*(?=\d)", ".", t)   # 492 · 0元 → 492.0元
    t = re.sub(r"(?<=\d)\s*,\s*(?=\d)", ",", t)      # 10 , 339 → 10,339
    t = re.sub(r"(?<=\d)\s*/\s*(?=\d)", "/", t)      # 115 / 02 / 04 → 115/02/04
    t = re.sub(r"(?<=[0-9A-Za-z])\s*[一−–—~〜]\s*(?=\d)", "-", t)  # 10一33 → 10-33
    t = re.sub(r"(?<=\d) (?=\d)", "", t)             # 1 0 3 3 9元 → 10339元
    t = t.replace("屮", "中")                         # frequent 中 misread
    t = t.replace("峯", "峰")                         # variant the parser expects
    t = re.sub(r"[-−](?=樓)", "一", t)                # 15號-樓 → 15號一樓
    return t


@dataclass
class Line:
    y: float
    x: float
    w: float  # pixels
    h: float  # pixels
    text: str


def winocr_image(img):
    """Windows OCR a PIL image, tiling when it exceeds the engine's size
    limit. Returns Lines with normalized coordinates, bottom-left origin."""
    load_engines()
    W, H = img.size
    tiles = [(0, 0, img)]
    if max(W, H) > MAX_OCR_DIM:
        tiles = []
        overlap = 120
        step = MAX_OCR_DIM - overlap
        for ty in range(0, H, step):
            for tx in range(0, W, step):
                box = (tx, ty, min(tx + MAX_OCR_DIM, W), min(ty + MAX_OCR_DIM, H))
                tiles.append((tx, ty, img.crop(box)))
                if box[2] >= W:
                    break
            if box[3] >= H:
                break
    out = []
    seen = set()
    for tx, ty, tile in tiles:
        try:
            res = winocr.recognize_pil_sync(tile, WINOCR_LANG)
        except Exception as e:
            raise RuntimeError(f"Windows OCR 失敗：{e}") from e
        for line in res.get("lines", []):
            words = line.get("words", [])
            if not words:
                continue
            rects = [w["bounding_rect"] for w in words]
            x0 = min(r["x"] for r in rects) + tx
            y0 = min(r["y"] for r in rects) + ty
            x1 = max(r["x"] + r["width"] for r in rects) + tx
            y1 = max(r["y"] + r["height"] for r in rects) + ty
            text = join_words(words)
            # tiles overlap; drop duplicate detections of the same line
            key = (round(x0 / 8), round(y0 / 8), text)
            if key in seen:
                continue
            seen.add(key)
            out.append(Line(y=1 - ((y0 + y1) / 2) / H, x=x0 / W,
                            w=x1 - x0, h=y1 - y0, text=text))
    return out


def rapid_image(img):
    """RapidOCR a PIL image. Returns Lines like winocr_image."""
    import numpy as np
    W, H = img.size
    result, _ = rapid()(np.array(img))
    out = []
    for box, text, conf in result or []:
        if float(conf) < 0.4:
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        text = normalize_text(text.translate(_S2T))
        out.append(Line(y=1 - ((min(ys) + max(ys)) / 2) / H, x=min(xs) / W,
                        w=max(xs) - min(xs), h=max(ys) - min(ys), text=text))
    return out


def ocr_image(img):
    load_engines()
    return rapid_image(img) if _HAVE_RAPID else winocr_image(img)


def orientation_score(lines):
    """OCR reads text even when the page is rendered 90 deg off; only in the
    correct orientation do long runs come back as WIDE boxes — score those."""
    return sum(len(l.text) for l in lines
               if len(l.text.replace(" ", "")) >= 6 and l.w > 1.5 * l.h)


def emit(buf, page_num, lines, rot, header):
    buf.append(f"== page {page_num} rotation {rot} {header} ==")
    for l in sorted(lines, key=lambda l: (-l.y, l.x)):
        buf.append(f"[y{l.y:.3f} x{l.x:.2f}] {l.text}")


# The wide-box score cannot tell a rotation from its 180-flip, so the emitted
# frame may be upside-down. Known page-header strings must sit in the TOP
# half; when their median y lands in the bottom half, re-render flipped.
FLIP_ANCHORS = ["台灣電力公司", "TAIWAN POWER", "綜合帳單", "台灣自來水",
                "自來水公司", "繳費通知單", "Electricity Bill"]


def process_page(doc, page_num, prev_rot=None, *, scale=3.0, forced_rot=None,
                 region=None, force_ocr=True, save_png=None):
    """OCR one page; returns (text, rot). rot is the orientation actually
    used, or None only in the explicit embedded-text diagnostic mode."""
    if not 1 <= page_num <= len(doc):
        raise ValueError("頁碼超出 PDF 範圍")
    if scale <= 0 or (forced_rot is not None and forced_rot % 90):
        raise ValueError("縮放須大於零，旋轉須為 90 度倍數")
    if region is not None and (len(region) != 4 or not
            (0 <= region[0] < region[2] <= 1 and 0 <= region[1] < region[3] <= 1)):
        raise ValueError("裁圖範圍須在 0..1 內且寬高大於零")
    with closing(doc[page_num - 1]) as page:
        return _process_page(page, page_num, prev_rot, scale, forced_rot,
                             region, force_ocr, save_png)


def _process_page(page, page_num, prev_rot, scale, forced_rot, region, force_ocr, save_png):
    buf = []

    # Embedded-text probe: scanned pages have no text layer, born-digital
    # PDFs do.
    if not force_ocr and region is None:
        with closing(page.get_textpage()) as textpage:
            text = textpage.get_text_range()
        if sum(1 for c in text if not c.isspace()) > 100:
            buf.append(f"== page {page_num} embedded-text ==")
            buf.append(text)
            return "\n".join(buf) + "\n", None

    if region is not None:
        rot = forced_rot if forced_rot is not None else 0
        r = region
        sub = render(page, rot, scale, crop_norm=r)
        if save_png:
            sub.save(save_png)
            print(f"png saved: {save_png}", file=sys.stderr)
        mapped = [Line(y=r[1] + l.y * (r[3] - r[1]), x=r[0] + l.x * (r[2] - r[0]),
                       w=l.w, h=l.h, text=l.text)
                  for l in ocr_image(sub)]
        emit(buf, page_num, mapped, rot,
             f"region {r[0]:.2f},{r[1]:.2f},{r[2]:.2f},{r[3]:.2f} scale {scale:.0f}")
        return "\n".join(buf) + "\n", rot

    if forced_rot is not None:
        img = render(page, forced_rot, scale)
        if save_png:
            img.save(save_png)
            print(f"png saved: {save_png}", file=sys.stderr)
        emit(buf, page_num, ocr_image(img), forced_rot, "forced")
        return "\n".join(buf) + "\n", forced_rot

    # Orientation probe with the fast engine at reduced resolution;
    # recognition with the accurate one at full resolution. The previous
    # page's rotation is probed first and, when unambiguous, the other
    # rotations are skipped.
    probe_scale = min(scale, PROBE_SCALE)
    rots = [0, 90, 270]
    if prev_rot is not None:
        pr = prev_rot % 360
        rots = [pr] + [r for r in rots if r != pr]
    best_score, best_rot = -1, rots[0]
    for rot in rots:
        score = orientation_score(winocr_image(render(page, rot, probe_scale)))
        if score > best_score:
            best_score, best_rot = score, rot
        if rot == rots[0] and prev_rot is not None and score >= PROBE_MIN_SCORE:
            break
    best_lines = ocr_image(render(page, best_rot, scale))

    anchor_ys = sorted(l.y for l in best_lines
                       if any(a in l.text for a in FLIP_ANCHORS))
    if anchor_ys and anchor_ys[len(anchor_ys) // 2] < 0.5:
        flipped = (best_rot + 180) % 360
        best_rot, best_lines = flipped, ocr_image(render(page, flipped, scale))

    emit(buf, page_num, best_lines, best_rot, f"score {best_score}")
    return "\n".join(buf) + "\n", best_rot


def main(argv=None):
    import argparse
    from pathlib import Path
    import pypdfium2 as pdfium
    ap = argparse.ArgumentParser(description="帳單座標 OCR；整批作業請用 run_batch.py")
    ap.add_argument("pdf")
    ap.add_argument("pages", nargs="?")
    ap.add_argument("--count", action="store_true")
    ap.add_argument("--force-ocr", action="store_true", help="相容舊命令；預設即輸出座標 OCR")
    ap.add_argument("--embedded-text", action="store_true", help="僅供診斷，不可交給帳單解析器")
    ap.add_argument("--scale", type=float, default=3.0)
    ap.add_argument("--rot", type=int)
    ap.add_argument("--region")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--out-dir", type=Path)
    ap.add_argument("--save-png")
    args = ap.parse_args(argv)
    with pdfium.PdfDocument(args.pdf) as doc:
        if args.count:
            print(len(doc))
            return
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", args.pages or "")
        if not match:
            ap.error("請提供頁碼或起迄頁碼")
        first, last = int(match[1]), int(match[2] or match[1])
        if not 1 <= first <= last <= len(doc):
            ap.error("頁碼超出 PDF 範圍")
        if last > first and (not args.out_dir or args.out or args.region or args.rot is not None or args.save_png):
            ap.error("多頁模式須使用 --out-dir，不能指定單頁裁圖參數")
        region = [float(v) for v in args.region.split(",")] if args.region else None
        if args.out_dir:
            args.out_dir.mkdir(parents=True, exist_ok=True)
        prev_rot = None
        for page in range(first, last + 1):
            text, rot = process_page(doc, page, prev_rot, scale=args.scale,
                forced_rot=args.rot, region=region, save_png=args.save_png,
                force_ocr=args.force_ocr or not args.embedded_text)
            prev_rot = rot if rot is not None else prev_rot
            output = args.out_dir / f"{Path(args.pdf).stem}_p{page}.txt" if args.out_dir else args.out
            if output:
                output.write_text(text, encoding="utf-8")
            else:
                sys.stdout.write(text)
            print(f"第 {page} 頁完成", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, RuntimeError) as error:
        die(str(error))
