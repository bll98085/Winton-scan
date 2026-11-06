#!/usr/bin/env python3
"""Parse Vision-OCR output of Taipower / Taiwan Water bill scans into a draft CSV.

Input files are produced by run_batch.py or run_ocr.ps1: one file per page, lines like

    == page 3 rotation 270 score 2340 ==
    [y0.871 x0.69] 台中市龍井區新興路3巷18號4

Usage:
    parse_bills.py -o draft.csv workdir/*.txt

Pass the OCR output of every source PDF in one call — the source PDF name is
derived from each txt filename (`水電費-1_p3.txt` → `水電費-1.pdf`), so a
single run produces the one consolidated CSV. `--source` overrides the
derived name (only sensible when all txt files come from the same PDF).

Emits the standard 14-column bill CSV (one row per bill; a page may hold
several bills) plus ⚠️ flags in the Notes column wherever a field is missing,
derived, or fails a consistency check. Warnings also go to stderr so the
caller knows which pages need visual review. The number column (電號/水號/
電話號碼) is written without dashes; 統一編號 is the biller's 8-digit UBN
(fixed 97179430 for FET); 年期碼 is the 5-digit ROC year-period prefix of
the 載具號碼. Rows are dropped only when invoice identity, critical fields and evidence
match a previous row; same-number conflicts are retained and flagged.

Built-in consistency checks (Taipower):
  * pre-tax amount + sales tax == total amount            (exact)
  * peak kWh + off-peak kWh   ~= carbon-emission kg/0.474 (±3 kWh)
  * average price × usage     ~= total amount             (±2%)
Taiwan Water pages are parsed too (parse_water):
  * pre-tax subtotal + sales tax == water-fee subtotal
  * water-fee subtotal + collected-fee subtotal == grand total
FET telecom stubs are parsed from their own line items (block subtotals);
the FET_MONTHLY/FET_DISCOUNT constants are only a flagged fallback.
"""

import argparse
import csv
import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

# Windows consoles default to a legacy code page (cp950); the CSV and the
# ⚠️ stderr notes are UTF-8 on every platform
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

CARBON_FACTOR = 0.474  # kg CO2e per kWh, printed on the bill as 電力排碳量

LINE_RE = re.compile(r"^\[y(?P<y>[0-9.]+) x(?P<x>[0-9.]+)\] (?P<text>.*)$")
HEADER_RE = re.compile(r"^== page (?P<page>\d+)(?: rotation (?P<rot>\d+))?")
ELEC_NO_RE = re.compile(r"\b(\d{2}-\d{2}-\d{4}-\d{2}-\d)\b")
# a money value must carry a 元-ish suffix, a decimal point, or a thousands
# comma — this rejects ID-like bare digit runs (e.g. the 統一編號 54595372)
AMOUNT_RE = re.compile(r"^-?(?:[\d,]*\d\.\d+|[\d,]+\s*[元完园兀]|\d{1,3}(?:,\d{3})+)\s*[元完园兀]?\s*$")
BARE_INT_RE = re.compile(r"^[1-9]\d{0,4}$")  # kWh readings never lead with 0 (查核碼 like 024 do)
STUB_TOTAL_RE = re.compile(r"\*+\s*([\d,]{2,9})\s*元")
PERIOD_RE = re.compile(r"\d{3}/\d{2}/\d{2}\s*[至全]\s*\d{3}/\d{2}/\d{2}")
CARBON_RE = re.compile(r"^(\d{1,5})\s*公斤$")
AVG_PRICE_RE = re.compile(r"^(\d\.\d{1,2})\s*元$")
MOBILE_RE = re.compile(r"\b(09\d{8})\b")
# 本期載具號碼 barcode text: 年期別 5 digits (ROC year + 2-digit period,
# e.g. 11506) followed by the 10-char 載具流水碼 — the prefix goes to the
# 年期碼 column, the serial to 發票號碼
CARRIER_RE = re.compile(r"\b(1\d{4})([A-Z0-9]{10})\b")
# 台水的載具字串在 10 碼流水號後還接 4-8 碼檢核碼(遠傳則接帳號尾碼),
# 字尾沒有邊界,不能用 CARRIER_RE
LONG_CARRIER_RE = re.compile(r"\b(1\d{4})([A-Z0-9]{10})[A-Z0-9]{4,8}\b")
UBN_RE = re.compile(r"^\d{8}$")  # 營利事業統一編號 (8 digits, may lead with 0)
# 遠傳綜合帳單不印遠傳自己的統編 — 固定值(遠傳電信股份有限公司)
FET_UBN = "97179430"
FET_PERIOD_RE = re.compile(r"(\d{3}/\d{2}/\d{2})\s*[-–~]\s*(\d{3}/\d{2}/\d{2})")
COLLECTION_DATE_RE = re.compile(r"(?<!\d)(\d{3,4}(?:[/.-]?\d{2}){2})(?!\d)")

# frequent multi-character OCR misreads inside addresses (行政區/路名/後綴).
# Exact, unambiguous replacement pairs only — never fuzzy-correct here.
ADDRESS_WORD_FIXES = (
    ("能井區", "龍井區"), ("竜井區", "龍井區"), ("龍并區", "龍井區"),
    ("仁徳區", "仁德區"), ("壹灣大道", "臺灣大道"), ("台灣大道", "臺灣大道"),
    ("地下至", "地下室"), ("庄单", "庄里"), ("右燙", "右邊"), ("左燙", "左邊"),
)


def fix_address_words(addr):
    for bad, good in ADDRESS_WORD_FIXES:
        addr = addr.replace(bad, good)
    return addr


def normalize_collection_date(value):
    value = str(value).strip()
    match = re.fullmatch(r"(\d{3}|\d{4})[/.-]?(\d{2})[/.-]?(\d{2})", value)
    if not match:
        raise ValueError("收款日期格式不符")
    year, month, day = match.groups()
    western_year = int(year) + 1911 if len(year) == 3 else int(year)
    parsed = date(western_year, int(month), int(day))
    roc_year = parsed.year - 1911
    if not 1 <= roc_year <= 999:
        raise ValueError("收款日期年份超出範圍")
    return f"{roc_year:03d}/{parsed.month:02d}/{parsed.day:02d}"


def extract_collection_date(frags):
    """Prefer a date printed inside/next to 收款章; otherwise use 繳費日期."""
    def candidates(label_pattern, max_dx=0.16, max_dy=0.07):
        found, proofs = {}, {}
        for label in frags:
            if not re.search(label_pattern, label.text):
                continue
            nearby = [(label.text, label.text)]
            nearby += [(fragment.text, label.text + " → " + fragment.text)
                       for fragment in frags if fragment is not label
                       and abs(fragment.x - label.x) <= max_dx
                       and abs(fragment.y - label.y) <= max_dy]
            for text, proof in nearby:
                for match in COLLECTION_DATE_RE.finditer(text):
                    try:
                        normalized = normalize_collection_date(match[1])
                    except ValueError:
                        continue
                    found[normalized] = normalized
                    proofs[normalized] = proof
        return found, proofs

    receipt, receipt_proofs = candidates(r"收款章|Receipt\s*Stamp")
    if len(receipt) == 1:
        value = next(iter(receipt))
        return value, "收款章", receipt_proofs[value], []
    if len(receipt) > 1:
        return "", "", "", ["⚠️收款章有多個日期，須目視確認收款日期"]

    payment, payment_proofs = candidates(r"繳費日期|Payment\s*Date")
    if len(payment) == 1:
        value = next(iter(payment))
        return value, "繳費日期", payment_proofs[value], []
    if len(payment) > 1:
        return "", "", "", ["⚠️繳費日期有多個候選值，須目視確認收款日期"]
    return "", "", "", ["⚠️收款日期未讀到收款章日期或繳費日期"]


