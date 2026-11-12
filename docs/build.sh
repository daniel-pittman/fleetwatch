#!/bin/sh
# Builds docs/fleetwatch-overview.pdf as a tagged PDF/UA-2 file.
#
# Needs a TeX Live with LuaLaTeX and tagging support (2025 or newer; TinyTeX works) on the
# PATH, and tectonic for the architecture figure. The screenshots are committed; to retake
# them, run docs/figures/capture.py (it needs rsvg-convert).
#
# lualatex runs three times: the tagged build's last page is only fully tagged once the
# cross-references have settled.
set -eu
cd "$(dirname "$0")"
(cd figures && tectonic architecture.tex)
for pass in 1 2 3; do
  lualatex -interaction=nonstopmode -halt-on-error fleetwatch-overview.tex >/dev/null
done
rm -f fleetwatch-overview.aux fleetwatch-overview.out fleetwatch-overview.log
echo "built $(pwd)/fleetwatch-overview.pdf"
