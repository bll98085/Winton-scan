import Foundation
import PDFKit
import Vision
import AppKit

// OCR a scanned PDF page with macOS Vision, printing one line per recognized
// text fragment with normalized coordinates (origin bottom-left):
//   [y0.123 x0.45] text
//
// Usage:
//   swift ocr.swift <pdf> --count                 print page count
//   swift ocr.swift <pdf> <page>                  OCR one page (tries embedded
//                                                 text first, then rotations
//                                                 0/90/270 and keeps the best)
//   swift ocr.swift <pdf> <page> --force-ocr      skip the embedded-text probe
//   swift ocr.swift <pdf> <page> --rot 270 --region 0.5,0.6,1.0,0.75 --scale 10
//                                                 re-OCR a small region at high
//                                                 DPI; region is x0,y0,x1,y1 in
//                                                 the rotated page's normalized
//                                                 coordinates (bottom-left
//                                                 origin, same space as the
//                                                 full-page output). Printed
//                                                 coordinates are mapped back
//                                                 to full-page space.

let args = CommandLine.arguments
guard args.count >= 3 else {
    fputs("usage: ocr <pdf> --count | <pdf> <page> [--force-ocr] [--rot 0|90|270] [--scale N] [--region x0,y0,x1,y1]\n", stderr)
    exit(1)
}
guard let doc = PDFDocument(url: URL(fileURLWithPath: args[1])) else {
    fputs("cannot open PDF: \(args[1])\n", stderr)
    exit(1)
}
if args[2] == "--count" {
    print(doc.pageCount)
    exit(0)
}
guard let pageNum = Int(args[2]), let page = doc.page(at: pageNum - 1) else {
    fputs("bad page number\n", stderr)
    exit(1)
}

var scale: CGFloat = 3.0
var forcedRot: Int? = nil
var region: [CGFloat]? = nil
var forceOCR = false
var i = 3
while i < args.count {
    switch args[i] {
    case "--scale": i += 1; scale = CGFloat(Double(args[i]) ?? 3.0)
    case "--rot": i += 1; forcedRot = Int(args[i])
    case "--force-ocr": forceOCR = true
    case "--region":
        i += 1
        region = args[i].split(separator: ",").compactMap { CGFloat(Double($0) ?? -1) }
        if region!.count != 4 { fputs("bad --region\n", stderr); exit(1) }
    default: fputs("unknown arg \(args[i])\n", stderr); exit(1)
    }
    i += 1
}

// Embedded-text probe: scanned pages have no text layer, born-digital PDFs do.
if !forceOCR && region == nil {
    if let text = page.string, text.filter({ !$0.isWhitespace }).count > 100 {
        print("== page \(pageNum) embedded-text ==")
        print(text)
        exit(0)
    }
}

let bounds = page.bounds(for: .mediaBox)

// cropNorm (optional) renders only that sub-rectangle of the rotated page,
// given in the rotated page's normalized coordinates with bottom-left origin —
// so high --scale values never allocate a full-page bitmap.
func render(rotationDegrees: Int, cropNorm: CGRect? = nil) -> CGImage? {
    let size = CGSize(width: bounds.width * scale, height: bounds.height * scale)
    let w = (rotationDegrees % 180 == 0) ? Int(size.width) : Int(size.height)
    let h = (rotationDegrees % 180 == 0) ? Int(size.height) : Int(size.width)
    var outW = w, outH = h
    var offX: CGFloat = 0, offY: CGFloat = 0
    if let c = cropNorm {
        offX = c.origin.x * CGFloat(w)
        offY = c.origin.y * CGFloat(h)
        outW = max(1, Int(c.width * CGFloat(w)))
        outH = max(1, Int(c.height * CGFloat(h)))
    }
    guard let ctx = CGContext(data: nil, width: outW, height: outH, bitsPerComponent: 8,
                              bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(),
                              bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return nil }
    ctx.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
    ctx.fill(CGRect(x: 0, y: 0, width: outW, height: outH))
    ctx.saveGState()
    ctx.translateBy(x: -offX, y: -offY)
    switch rotationDegrees {
    case 90:
        ctx.translateBy(x: 0, y: CGFloat(h)); ctx.rotate(by: -.pi / 2)
    case 180:
        ctx.translateBy(x: CGFloat(w), y: CGFloat(h)); ctx.rotate(by: .pi)
    case 270:
        ctx.translateBy(x: CGFloat(w), y: 0); ctx.rotate(by: .pi / 2)
    default: break
    }
    ctx.scaleBy(x: scale, y: scale)
    ctx.translateBy(x: -bounds.origin.x, y: -bounds.origin.y)
    page.draw(with: .mediaBox, to: ctx)
    ctx.restoreGState()
    return ctx.makeImage()
}