def num(s):
    return float(s.replace(",", "").replace(" ", "").rstrip("元完园兀"))


class Frag:
    def __init__(self, y, x, text):
        self.y, self.x, self.text = y, x, text

    def __repr__(self):
        return f"[y{self.y} x{self.x}] {self.text}"


def load_page(path):
    page = None
    rot = 0
    frags = []
    embedded = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        h = HEADER_RE.match(raw)
        if h:
            page = int(h.group("page"))
            rot = int(h.group("rot") or 0)
            if "embedded-text" in raw:
                embedded = None  # sentinel: rest of file is plain text
            continue
        if embedded is None:
            continue
        m = LINE_RE.match(raw)
        if m:
            frags.append(Frag(float(m.group("y")), float(m.group("x")), m.group("text")))
    if page is None:
        m = re.search(r"_p(\d+)\.txt$", str(path))
        page = int(m.group(1)) if m else 0
    return page, frags, embedded is None, rot


# a bill's total must carry a thousands comma or a 元-ish suffix; bare digit
# runs (kWh readings, 查核碼) are never accepted as totals
TOTAL_VAL_RE = re.compile(r"^(?:[\d,]+\s*[元完园兀]|\d{1,3}(?:,\d{3})+)\s*$")


def nearest_value(frags, label, pattern, dy=0.008, dx=0.35, cast=num):
    """Value fragment on (roughly) the same text row as a label fragment.

    y misalignment is weighted heavily: adjacent rows sit ~0.008 apart, so a
    value from the neighbouring row must never beat one on the label's own
    row just because their x offsets tie (that mispairing made 離峰度數 echo
    the 尖峰 row's value)."""
    best, best_d = None, 1e9
    for f in frags:
        if f is label or abs(f.y - label.y) > dy or abs(f.x - label.x) > dx:
            continue
        if not pattern.match(f.text.strip()):
            continue
        d = abs(f.x - label.x) + 5 * abs(f.y - label.y)
        if d < best_d:
            best, best_d = f, d
    if best is None:
        return None
    try:
        return cast(best.text.strip())
    except ValueError:
        return None


def find_ubn(frags, notes, flags):
    """本公司營利事業統一編號 — the biller's 8-digit UBN printed on the bill.

    The seller's label carries 本公司 (本公司營利事業統一編號); the customer's
    carries 用戶 (用戶營利事業統一編號) and is skipped. A bare 統一編號 label
    is only accepted as a fallback with a spot-check note."""
    strong = weak = None
    for f in frags:
        # OCR emits simplified variants (统/编/号) the S2T map may miss, and
        # garbles drop characters (本公司利事業统一號05798960)
        t = (f.text.replace(" ", "")
             .replace("统", "統").replace("编", "編").replace("号", "號"))
        if not re.search(r"統一編|統一號|統編", t):
            continue
        if re.search(r"用戶|用户|買受|买受", t):
            continue  # the customer's UBN, never the biller's
        m = re.search(r"統一編?號?[:：]?(\d{8})(?!\d)", t)
        v = m.group(1) if m else nearest_value(frags, f, UBN_RE, dx=0.30,
                                               cast=lambda s: s)
        if v is None:
            continue
        if "公司" in t:
            strong = strong or v
        else:
            weak = weak or v
    if strong:
        return strong
    if weak:
        notes.append("統一編號取自無「營利事業/公司」字樣的標籤,請抽查")
        return weak
    flags.append("⚠️統一編號未讀到")
    return ""


class Refiner:
    """Re-OCR a small region of the source page at high DPI with the shared OCR engine.

    Recovers fields the full-page pass misses (wrapped address suffixes,
    tiny TOU kWh digits). Regions are given in the same rotated normalized
    frame as the page's full OCR output, so coordinates from that output can
    be used directly. Calls are capped per page to bound runtime.
    """

    def __init__(self, pdf, page, rot, backend=None):
        self.pdf, self.page, self.rot = str(pdf), page, rot
        here = Path(__file__).resolve().parent
        # macOS: compiled Vision tool; Windows: ocr_page.py under this python
        self.bin = here / "ocrbin"
        if self.bin.exists() and sys.platform != "win32":
            self.cmd = [str(self.bin)]
        else:
            self.cmd = [sys.executable, str(here / "ocr_page.py")]
            self.bin = here / "ocr_page.py"
        self.enabled = self.bin.exists() and Path(pdf).exists()
        self.cache = {}
        self.calls = 0
        self.backend = backend
        self.errors = []

    def reocr(self, x0, y0, x1, y1, scale=12):
        if not self.enabled or self.calls >= 12:
            return []
        key = (round(x0, 3), round(y0, 3), round(x1, 3), round(y1, 3), scale)
        if key in self.cache:
            return self.cache[key]
        region = f"{max(0, x0):.3f},{max(0, y0):.3f},{min(1, x1):.3f},{min(1, y1):.3f}"
        self.calls += 1
        try:
            if self.backend is not None:
                out = self.backend(self.pdf, self.page, self.rot,
                                   [float(v) for v in region.split(",")], scale)
            elif sys.platform == "win32":
                import pypdfium2 as pdfium
                from ocr_page import process_page
                with pdfium.PdfDocument(self.pdf) as doc:
                    out, _ = process_page(doc, self.page, forced_rot=self.rot,
                        region=[float(v) for v in region.split(",")], scale=scale)
            else:
                import subprocess
                out = subprocess.run(self.cmd + [self.pdf, str(self.page),
                    "--rot", str(self.rot), "--region", region, "--scale", str(scale)],
                    capture_output=True, text=True, encoding="utf-8", timeout=120,
                    check=True).stdout
        except Exception as error:
            message = f"⚠️局部重辨識失敗：{type(error).__name__}: {error}"
            if message not in self.errors:
                self.errors.append(message)
            print(message, file=sys.stderr)
            out = ""
        frags = [Frag(float(m.group("y")), float(m.group("x")), m.group("text"))
                 for m in map(LINE_RE.match, out.splitlines()) if m]
        self.cache[key] = frags
        return frags


