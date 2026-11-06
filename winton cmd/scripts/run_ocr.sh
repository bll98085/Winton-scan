#!/bin/bash
# OCR every page of a scanned bill PDF into per-page text files.
#
#   run_ocr.sh <pdf> <outdir>
#
# Produces <outdir>/<pdf-basename>_pN.txt for every page N.
# Compiles the Vision OCR tool once and caches the binary next to this script.
set -euo pipefail

PDF="$1"
OUTDIR="$2"
DIR="$(cd "$(dirname "$0")" && pwd)"
BIN="$DIR/ocrbin"

if [[ ! -x "$BIN" || "$DIR/ocr.swift" -nt "$BIN" ]]; then
  echo "compiling ocr.swift..." >&2
  swiftc -O "$DIR/ocr.swift" -o "$BIN"
fi

mkdir -p "$OUTDIR"
BASE="$(basename "$PDF" .pdf)"
PAGES="$("$BIN" "$PDF" --count)"
echo "$PDF: $PAGES pages" >&2
for ((p = 1; p <= PAGES; p++)); do
  "$BIN" "$PDF" "$p" > "$OUTDIR/${BASE}_p${p}.txt"
  echo "  page $p done" >&2
done
