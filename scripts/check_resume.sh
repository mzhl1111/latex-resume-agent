#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: bash scripts/check_resume.sh path/to/resume.tex" >&2
  exit 2
fi

TEX="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
DIR="$(dirname "$TEX")"
BASE="$(basename "$TEX" .tex)"

cd "$DIR"
pdflatex -interaction=nonstopmode -halt-on-error "$BASE.tex" >/tmp/resume_compile.log

PAGES=$(pdfinfo "$BASE.pdf" | awk '/^Pages:/ {print $2}')
if [[ "$PAGES" != "1" ]]; then
  echo "FAIL: expected 1 page, got $PAGES" >&2
  exit 1
fi

pdftoppm -png -f 1 -singlefile -r 160 "$BASE.pdf" "$BASE" >/dev/null 2>&1

echo "OK: $BASE.pdf is 1 page"
echo "Preview: $DIR/$BASE.png"