def parse_electricity(frags, numbers_with_pos, refiner=None):
    """Split the page's fragments among bills and extract fields.

    Bills tile the page in a grid: their 電號 fragments mark each bill's left
    edge, so we cut the page into columns at those x positions, and cut a
    column horizontally when two bills stack in it.
    """
    insts = defaultdict(list)
    for no, a in numbers_with_pos:
        insts[no].append(a)

    # cluster bills into columns by the leftmost 電號 instance
    metas = sorted(
        ({"no": no, "x": min(f.x for f in lst), "ys": sorted(f.y for f in lst)}
         for no, lst in insts.items()),
        key=lambda m: m["x"])
    columns = []
    for m in metas:
        if columns and m["x"] - columns[-1][-1]["x"] < 0.25:
            columns[-1].append(m)
        else:
            columns.append([m])

    # Bills tile the page evenly, so with N columns the cut lines sit at k/N —
    # this holds for both normal sheets (電號 near a bill's left edge) and
    # sheets scanned upside-down, where the whole layout is mirrored and the
    # 電號 lands deep inside its tile.
    ncols = len(columns)
    mirrored = ncols > 0 and min(
        col[0]["x"] - i / ncols for i, col in enumerate(columns)) > 0.5 / ncols

    def locate(f):
        ci = min(int(f.x * ncols), ncols - 1)
        col = columns[ci]
        for j in range(len(col) - 1):
            boundary = (min(col[j]["ys"]) + max(col[j + 1]["ys"])) / 2
            above = f.y >= boundary
            if above != mirrored:
                return col[j]["no"]
        return col[-1]["no"]

    # physical reading order: on mirrored sheets the top bill lands at the right
    order = []
    for i, col in enumerate(columns):
        col.sort(key=lambda m: max(m["ys"]), reverse=not mirrored)
        for j, m in enumerate(col):
            order.append((j, ncols - 1 - i if mirrored else i, m["no"]))
    order.sort()

    page_periods = {m.group(0).replace("全", "至").replace(" ", "")
                    for f in frags for m in [PERIOD_RE.search(f.text)] if m}

    bills = defaultdict(list)
    for f in frags:
        bills[locate(f)].append(f)

    return [extract_bill(no, bills[no], page_periods, refiner) for _, _, no in order]