struct Line { let y: CGFloat; let x: CGFloat; let w: CGFloat; let h: CGFloat; let text: String }

func ocr(_ img: CGImage) -> [Line] {
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.recognitionLanguages = ["zh-Hant", "en-US"]
    req.usesLanguageCorrection = false
    let handler = VNImageRequestHandler(cgImage: img, options: [:])
    try? handler.perform([req])
    var out: [Line] = []
    for obs in req.results ?? [] {
        guard let cand = obs.topCandidates(1).first else { continue }
        let b = obs.boundingBox
        out.append(Line(y: b.midY, x: b.minX,
                        w: b.width * CGFloat(img.width), h: b.height * CGFloat(img.height),
                        text: cand.string))
    }
    return out
}

// Vision reads text even when the page is rendered 90° off, so raw character
// count cannot pick the right rotation. Only in the correct orientation do
// long text runs come back as WIDE boxes — score those.
func orientationScore(_ lines: [Line]) -> Int {
    lines.reduce(0) { acc, l in
        (l.text.count >= 6 && l.w > 1.5 * l.h) ? acc + l.text.count : acc
    }
}

func emit(_ lines: [Line], rot: Int, header: String) {
    print("== page \(pageNum) rotation \(rot) \(header) ==")
    for l in lines.sorted(by: { $0.y == $1.y ? $0.x < $1.x : $0.y > $1.y }) {
        print(String(format: "[y%.3f x%.2f] %@", l.y, l.x, l.text))
    }
}

if let r = region {
    let rot = forcedRot ?? 0
    let cropNorm = CGRect(x: r[0], y: r[1], width: r[2] - r[0], height: r[3] - r[1])
    guard let sub = render(rotationDegrees: rot, cropNorm: cropNorm) else {
        fputs("crop render failed\n", stderr); exit(1)
    }
    // map crop-local coordinates back to full-page space
    let mapped = ocr(sub).map {
        Line(y: r[1] + $0.y * (r[3] - r[1]), x: r[0] + $0.x * (r[2] - r[0]),
             w: $0.w, h: $0.h, text: $0.text)
    }
    emit(mapped, rot: rot, header: String(format: "region %.2f,%.2f,%.2f,%.2f scale %.0f", r[0], r[1], r[2], r[3], scale))
    exit(0)
}

if let rot = forcedRot {
    guard let img = render(rotationDegrees: rot) else { exit(1) }
    emit(ocr(img), rot: rot, header: "forced")
    exit(0)
}

var best: (score: Int, rot: Int, lines: [Line]) = (-1, 0, [])
for rot in [0, 90, 270] {
    guard let img = render(rotationDegrees: rot) else { continue }
    let lines = ocr(img)
    let score = orientationScore(lines)
    if score > best.score { best = (score, rot, lines) }
}

// Vision reads 180°-flipped text just as well as upright text, so the wide-box
// score cannot tell a rotation from its flip and the emitted coordinate frame
// may be upside-down. Known page-header strings must sit in the TOP half; when
// their median y lands in the bottom half, re-render flipped and keep that.
let flipAnchors = ["台灣電力公司", "TAIWAN POWER", "綜合帳單", "台灣自來水",
                   "自來水公司", "繳費通知單", "Electricity Bill"]
let anchorYs = best.lines
    .filter { l in flipAnchors.contains { l.text.contains($0) } }
    .map { $0.y }
    .sorted()
if !anchorYs.isEmpty && anchorYs[anchorYs.count / 2] < 0.5 {
    let flipped = (best.rot + 180) % 360
    if let img = render(rotationDegrees: flipped) {
        best = (best.score, flipped, ocr(img))
    }
}
emit(best.lines, rot: best.rot, header: "score \(best.score)")
