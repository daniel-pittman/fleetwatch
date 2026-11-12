"""The radar mark: a scope with rings, a sweep line and blips, drawn from code.

Braille cells give a 9x5 cell mark on an 18x20 dot grid. One blip is amber while something needs the
user and teal otherwise; every other mark is teal. The sweep steps through STEPS angles, one per call.
Nothing here touches the app, so the output is the same wherever it is drawn.
"""
import math

TEAL = "#96dcc8"
RING = "#4f8f7c"
AMBER = "#f5b950"
STEPS = 8
COLS, ROWS = 9, 5
GLYPH_UTF8, GLYPH_ASCII = "◉", "*"

_W, _H = COLS * 2, ROWS * 4
_CX, _CY = (_W - 1) / 2, (_H - 1) / 2
_RINGS = (8.6, 4.6)
# Braille dot bits by (dx, dy) inside a cell.
_BIT = {(0, 0): 0x01, (0, 1): 0x02, (0, 2): 0x04, (1, 0): 0x08, (1, 1): 0x10, (1, 2): 0x20,
        (0, 3): 0x40, (1, 3): 0x80}
# Top-left dot of each 2x2 blip. The first is the one that turns amber.
_ALERT_BLIP = (12, 4)
_CALM_BLIPS = ((4, 5), (11, 13), (3, 14))


def _angle(step):
    return 2 * math.pi * (step % STEPS) / STEPS


def _on_sweep(x, y, step):
    dx, dy = x - _CX, y - _CY
    ux, uy = math.sin(_angle(step)), -math.cos(_angle(step))      # 0 is straight up, turning clockwise
    along = dx * ux + dy * uy
    return 0 <= along <= _RINGS[0] and abs(dx * uy - dy * ux) < 0.51


def _ring_dot(x, y):
    d = math.hypot(x - _CX, y - _CY)
    return d < 0.9 or any(abs(d - r) < 0.55 for r in _RINGS)


def _blip_dots(top_left):
    bx, by = top_left
    return {(bx + i, by + j) for i in (0, 1) for j in (0, 1)}


def rows(alert, step=0):
    """The braille mark: five rows of nine (char, color) pairs. A blank cell is a space."""
    alert_dots = _blip_dots(_ALERT_BLIP)
    alert_cell = (_ALERT_BLIP[0] // 2, _ALERT_BLIP[1] // 4)
    calm = set().union(*(_blip_dots(b) for b in _CALM_BLIPS))
    out = []
    for r in range(ROWS):
        line = []
        for c in range(COLS):
            bits, sweep = 0, False
            for (dx, dy), bit in _BIT.items():
                x, y = c * 2 + dx, r * 4 + dy
                if (c, r) == alert_cell:
                    on = (x, y) in alert_dots
                else:
                    hit = _on_sweep(x, y, step)
                    sweep = sweep or hit
                    on = hit or (x, y) in calm or _ring_dot(x, y)
                bits |= bit if on else 0
            if not bits:
                line.append((" ", None))
            elif (c, r) == alert_cell:
                line.append((chr(0x2800 + bits), AMBER if alert else TEAL))
            else:
                line.append((chr(0x2800 + bits), TEAL if sweep else RING))
        out.append(line)
    return out


def _cell_art(alert, step):
    """The plain ASCII mark, one character per cell."""
    blip = (6, 1)
    out = []
    ux, uy = math.sin(_angle(step)), -math.cos(_angle(step))
    slash = {0: "|", 1: "/", 2: "-", 3: "\\"}[step % 4]
    for r in range(ROWS):
        line = []
        for c in range(COLS):
            x, y = (c - COLS // 2) / (COLS // 2), (r - ROWS // 2) / (ROWS // 2)
            d = math.hypot(x, y)
            if (c, r) == blip:
                line.append(("o", AMBER if alert else TEAL))
            elif (c, r) == (COLS // 2, ROWS // 2):
                line.append(("+", TEAL))
            elif 0 < d <= 1.05 and 0 <= x * ux + y * uy and abs(x * uy - y * ux) < 0.2:
                line.append((slash, TEAL))
            elif abs(d - 1) < 0.3:
                line.append((".", RING))
            else:
                line.append((" ", None))
        out.append(line)
    return out


def ascii_rows(alert, step=0):
    return _cell_art(alert, step)


def glyph(alert, utf8=True):
    """One coloured character for a terminal too short for the mark."""
    return (GLYPH_UTF8 if utf8 else GLYPH_ASCII), (AMBER if alert else TEAL)


def mark(alert, step=0, utf8=True):
    return rows(alert, step) if utf8 else ascii_rows(alert, step)


def plain(cells):
    return "\n".join("".join(ch for ch, _ in line).rstrip() for line in cells)