def extract_bill(no, frags, page_periods=(), refiner=None):
    notes, flags = [], []

    def labels(substr, exclude=None):
        out = []
        for f in frags:
            if substr in f.text and (exclude is None or exclude not in f.text):
                out.append(f)
        return out

    # --- amounts -----------------------------------------------------------
    pretax = tax = None
    for lab in labels("稅前"):
        pretax = nearest_value(frags, lab, AMOUNT_RE)
        if pretax is not None:
            break
    for lab in labels("營業稅", exclude="申報"):
        inline = re.search(r"營業稅\s*(-?[\d,]+(?:\.\d+)?)\s*元", lab.text)
        if inline:
            tax = num(inline.group(1))
            break
        tax = nearest_value(frags, lab, AMOUNT_RE)
        if tax is not None:
            break

    # the total is printed several times per bill; gather every copy and vote
    totals = []
    for f in frags:
        m = STUB_TOTAL_RE.search(f.text)
        if m:
            totals.append(num(m.group(1)))
    for lab in labels("Total Amount"):  # header box: value sits below the label
        cands = [f for f in frags
                 if lab.y - 0.045 < f.y < lab.y - 0.003 and abs(f.x - lab.x) < 0.3
                 and TOTAL_VAL_RE.match(f.text.strip())]
        cands.sort(key=lambda f: lab.y - f.y)
        if cands:
            totals.append(num(cands[0].text.strip()))
    for lab in labels("應繳總金額", exclude="稅前"):
        v = nearest_value(frags, lab, TOTAL_VAL_RE, dx=0.42)
        if v is not None:
            totals.append(v)
    totals = [t for t in totals if t == int(t) and t >= 10]
    total = None
    if totals:
        total = max(set(totals), key=totals.count)  # consensus across copies

    original_tax = tax

    # Arithmetic completion/repair. The total is consensus-voted across its
    # several printed copies (most trustworthy), and 營業稅 is always 5% of
    # 稅前 (rounded) — so one missing or misread component is recoverable:
    # trust the pair that satisfies both 稅前+稅=總額 and 稅≈稅前×5%.
    def tax_ok(p, t):
        return p is not None and t is not None and abs(p * 0.05 - t) <= 1.5

    if total is not None:
        if None not in (pretax, tax) and abs((pretax + tax) - total) > 0.5:
            if tax_ok(total - tax, tax):
                notes.append(f"稅前OCR值{pretax:g}不符檢核,依總額−營業稅更正為{total - tax:g}")
                pretax = total - tax
            elif tax_ok(pretax, total - pretax):
                notes.append(f"營業稅OCR值{tax:g}不符檢核,依總額−稅前更正為{total - pretax:g}")
                tax = total - pretax
        elif pretax is None and tax is not None and tax_ok(total - tax, tax):
            pretax = total - tax
            notes.append("稅前由總額−營業稅推得")
        elif tax is None and pretax is not None and tax_ok(pretax, total - pretax):
            tax = total - pretax
            notes.append("營業稅由總額−稅前推得")
        elif pretax is None and tax is None:
            t = round(total * 0.05 / 1.05)
            if tax_ok(total - t, t):
                pretax, tax = total - t, t
                notes.append("稅前/營業稅由總額推得(5%)")

    if total is None:
        flags.append("⚠️總金額未讀到")
    if pretax is None:
        flags.append("⚠️稅前金額未讀到")
    if tax is None:
        flags.append("⚠️營業稅未讀到")
    if None not in (pretax, tax, total) and abs((pretax + tax) - total) > 0.5:
        flags.append(f"⚠️金額檢核不符(稅前{pretax:g}+稅{tax:g}≠總額{total:g})")

    tax_derived = tax is not None and (original_tax is None or tax != original_tax)
    if tax_derived:
        flags.append("⚠️營業稅為推算或修正值，須核對原單後才能匯入稅額與扣抵否")
    tax_mismatch = pretax is not None and tax is not None and not tax_ok(pretax, tax)
    if tax_mismatch:
        flags.append(f"⚠️營業稅檢核不符(稅前{pretax:g}×5%≠{tax:g})")

    # --- usage --------------------------------------------------------------
    carbon = avg_price = None
    for f in frags:
        m = CARBON_RE.match(f.text.strip())
        if m:
            carbon = int(m.group(1))
        m = re.search(r"平均電價\s*([\d.]+)\s*元", f.text)
        if m:
            avg_price = float(m.group(1))
    if avg_price is None:
        for lab in labels("平均電價"):
            avg_price = nearest_value(frags, lab, AVG_PRICE_RE,
                                      cast=lambda s: float(s.rstrip("元")))
            if avg_price is not None:
                break
    # a dropped decimal point (3.72 → 372) turns the price check into noise
    if avg_price is not None and not 1.0 <= avg_price <= 20:
        avg_price = None

    def read_tou(fr):
        p = o = None
        for f in fr:
            if "尖峰度數" in f.text and p is None:
                p = nearest_value(fr, f, BARE_INT_RE, dx=0.45, cast=int)
            if "離峰度數" in f.text and o is None:
                o = nearest_value(fr, f, BARE_INT_RE, dx=0.45, cast=int)
        return p, o

    peak, offpeak = read_tou(frags)

    # fallback: two bare integers stacked right below the 計費度數 label
    tou_block = (labels("Energy Consumption") + labels("計費度數")
                 + labels("計貴度數"))
    if peak is None or offpeak is None:
        for lab in tou_block:
            cands = [f for f in frags
                     if BARE_INT_RE.match(f.text.strip())
                     and lab.y - 0.035 < f.y < lab.y + 0.004
                     and -0.05 < f.x - lab.x < 0.3]
            cands.sort(key=lambda f: -f.y)
            if len(cands) >= 2:
                peak, offpeak = int(cands[0].text), int(cands[1].text)
                break
            if len(cands) == 1 and peak is None and offpeak is None:
                peak = int(cands[0].text)  # single-rate bill (no TOU split)

    has_tou = bool(labels("尖峰度數") or labels("離峰度數"))

    # refine pass: the kWh digits are tiny — re-OCR the 計費度數 block at high
    # DPI when one TOU component is still missing
    tou_anchor = (tou_block + labels("尖峰度數") + labels("離峰度數") + [None])[0]
    if refiner is not None and has_tou and (peak is None or offpeak is None) \
            and tou_anchor is not None:
        extra = refiner.reocr(tou_anchor.x - 0.02, tou_anchor.y - 0.05,
                              tou_anchor.x + 0.36, tou_anchor.y + 0.012)
        if extra:
            p2, o2 = read_tou(frags + extra)
            if peak is None:
                peak = p2
            if offpeak is None:
                offpeak = o2
    # 非時間電價帳單的度數印在 經常度數 列
    if peak is None and offpeak is None and not has_tou:
        for lab in labels("經常度數"):
            peak = nearest_value(frags, lab, BARE_INT_RE, dx=0.45, cast=int)
            if peak is not None:
                break

    usage = None
    if peak is not None and offpeak is not None:
        usage = peak + offpeak
        notes.append(f"尖峰{peak}+離峰{offpeak}")
    elif not has_tou and peak is not None:
        usage = peak
        notes.append("非時間電價(單一度數)")

    def price_ok(u):
        return (None not in (avg_price, total) and u
                and abs(avg_price * u - total) / total <= 0.02)

    if carbon:
        implied = carbon / CARBON_FACTOR
        if usage is None and has_tou and (peak is not None) != (offpeak is not None):
            # one of the TOU components was读到; derive the other from carbon kg
            found = peak if peak is not None else offpeak
            other = round(implied) - found
            usage = round(implied)
            which = "離峰" if peak is not None else "尖峰"
            notes.append(f"{'尖峰' if peak is not None else '離峰'}{found}")
            flags.append(f"⚠️{which}未讀到,依排碳量{carbon}公斤推算{which}約{other}、總度數約{usage}(±1)")
        elif usage is None:
            usage = round(implied)
            flags.append(f"⚠️度數未讀到,依排碳量{carbon}公斤推算約{usage}度(±1)")
        elif abs(usage - implied) > 3:
            # 度數與排碳量不符:先高解析重讀度數區塊拿正確讀值;
            # 讀不到再以排碳量推回總度數,均價檢核把關
            repaired = None
            tou_note = f"尖峰{peak}+離峰{offpeak}"
            if has_tou and refiner is not None and tou_anchor is not None:
                extra = refiner.reocr(tou_anchor.x - 0.02, tou_anchor.y - 0.05,
                                      tou_anchor.x + 0.36, tou_anchor.y + 0.012)
                p2, o2 = read_tou(extra)
                if None not in (p2, o2) and abs((p2 + o2) - implied) <= 3:
                    repaired = (p2, o2, p2 + o2,
                                f"尖峰{p2}+離峰{o2}(原讀值{peak}/{offpeak}"
                                f"經高解析重讀修正)")
            if repaired is None and price_ok(round(implied)):
                repaired = (peak, offpeak, round(implied),
                            f"尖峰離峰讀值({peak}/{offpeak})與排碳不符,"
                            f"總度數依排碳量修正為{round(implied)}"
                            f"(±1,通過均價檢核),個別值請抽查")
            if repaired is not None:
                peak, offpeak, usage = repaired[0], repaired[1], repaired[2]
                if tou_note in notes:
                    notes[notes.index(tou_note)] = repaired[3]
                else:
                    notes.append(repaired[3])
            else:
                flags.append(f"⚠️度數與排碳量不符(排碳{carbon}公斤≈{implied:.0f}度,請人工複核)")
    elif usage is None:
        flags.append("⚠️度數未讀到")

    if None not in (avg_price, usage, total) and usage > 0:
        if abs(avg_price * usage - total) / total > 0.02:
            flags.append(f"⚠️均價檢核不符(均價{avg_price}×{usage}度≠{total:g})")

    # --- address ------------------------------------------------------------
    def find_address(fr):
        """Returns (address, addr_frag, flag, note); flag is None when complete."""
        address = ""
        addr_frag = None
        label_frags = []
        for f in fr:
            if (re.search(r"[用甬角日開][電笔水蜜雷單].{0,2}[址价虀]", f.text) or "地址" in f.text
                    or (len(f.text.strip()) <= 4 and re.match(r"^[用甬角日開][電笔水蜜雷單]", f.text.strip()))):
                label_frags.append(f)
                if addr_frag is None:
                    addr_frag = f
                m = re.search(r"[址价虀][:：]?\s*(.+)$", f.text)
                if m and len(m.group(1)) >= 6 and len(m.group(1)) > len(address):
                    address = m.group(1)  # keep the longest candidate
                    addr_frag = f
        # value-only lines (label in a separate fragment, or a longer re-read
        # of a truncated line): anything city-shaped on the same physical row
        anchors = label_frags + ([addr_frag] if addr_frag is not None else [])
        for f in fr:
            if f in label_frags or "地址" in f.text:
                continue
            if not re.search(r"[市縣].{1,4}[區鄉鎮市]", f.text):
                continue
            if any(abs(f.y - a.y) < 0.012 and abs(f.x - a.x) < 0.3 for a in anchors):
                if len(f.text.strip()) > len(address):
                    address = f.text.strip()
                    addr_frag = f
        flag = note = None
        if address:
            address = re.sub(r"^[：:\s]+", "", address.replace(" ", ""))
            # OCR often garbles the first character of 台中市/台南市 (雇/盒/阜/草…)
            address = re.sub(r"^[^台臺]{1,2}(中市|南市|北市)", r"台\1", address)
            if not re.match(r"^[台臺新桃苗彰南高屏宜花基雲嘉]", address):
                address = re.sub(r"^[^台臺新桃苗彰南高屏宜花基雲嘉]*", "", address)
            # bracket misreads of digit 1 inside the street number (巷】8號 → 巷18號)
            address = re.sub(r"(?<=[路街巷弄])[】〕\]](?=\d)", "1", address)
            # wrapped suffix (floor / basement / unit) on the adjacent physical row
            # (the row above on upside-down scans, so search both directions);
            # it must carry a CJK address marker — or be a pure unit suffix like
            # -2 — not just digits/punctuation
            def cont_ok(t):
                if re.fullmatch(r"[-−–]\d{1,2}", t):
                    return True
                return bool(re.match(r"^[0-9０-９一二三四五六七八九十丶、．\s]*[號樓室邊巷弄之地下]", t)
                            and re.search(r"[號樓室邊巷弄地下]", t))
            conts = [f for f in fr
                     if 0.004 < abs(addr_frag.y - f.y) < 0.022 and abs(f.x - addr_frag.x) < 0.18
                     and len(f.text.strip()) <= 10 and cont_ok(f.text.strip())]
            # nearest row first; on the same row prefer the longer read
            conts.sort(key=lambda f: (round(abs(addr_frag.y - f.y), 3), -len(f.text)))
            if conts:
                address += re.sub(r"^[丶、．\s]+", "", conts[0].text.strip().replace(" ", ""))
            # frequent misreads (行政區/路名/後綴)
            address = fix_address_words(address)
            address = re.sub(r"(?<=\d)慢", "樓", address)  # 3慢 → 3樓
            # the printed suffix is always 右邊/左邊 but OCR reliably drops the
            # line-final 邊 even at high DPI — complete it, note it for spot checks
            if re.search(r"[右左]$", address):
                address += "邊"
                note = "地址尾補「邊」(版式為右邊/左邊,行尾OCR常漏,請抽查)"
            # completeness checks on the FINAL merged address
            if not re.search(r"(樓|室|邊|之\d+|[樓層]-?\d*)$", address):
                flag = "⚠️地址可能不完整(請確認樓層/門牌後綴)"
            # patterns that read "complete" but are missing characters
            if flag is None and re.search(r"[巷弄]號", address):
                flag = "⚠️地址疑缺門牌數字(如「弄號」應為「弄N號」)"
            elif flag is None and re.search(r"號樓", address):
                # 行尾的樓層數字(尤其「一」)連高解析都常漏讀,需目視裁圖確認
                flag = "⚠️地址疑缺樓層數字(「號樓」應為「號N樓」)"
            elif flag is None and re.search(r"號地(?![下])", address):
                flag = "⚠️地址「號地」後疑缺「下」(如地下1樓)"
            elif flag is None and re.search(r"[里村]", address) and "巷" in address \
                    and not re.search(r"[街路]", address):
                flag = "⚠️地址疑缺街/路名(村里後直接接巷)"
        else:
            flag = "⚠️地址未讀到"
        return address, addr_frag, flag, note

    address, addr_frag, aflag, anote = find_address(frags)
    # 全形樓層常把「3.4樓」讀成「34樓」— 視為可疑,一併高解析複查
    suspicious = re.search(r"號\d{2,3}樓", address) is not None
    if (aflag or suspicious) and refiner is not None:
        # refine pass: re-OCR tightly around the address line (the wrapped
        # floor suffix is tiny and often skipped by the full-page pass)
        if addr_frag is not None:
            extra = refiner.reocr(addr_frag.x - 0.01, addr_frag.y - 0.022,
                                  addr_frag.x + 0.34, addr_frag.y + 0.010, scale=14)
        else:
            # no address line at all: the address sits next to the 統一編號 row
            labs = labels("統一編號") + labels("事業統")
            extra = refiner.reocr(labs[0].x - 0.03, labs[0].y - 0.05,
                                  labs[0].x + 0.36, labs[0].y + 0.05, scale=14) if labs else []
        if extra:
            a2, f2, flag2, note2 = find_address(frags + extra)
            if flag2 is None or (aflag == "⚠️地址未讀到" and a2):
                if suspicious and not aflag and a2 != address and flag2 is None:
                    note2 = ((note2 + ";") if note2 else "") + "地址樓層經高解析重讀修正"
                address, addr_frag, aflag, anote = a2, f2, flag2, note2
    if anote:
        notes.append(anote)
    if aflag:
        flags.append(aflag)

    # --- billing period -----------------------------------------------------
    period = ""
    for f in frags:
        m = PERIOD_RE.search(f.text)
        if m:
            period = m.group(0).replace("全", "至").replace(" ", "")
            break
    if not period and len(page_periods) == 1:
        period = next(iter(page_periods))
        flags.append("⚠️計費期間取自同頁另一帳單,請確認")
    elif not period:
        flags.append("⚠️計費期間未讀到")

    # --- invoice number (本期載具號碼: 年期別5碼+載具流水碼10碼, keep the serial) --
    # other 15-char digit runs exist on the stub (3rd payment barcode), so a
    # candidate only counts as the carrier number when a 載具 label anchors it
    invoice = ym = ""
    carrier_cands = []
    for f in frags:
        cleaned = f.text.replace(" ", "").upper()  # OCR may drop letter case
        if re.fullmatch(r"1009\d{11}", cleaned):
            continue  # 檢核碼 barcode line, not the carrier number
        m = CARRIER_RE.search(cleaned)
        if m:
            carrier_cands.append((f, m.group(1), m.group(2)))
    carrier_labels = labels("載具")
    if carrier_labels:
        def label_dist(c):
            return min(abs(c[0].y - l.y) + abs(c[0].x - l.x) for l in carrier_labels)
        near = [c for c in carrier_cands if label_dist(c) < 0.35]
        near.sort(key=label_dist)
        if near:
            ym, invoice = near[0][1], near[0][2]
        else:
            flags.append("⚠️發票號碼(載具流水碼)未讀到")
    elif len(carrier_cands) == 1:
        ym, invoice = carrier_cands[0][1], carrier_cands[0][2]
        if re.fullmatch(r"[8B]{2}[A-Z0-9]\d{7}", invoice):
            # 年期別前綴+BB 開頭流水號的結構正確,單一候選即可信
            notes.append("載具標籤未讀到,號碼結構符合(BB流水號),已採用")
        else:
            flags.append("⚠️載具標籤未讀到,發票號碼請人工確認")
    else:
        flags.append("⚠️發票號碼(載具流水碼)未讀到")

    invoice = normalize_serial(invoice, notes)

    ubn = find_ubn(frags, notes, flags)

    if pretax is not None:
        notes.insert(0, f"稅前{pretax:g}元")

    return {
        "_frags": frags,
        "_tax_status": "derived" if tax_derived or tax_mismatch else ("source" if tax is not None else "missing"),
        "type": "電費",
        "usage": "" if usage is None else usage,
        "total": "" if total is None else f"{total:,.0f}",
        "tax": "" if tax is None else f"{tax:,.0f}",
        "address": address,
        "period": period,
        "number": no,
        "ubn": ubn,
        "ym": ym,
        "invoice": invoice,
        "notes": ";".join(notes + flags),
        "_flags": flags,
        "_anchors": {
            "addr": [addr_frag.y, addr_frag.x] if addr_frag is not None else None,
            "tou": [tou_anchor.y, tou_anchor.x] if tou_anchor is not None else None,
        },
    }


