"""Screenshots of `fleetwatch --demo` for the overview PDF, rendered by Textual itself.

Run from the repository root with the board's dependencies installed:

    FLEETWATCH_NO_MIGRATE=1 TZ=America/New_York python docs/figures/capture.py

It writes one SVG per view into docs/figures/ and converts each to PDF with rsvg-convert, then
outlines the text with Ghostscript (gs); both must be on the PATH. Every session in the pictures is the demo's fictional fleet in a temporary
folder; nothing reads the user's own files.
"""
import asyncio
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))

from fleetwatch import app as fapp, data, demo  # noqa: E402

# (file, (columns, rows), keys pressed after the first refresh). 120 columns is the hotkey window's
# width. The radar mark floats over the bottom right corner and needs 32 rows and 100 columns; a
# shorter board hides it, so only the Needs you view shows it.
VIEWS = (
    ("board-needs-you", (120, 32), "e"),
    ("board-todos", (120, 13), "2"),
    ("board-sessions", (120, 17), "3"),
    ("board-resume", (120, 14), "4 space"),
    ("board-indexes", (120, 14), "5"),
)
# Views that also become a PNG in docs/images, for the README.
PNGS = {"board-needs-you": "board", "board-indexes": "board-indexes"}


async def shoot(paths, name, size, keys):
    app = fapp.FleetwatchApp(lambda: fapp.gather(paths, data.parse_window("24h"), data.DEFAULT_HORIZON_DAYS),
                             3600, paths=paths)
    async with app.run_test(size=size) as pilot:
        await pilot.pause(1.5)
        for key in keys.split():
            await pilot.press(key)
            await pilot.pause(0.3)
        await pilot.pause(1.0)
        svg = app.export_screenshot(title="fleetwatch --demo")
    # librsvg drops the no-break spaces that open a text run unless whitespace is preserved,
    # which runs adjacent words together.
    svg = svg.replace('<svg class="rich-terminal"', '<svg xml:space="preserve" class="rich-terminal"', 1)
    path = os.path.join(HERE, name + ".svg")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(svg)
    return path


def main():
    tmp, paths = demo.prepare()
    try:
        for name, size, keys in VIEWS:
            svg = asyncio.run(shoot(paths, name, size, keys))
            raw = svg[:-4] + ".raw.pdf"
            subprocess.run(["rsvg-convert", "-f", "pdf", "-o", raw, svg], check=True)
            # The tagged overview must pass PDF/UA, and cairo's embedded CID fonts lack the
            # CIDToGIDMap it requires. The screenshot's text is drawn as outlines instead; the
            # figure's alt text carries what it says.
            subprocess.run(["gs", "-q", "-o", svg[:-4] + ".pdf", "-sDEVICE=pdfwrite", "-dNoOutputFonts", raw],
                           check=True)
            os.unlink(raw)
            if name in PNGS:
                png = os.path.join(HERE, "..", "images", PNGS[name] + ".png")
                subprocess.run(["rsvg-convert", "-w", "2223", "-o", png, svg], check=True)
                print("wrote", os.path.relpath(png))
            print("wrote", os.path.relpath(svg[:-4] + ".pdf"))
    finally:
        tmp.cleanup()


if __name__ == "__main__":
    main()
