#!/usr/bin/env python3
"""Generate zoomed review crops for rows parse_bills.py still flags.

Usage:
    parse_bills.py --refine <pdf_dir> --flags-json flags.json workdir/*.txt -o draft.csv
    make_review_crops.py flags.json <pdf_dir> <outdir>

For every flagged row this renders the source page at 300 dpi, rotates it to
the same orientation as the OCR output, and crops a full-page-wide band around
the row's address anchor (falling back to the TOU-block anchor, then to the
page middle), zoomed 250%. Inspect the resulting PNGs with view_image to
resolve the remaining ⚠️ items — no coordinate arithmetic needed.

Requires pypdfium2 and Pillow (no external tools). The rotation is applied by
pypdfium2's renderer, i.e. the exact same frame ocr_page.py emits.
"""

import json
import sys
from contextlib import closing
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image

SCALE = 300 / 72  # 300 dpi


def main():
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    flags_path, pdf_dir, outdir = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    outdir.mkdir(parents=True, exist_ok=True)

    rows = json.loads(flags_path.read_text(encoding="utf-8"))
    if not rows:
        print("no flagged rows — nothing to crop")
        return

    docs = {}
    rendered = {}
    for index, r in enumerate(rows, 1):
        pdf = pdf_dir / r["source"]
        if not pdf.exists():
            raise FileNotFoundError(f"{r['source']}：來源 PDF 不存在")
        page, rot = r["page"], r.get("rotation", 0)

        key = (r["source"], page, rot)
        if key not in rendered:
            for previous in rendered.values():
                previous.close()
            rendered.clear()
            with pdfium.PdfDocument(str(pdf)) as doc:
                with closing(doc[page - 1]) as pdf_page:
                    bitmap = pdf_page.render(scale=SCALE, rotation=rot % 360)
                    try:
                        rendered[key] = bitmap.to_pil().convert("RGB")
                    finally:
                        bitmap.close()
        img = rendered[key]

        W, H = img.size
        anchor = r.get("addr_anchor") or r.get("tou_anchor")
        number = (r.get("number") or r.get("type") or "row").replace("/", "-")
        out = outdir / f"crop_{index}_{pdf.stem}_p{page}_{number}.png"
        if anchor is None or r.get("full_page"):
            # no per-field anchor (water/telecom/unknown layout): emit the
            # whole page so the Read tool sees every field at once
            band = img.resize((2200, int(H * 2200 / W)), Image.LANCZOS)
        else:
            # the address row sits just above the TOU block, so leave generous
            # headroom above the anchor
            y = anchor[0]
            top = max(0, int((1 - (y + 0.045)) * H))
            h = int(0.105 * H)
            band = img.crop((0, top, W, min(H, top + h)))
            band = band.resize((int(band.width * 2.5), int(band.height * 2.5)),
                               Image.LANCZOS)
        band.save(out)
        band.close()
        print(out)

    for img in rendered.values():
        img.close()


if __name__ == "__main__":
    main()