def normalize_serial(serial, notes):
    """載具流水碼 always opens with two letters (BB…), and Vision confuses
    B with 8 in the barcode's human-readable line — normalize leading 8s."""
    if serial and re.fullmatch(r"[8B]{2}[A-Z0-9]\d{7}", serial) and "8" in serial[:2]:
        notes.append(f"發票號碼開頭{serial[:2]}已正規化為BB(8/B形近)")
        return "BB" + serial[2:]
    return serial


def water_value(frags, label, dy=0.008, dx=0.40):
    """Amount to the right of a label on a water bill ($ prefix and fullwidth
    comma tolerated, e.g. $2，405元)."""
    best, best_d = None, 1e9
    for f in frags:
        if f is label or abs(f.y - label.y) > dy:
            continue
        t = f.text.strip().replace("，", ",").lstrip("$＄")
        if not (0 < f.x - label.x <= dx) or not AMOUNT_RE.match(t):
            continue
        if f.x - label.x < best_d:
            try:
                best, best_d = num(t), f.x - label.x
            except ValueError:
                pass
    return best


def parse_water(frags):
    """Taiwan Water bill (one bill per page). Extracts the fields geometrically
    and runs the water identities:
      稅前小計 + 營業稅 = 水費小計 ;  水費小計 + 代徵費用小計 = 代收總金額
      營業稅 ≈ 稅前 × 5% (只有水費課稅,代收費用不課)
    """
    notes, flags = [], []

    def labels(substr, exclude=None):
        return [f for f in frags if substr in f.text
                and (exclude is None or exclude not in f.text)]

    def first_value(labs, **kw):
        for lab in labs:
            v = water_value(frags, lab, **kw)
            if v is not None:
                return v
        return None

    # --- amounts (labels come back garbled: 營/萱/管業稅, 代微/代徵費用小計) ----
    pretax = first_value(labels("稅前"))
    tax = first_value(labels("業稅", exclude="事業"))
    fee_sub = first_value([f for f in labels("小計") if "水費" in f.text], dx=0.45)
    coll_sub = first_value([f for f in labels("費用小計") if "水費" not in f.text],
                           dx=0.45)

    totals = [v for lab in labels("總金額")
              for v in [water_value(frags, lab, dx=0.45)] if v is not None]
    # the left-hand stub prints the total after a short 金/金額/總金 label
    for f in frags:
        t = f.text.strip()
        if len(t) <= 3 and "金" in t and "稅" not in t:
            v = water_value(frags, f, dx=0.30)
            if v is not None:
                totals.append(v)
    total = max(set(totals), key=totals.count) if totals else None

    original_tax = tax
    if fee_sub is None and None not in (pretax, tax):
        fee_sub = pretax + tax
    if None not in (fee_sub, coll_sub):
        implied = fee_sub + coll_sub
        if total is None:
            total = implied
            notes.append("總金額由水費小計+代徵小計推得")
        elif abs(total - implied) > 0.5:
            flags.append(f"⚠️金額檢核不符(水費{fee_sub:g}+代徵{coll_sub:g}≠總額{total:g})")
    if pretax is None and None not in (tax, fee_sub):
        pretax = fee_sub - tax
        notes.append("稅前由水費小計−營業稅推得")
    elif tax is None and None not in (pretax, fee_sub):
        tax = fee_sub - pretax
        notes.append("營業稅由水費小計−稅前推得")
    if None not in (pretax, tax):
        if abs(pretax * 0.05 - tax) > 1.5:
            flags.append(f"⚠️營業稅檢核不符(稅前{pretax:g}×5%≠{tax:g})")
        if fee_sub is not None and abs((pretax + tax) - fee_sub) > 1:
            flags.append(f"⚠️金額檢核不符(稅前{pretax:g}+稅{tax:g}≠水費小計{fee_sub:g})")
    if total is None:
        flags.append("⚠️總金額未讀到")
    if tax is None:
        flags.append("⚠️營業稅未讀到")

    tax_derived = tax is not None and (original_tax is None or tax != original_tax)
    tax_mismatch = None not in (pretax, tax) and abs(pretax * 0.05 - tax) > 1.5
    if tax_derived:
        flags.append("⚠️營業稅為推算或修正值，須核對原單後才能匯入稅額與扣抵否")

    # --- usage (本期實用度數) ------------------------------------------------
    usage = None
    for lab in labels("實用度數"):
        usage = nearest_value(frags, lab, BARE_INT_RE, dx=0.30, cast=int)
        if usage is not None:
            break
    if usage is None:
        flags.append("⚠️度數未讀到")

    # --- 水號 (站所-號-檢號, e.g. 4P-51666420-3) -----------------------------
    number = ""
    for lab in labels("水號"):
        parts = [f for f in frags
                 if f is not lab and abs(f.y - lab.y) < 0.006
                 and 0 < f.x - lab.x < 0.30
                 and re.fullmatch(r"[A-Za-z0-9]{1,10}", f.text.strip())]
        parts.sort(key=lambda f: f.x)
        if parts:
            number = "-".join(p.text.strip().upper() for p in parts)
            break
    if not re.search(r"\d{7,8}", number):
        flags.append("⚠️水號未讀到或不完整")

    # --- address (label 用水地址, value lines sit BELOW the label) ------------
    address, addr_frag = "", None
    labs = labels("水地址") or labels("地址")
    if labs:
        addr_frag = labs[0]
        lines = [f for f in frags
                 if abs(f.x - addr_frag.x) < 0.06
                 and 0.004 < addr_frag.y - f.y < 0.045
                 and not re.search(r"統一|编號|編號|繳費|期日", f.text)
                 and re.search(r"[市縣區鄉鎮里村路街巷弄號樓段FfBb]", f.text)]
        lines.sort(key=lambda f: -f.y)
        address = "".join(l.text.strip().replace(" ", "") for l in lines)
    if address:
        address = re.sub(r"^[：:\s]+", "", address)
        address = re.sub(r"^[^台臺]{1,2}(中市|南市|北市)", r"台\1", address)
        address = fix_address_words(address)
    else:
        flags.append("⚠️地址未讀到")

    # --- billing period (本期計費用水期間, the only date RANGE on the page) ---
    period = ""
    m = next((FET_PERIOD_RE.search(f.text) for f in frags
              if FET_PERIOD_RE.search(f.text)), None)
    if m:
        period = f"{m.group(1)}-{m.group(2)}"
    else:
        flags.append("⚠️計費期間未讀到")

    # --- carrier serial + printed invoice number ------------------------------
    invoice = ym = ""
    for f in frags:
        m = LONG_CARRIER_RE.search(f.text.replace(" ", "").upper())
        if m:
            ym = m.group(1)
            invoice = normalize_serial(m.group(2), notes)
            break
    if not invoice:
        flags.append("⚠️發票號碼(載具流水碼)未讀到")
    printed = next((m.group(1) for f in frags
                    for m in [re.search(r"發票號碼[:：]?([A-Z]{2}\d{8})",
                                        f.text.replace(" ", "").upper())] if m), "")
    if not printed:  # label and value may land in separate fragments
        for lab in labels("發票號"):
            for f in frags:
                if abs(f.y - lab.y) < 0.008 and 0 < f.x - lab.x < 0.3 \
                        and re.fullmatch(r"[A-Z]{2}\d{8}", f.text.strip().upper()):
                    printed = f.text.strip().upper()
                    break
    if printed:
        notes.append(f"帳面發票號碼{printed}")

    ubn = find_ubn(frags, notes, flags)

    if None not in (fee_sub, coll_sub):
        notes.insert(0, f"水費小計{fee_sub:g}元+代收費用{coll_sub:g}元")

    return [{
        "_frags": frags,
        "_tax_status": "derived" if tax_derived or tax_mismatch else ("source" if tax is not None else "missing"),
        "_water_gross": fee_sub,
        "type": "水費",
        "usage": "" if usage is None else usage,
        "total": "" if total is None else f"{total:,.0f}",
        "tax": "" if tax is None else f"{tax:,.0f}",
        "call": "",
        "address": address, "period": period, "number": number,
        "ubn": ubn, "ym": ym,
        "invoice": invoice, "notes": ";".join(notes + flags),
        "_flags": flags,
        "_anchors": {"addr": [addr_frag.y, addr_frag.x] if addr_frag else None,
                     "tou": None},
    }]


MANUAL_NOTE = "⚠️非台電/台水版面,金額等欄位請以 view_image 工具人工判讀"
# 遠傳電信月租方案:月租 599 - 優惠 100;帳單總額超出 499 的部分視為通話費。
# 方案變更時請更新這兩個常數。
FET_MONTHLY, FET_DISCOUNT = 599, 100


def parse_other(frags, page_text):
    """Non-utility page (telecom etc.): emit minimal rows for human review.

    Telecom (FET) sheets carry one stub per phone number; the page is tiled
    into columns like the electricity stubs, and 本期金額 / 載具流水碼 /
    billing period are pre-filled per stub where they read cleanly. The rows
    keep the manual-review flag either way.
    """
    is_fet = "遠傳" in page_text or "FET" in page_text
    if is_fet:
        kind = "電信費"
    elif "自來水" in page_text:
        kind = "水費"
    else:
        kind = "其他(待人工判讀)"

    insts = defaultdict(list)
    for f in frags:
        for m in MOBILE_RE.finditer(f.text):
            insts[m.group(1)].append(f)

    m = FET_PERIOD_RE.search(page_text)
    page_period = f"{m.group(1)}-{m.group(2)}" if m else ""
    if not insts:
        return [{
            "_frags": frags,
            "type": kind, "usage": "", "total": "", "tax": "", "call": "",
            "address": "", "period": page_period, "number": "",
            "ubn": FET_UBN if is_fet else "", "ym": "", "invoice": "",
            "notes": MANUAL_NOTE,
        }]

    # tile the page into columns, one per phone number (same layout rule as
    # the electricity stubs)
    metas = sorted(({"no": no, "x": min(f.x for f in lst)} for no, lst in insts.items()),
                   key=lambda mt: mt["x"])
    ncols = len(metas)
    stubs = defaultdict(list)
    for f in frags:
        stubs[min(int(f.x * ncols), ncols - 1)].append(f)

    rows = []
    for meta in metas:
        sf = stubs.get(min(int(meta["x"] * ncols), ncols - 1)) or frags
        notes, fflags = [], []

        def numeric(f):
            t = f.text.strip().replace("，", ",")
            m = re.fullmatch(r"[\$＄]?\s*(-?[\d,]+(?:\.\d{1,2})?)", t)
            try:
                return num(m.group(1)) if m else None
            except ValueError:
                return None

        # 本期金額 is printed 2-3 times per stub (top summary / bottom line /
        # 本月使用總覽 first row) — collect every copy and reconcile
        cands = []
        for lab in (f for f in sf if "本期金額" in f.text):
            for f in sf:
                v = numeric(f)
                if v is None or abs(f.x - lab.x) > 0.6:
                    continue
                if abs(f.y - lab.y) <= 0.012 or 0 < lab.y - f.y <= 0.035:
                    cands.append(v)
        for lab in (f for f in sf if "月租費及國" in f.text):
            for f in sf:
                v = numeric(f)
                if v is not None and abs(f.y - lab.y) <= 0.01 and abs(f.x - lab.x) < 0.25:
                    cands.append(v)

        # line items: the subtotal right below a 【…】 block header
        def block_subtotal(header_sub):
            hdr = next((f for f in sf if header_sub in f.text and "【" in f.text), None)
            if hdr is None:
                return None
            labs = [l for l in sf if "小計" in l.text and 0 < hdr.y - l.y < 0.08]
            labs.sort(key=lambda l: hdr.y - l.y)
            for lab in labs:
                m = re.search(r"小計\s*[\$＄]?([\d,]+(?:\.\d{1,2})?)", lab.text)
                if m:
                    return num(m.group(1))
                for f in sf:
                    v = numeric(f)
                    if v is not None and abs(f.y - lab.y) <= 0.008 \
                            and 0 < f.x - lab.x < 0.3:
                        return v
            return None

        rent = block_subtotal("月租費") if is_fet else None
        call_sub = block_subtotal("通話費") if is_fet else None
        plan = disc = None
        for f in sf:
            m = re.search(r"([45]G)\S{0,8}?(\d{3})月租費", f.text)
            if m:
                plan = f"{m.group(1)}月租{m.group(2)}"
                break
        for f in sf:
            m = re.search(r"月租費優惠\s*(\d{1,3})\s*元", f.text)
            if m:
                disc = int(m.group(1))
                break

        # reconcile the total: prefer the copy matching 月租小計+通話小計
        expected = None
        if rent is not None:
            expected = round(rent + (call_sub or 0), 2)
        amount = None
        if cands:
            if expected is not None:
                close = sorted((c for c in cands if abs(c - expected) <= 1),
                               key=lambda c: abs(c - expected))
                amount = close[0] if close else None
            if amount is None:
                amount = max(set(cands), key=cands.count)
        elif expected is not None:
            amount = expected
            fflags.append("⚠️本期金額未直接讀到,由月租+通話小計推得")

        invoice = ym = ""
        for f in sf:
            if not re.search(r"[載戴]具|發票", f.text):
                continue
            mm = re.search(r"(1\d{4})([A-Z0-9]{10})", f.text.replace(" ", "").upper())
            if mm:
                ym = mm.group(1)
                invoice = normalize_serial(mm.group(2), notes)
                break
        mm = next((FET_PERIOD_RE.search(f.text) for f in sf
                   if FET_PERIOD_RE.search(f.text)), None)
        period = f"{mm.group(1)}-{mm.group(2)}" if mm else page_period

        fmt = lambda v: f"{v:,.2f}".rstrip("0").rstrip(".")
        call = None
        if is_fet and amount is not None:
            if rent is not None:
                call = round(amount - rent, 2)
            else:
                # 帳面明細沒讀到,退回預設方案常數(方案變更時請更新常數)
                call = round(amount - (FET_MONTHLY - FET_DISCOUNT), 2)
                fflags.append(f"⚠️月租小計未讀到,通話費依預設方案"
                              f"{FET_MONTHLY}-{FET_DISCOUNT}推算,請人工確認")
            if call < 0:
                fflags.append(f"⚠️通話費計算為負({call:g}),版面請人工判讀")
                call = None
        if is_fet:
            desc = "遠傳電信綜合帳單"
            if plan is not None and rent is not None:
                desc += ";" + plan + (f"-優惠{disc}" if disc else "(無月租優惠)")
                if call is not None:
                    desc += f"+通話{call:g}"
                    if call_sub is not None and abs(call - call_sub) > 0.005:
                        desc += f"(通話明細{call_sub:g},總額四捨五入)"
            else:
                desc += "(人工判讀)"
                if amount is not None and call is not None:
                    desc += (f";月租{FET_MONTHLY}-優惠{FET_DISCOUNT}"
                             f"+通話{call:g}(預設方案)")
                if not fflags:
                    fflags.append("⚠️帳面明細未完整讀到,請人工判讀")
            notes.append(desc)
            if amount is None:
                fflags.append("⚠️本期金額未讀到")
        else:
            notes.append(MANUAL_NOTE)
        rows.append({
            "_frags": sf,
            "type": kind, "usage": "",
            "total": "" if amount is None else fmt(amount),
            "tax": "", "call": "" if call is None else f"{call:g}",
            "address": "", "period": period,
            "number": meta["no"],
            "ubn": FET_UBN if is_fet else "", "ym": ym, "invoice": invoice,
            "notes": ";".join(notes + fflags),
            "_flags": fflags,
        })
    return rows


def main(argv=None, ocr_backend=None):
    import json
    from batch_support import CSV_COLUMNS, atomic_json, atomic_text
    from import_fields import extract_import_fields
    ap = argparse.ArgumentParser()
    ap.add_argument("--source")
    ap.add_argument("--refine", metavar="PDF_DIR")
    ap.add_argument("--flags-json")
    ap.add_argument("--details-json")
    ap.add_argument("--review-json")
    ap.add_argument("-o", "--out")
    ap.add_argument("files", nargs="+")
    args = ap.parse_args(argv)
    pages = []
    for path in args.files:
        page, frags, embedded, rot = load_page(path)
        if embedded or not frags or page < 1:
            raise ValueError(f"{path}：缺少有效座標 OCR；請執行 run_batch.py，未輸出部分 CSV")
        source = args.source or re.sub(r"_p\d+$", "", Path(path).stem) + ".pdf"
        pages.append((source, page, frags, rot))
    if len({(x[0], x[1]) for x in pages}) != len(pages):
        raise ValueError("同一 PDF 頁面被重複傳入")
    pages.sort(key=lambda p: (p[0], p[1]))
    records, dropped, page_reports = [], [], []
    seen_numbers = defaultdict(list)
    for source, page, frags, rot in pages:
        refiner = Refiner(Path(args.refine) / source, page, rot, ocr_backend) if args.refine else None
        page_text = "".join(f.text for f in frags)
        seen = defaultdict(list)
        for f in frags:
            for m in ELEC_NO_RE.finditer(f.text):
                seen[m.group(1)].append(f)
        # the 檢核碼 barcode (1009 + the 11 電號 digits) restores a 電號 the
        # OCR garbled everywhere else on the page
        for f in frags:
            m = re.fullmatch(r"1009(\d{2})(\d{2})(\d{4})(\d{2})(\d)",
                             f.text.replace(" ", ""))
            if m:
                seen["-".join(m.groups())].append(f)
        # every bill prints its number 2-4 times and OCR occasionally corrupts
        # the final check digit; merge variants that share the leading groups,
        # keeping the most frequently seen spelling
        by_prefix = defaultdict(list)
        for no, insts in seen.items():
            by_prefix[no.rsplit("-", 1)[0]].append((no, insts))
        anchors = []
        for variants in by_prefix.values():
            canonical = max(variants, key=lambda v: len(v[1]))[0]
            for _, insts in variants:
                for inst in insts:
                    anchors.append((canonical, inst))

        if anchors:
            rows = parse_electricity(frags, anchors, refiner)
        elif "自來水" in page_text or "自来水" in page_text:
            rows = parse_water(frags)
        else:
            rows = parse_other(frags, page_text)
        page_report = {"source": source, "page": page, "parsed": len(rows), "kept": 0, "duplicates": 0}
        if not rows:
            raise ValueError(f"{source} p{page}：未產生帳單或待確認列")
        for r in rows:
            r["number"] = r["number"].replace("-", "")
            r["source"], r["page"], r["rotation"] = source, page, rot
            collection_date, collection_source, collection_evidence, collection_flags = extract_collection_date(r.get("_frags", []))
            r["receipt_date"] = collection_date
            r["_collection_evidence"] = collection_evidence
            r.setdefault("_flags", []).extend(collection_flags)
            if collection_source == "繳費日期":
                r["notes"] += ";未見明確收款章日期,收款日期取自繳費日期"
            if collection_flags:
                r["notes"] += ";" + ";".join(collection_flags)
            if refiner and refiner.errors:
                r["notes"] += ";" + ";".join(refiner.errors)
            r["detail"], r["import_flags"] = extract_import_fields(r)
            printed = r["detail"].get("printed_invoice")
            if printed and "帳面發票號碼" not in r["notes"]:
                r["notes"] += ";帳面發票號碼" + printed
            # Missing identity fields or differing evidence can never prove a rescan.
            fingerprint = tuple(str(r.get(k, "")) for k in
                ("type", "number", "period", "receipt_date", "total", "invoice", "ym", "tax", "ubn", "usage", "address", "call"))
            evidence = json.dumps(r["detail"], ensure_ascii=False, sort_keys=True)
            required = all(r.get(k) for k in ("number", "period", "total", "invoice", "ubn"))
            previous = seen_numbers[r["number"]] if r["number"] else []
            same = next((old for old in previous if required and old["fingerprint"] == fingerprint
                         and old["evidence"] == evidence and old["notes"] == r["notes"]), None)
            if same:
                dropped.append({"source": source, "page": page, "number": r["number"],
                    "kept_source": same["source"], "kept_page": same["page"],
                    "reason": "發票號碼、帳單關鍵欄位與辨識證據一致"})
                page_report["duplicates"] += 1
                continue
            if previous:
                message = "⚠️同號碼帳單的發票／期間／金額或其他欄位有差異，保留衝突列，須核對後匯出"
                for item in [*previous, r]:
                    if message not in item["notes"]:
                        item["notes"] += ";" + message
            r["fingerprint"], r["evidence"] = fingerprint, evidence
            seen_numbers[r["number"]].append(r)
            records.append(r)
            page_report["kept"] += 1
        page_reports.append(page_report)
    csv_rows, review, details = [], [], {}
    counts = {n: len(items) for n, items in seen_numbers.items()}
    for r in records:
        location = f"{r['source']} p{r['page']}"
        csv_rows.append([location, r["type"], r["usage"], r["total"], r["tax"], r.get("call", ""),
            r["address"], r["period"], r.get("receipt_date", ""), r["number"], r.get("ubn", ""), r.get("ym", ""), r["invoice"], r["notes"].strip(";")])
        flags = [x for x in r["notes"].split(";") if "⚠️" in x]
        if args.details_json or args.review_json:
            flags.extend(r["import_flags"])
        detail = dict(r["detail"])
        detail["evidence"] = location + "; " + detail.get("evidence", "")
        if r["number"] and counts[r["number"]] == 1:
            details[r["number"]] = detail
        if flags:
            review.append({"source": r["source"], "page": r["page"], "rotation": r["rotation"],
                "number": r["number"], "type": r["type"], "flags": list(dict.fromkeys(flags)),
                "addr_anchor": r.get("_anchors", {}).get("addr"),
                "tou_anchor": r.get("_anchors", {}).get("tou"),
                "full_page": bool(r["import_flags"] or any("稅" in x or "發票" in x or "號碼" in x for x in flags)),
                "detail_candidates": detail})
    import io
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    writer.writerows(csv_rows)
    if args.out:
        atomic_text(Path(args.out), buffer.getvalue(), encoding="utf-8-sig")
    else:
        sys.stdout.write(buffer.getvalue())
    if args.flags_json:
        atomic_json(Path(args.flags_json), review)
    if args.details_json:
        atomic_json(Path(args.details_json), details)
    report = {"pages": page_reports, "rows": len(records), "dropped_duplicates": dropped, "review": review}
    if args.review_json:
        atomic_json(Path(args.review_json), report)
    print(f"已解析 {len(records)} 筆；{len(review)} 筆待確認；略過 {len(dropped)} 筆已確認重掃", file=sys.stderr)
    return report


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as error:
        print(f"解析失敗：{error}", file=sys.stderr)
        sys.exit(1)
