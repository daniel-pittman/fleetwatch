"""Tests for the rich-format document converter (fleetwatch/memory/doc_corpus.py).

Builds small synthetic fixtures in a temp dir (pandoc, python-pptx, openpyxl, soffice, the
standard library) and checks each converter, the row cap, the column denylist, the same-stem
skip, failure handling and the manifest. Exits 1 on any failure.

    python tests/indexer_checks/doc_formats.py (or through python -m unittest discover -s tests)

The fixtures and checks need three programs on PATH. When one is missing the check prints one
`skip:` line per missing program and exits SKIP_EXIT, which the unittest wrapper reports as a
skip; with FLEETWATCH_REQUIRE_DOC_TOOLS set (the CI legs that install them) a missing program
is a failure instead.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from email.message import EmailMessage
from pathlib import Path

from fleetwatch.memory import doc_corpus as dc  # noqa: E402
from fleetwatch.memory.privacy_scan import RuleSet  # noqa: E402

FAILS = []
SKIP_EXIT = 77
TOOLS = (("pandoc", "builds the docx, odt, rtf and epub fixtures"),
         ("soffice", "LibreOffice: the ods, xls, odp, ppt and doc fixtures and formula recalculation"),
         ("tesseract", "OCR of an image"))


def missing_tools():
    return [(name, why) for name, why in TOOLS if not shutil.which(name)]


def check(name, cond, detail=""):
    print("%s  %s%s" % ("ok  " if cond else "FAIL", name, "" if cond else "  " + detail))
    if not cond:
        FAILS.append(name)


class StubScanner:
    version = "stub-1"

    def scan_groups(self, groups, triage=None, chunk=100):
        return [(g, {}, 0) for g in groups]


def pandoc(src_md, dest):
    subprocess.run(["pandoc", str(src_md), "-o", str(dest)], check=True, capture_output=True)


def soffice(src, fmt, outdir, profile):
    subprocess.run(["soffice", "--headless", "--norestore", "-env:UserInstallation=file://%s" % profile,
                    "--convert-to", fmt, "--outdir", str(outdir), str(src)],
                   check=True, capture_output=True, timeout=180)


# The column denylist the refresh driver composes with the default packs (core + education).
PACK_COLUMNS = dc.DEFAULT_OPTS["column_denylist"] + RuleSet().column_denylist


def run(root, state, **kw):
    opts = dict(kw.pop("opts", None) or {})
    opts.setdefault("column_denylist", PACK_COLUMNS)
    kw["opts"] = opts
    spec = dict(path=str(root), include=["**/*"], label="t")
    corpus = state / "corpus"
    res = dc.convert_docs([spec], corpus, state / "docs-manifest.json", StubScanner(), **kw)
    return res, corpus / "t"


def build_fixtures(root, tmp):
    from openpyxl import Workbook
    from pptx import Presentation
    md = tmp / "src.md"
    md.write_text("# Handbook\n\nIntro paragraph about zebrafish.\n\n## Policies\n\n- first item\n"
                  "- second item\n\n| A | B |\n|---|---|\n| 1 | 2 |\n")
    for ext in ("docx", "odt", "rtf", "epub"):
        src = tmp / ("src_%s.md" % ext)      # distinct text, so the duplicate-text rule keeps each
        src.write_text(md.read_text() + "\nVariant %s.\n" % ext)
        pandoc(src, root / ("guide." + ext))
    (root / "page.html").write_text("<html><body><h1>Page title</h1><p>Hello quokka.</p></body></html>")
    for ext in ("pptx", "odp", "ppt"):
        prs = Presentation()
        s = prs.slides.add_slide(prs.slide_layouts[1])
        s.shapes.title.text = "Launch plan"
        s.placeholders[1].text = "Body bullet pelican " + ext
        s.notes_slide.notes_text_frame.text = "Speaker note walrus"
        prs.slides.add_slide(prs.slide_layouts[5]).shapes.title.text = "Second slide"
        prs.save(str(tmp / ("deck_" + ext + ".pptx")))
    (tmp / "deck_pptx.pptx").rename(root / "deck.pptx")
    wb = Workbook()
    ws = wb.active
    ws.title = "Roster"
    ws.append(["Student ID", "Email", "Course", "Score"])
    for i in range(1, 8):
        ws.append(["s%d" % i, "x%d@example.org" % i, "CS%d" % (100 + i), i * 10])
    big = wb.create_sheet("Big")
    big.append(["Item", "Count"])
    for i in range(1, 41):
        big.append(["thing%d %s" % (i, "x" * 60), i])
    wb.save(str(root / "book.xlsx"))
    (root / "table.csv").write_text("Topic,Phone,Value\nalpha,555-0100,1\nbeta,555-0101,2\n")
    (root / "table.tsv").write_text("Topic\tValue\ngamma\t3\n")
    nb = {"cells": [{"cell_type": "markdown", "source": ["# Notebook head\n", "text here"]},
                    {"cell_type": "code", "source": ["print('hi')"], "outputs": [{"text": "SECRETOUT"}]}],
          "metadata": {"language_info": {"name": "python"}}, "nbformat": 4, "nbformat_minor": 5}
    (root / "nb.ipynb").write_text(json.dumps(nb))
    (root / "conf.json").write_text('{"key": "value"}')
    (root / "conf.yaml").write_text("# comment\nkey: value\n")
    (root / "huge.json").write_text('{"a": "%s"}' % ("x" * (dc.DEFAULT_OPTS["fenced_max_bytes"] + 10)))
    m = EmailMessage()
    m["Subject"] = "Budget meeting"
    m["Date"] = "Mon, 1 Jan 2026 10:00:00 -0700"
    m["From"] = "Pat Example <pat@example.org>"
    m["To"] = "Sam Sample <sam@example.org>, other@example.org"
    m.set_content("Plain body about the budget.")
    m.add_attachment(b"ATTACH", maintype="application", subtype="octet-stream", filename="a.bin")
    (root / "mail.eml").write_bytes(bytes(m))
    (root / "binary.zip").write_bytes(b"PK\x03\x04")
    (root / "broken.docx").write_bytes(b"this is not a docx")
    # a compiled copy beside its source
    (root / "notes.md").write_text("# Notes\n\nsource text\n")
    pandoc(md, root / "notes.docx")


def main():
    gone = missing_tools()
    if gone:
        for name, why in gone:
            print("skip: %s is not on PATH (%s)" % (name, why))
        if os.environ.get("FLEETWATCH_REQUIRE_DOC_TOOLS"):
            print("FAIL  FLEETWATCH_REQUIRE_DOC_TOOLS is set, so every program above is required")
            return 1
        print("doc-format check skipped; install the programs above to run it")
        return SKIP_EXIT
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        root, state = tmp / "docs", tmp / "state"
        root.mkdir()
        state.mkdir()
        build_fixtures(root, tmp)
        md_text = (tmp / "src.md").read_text()
        # soffice-backed formats: ods, xls, odp, ppt, doc
        prof = state / "soffice-profile"
        (tmp / "conv").mkdir()
        soffice(root / "book.xlsx", "ods", root, prof)
        (root / "book.ods").rename(root / "sheet.ods")
        soffice(root / "book.xlsx", "xls", root, prof)
        (root / "book.xls").rename(root / "legacy.xls")
        soffice(tmp / "deck_odp.pptx", "odp", root, prof)
        (root / "deck_odp.odp").rename(root / "deck.odp")
        soffice(tmp / "deck_ppt.pptx", "ppt", root, prof)
        (root / "deck_ppt.ppt").rename(root / "deck.ppt")
        (tmp / "legacy.md").write_text(md_text + "\nVariant legacy doc.\n")
        pandoc(tmp / "legacy.md", tmp / "legacy.docx")
        soffice(tmp / "legacy.docx", "doc", root, prof)

        opts = dict(row_cap=20)
        res, out = run(root, state, opts=opts)
        read = lambda n: (out / (n + ".md")).read_text() if (out / (n + ".md")).exists() else ""

        for ext in ("docx", "odt", "rtf", "epub"):
            t = read("guide." + ext)
            check("%s: H1 stem, heading, list, table" % ext,
                  t.startswith("# guide") and "## Handbook" in t and "zebrafish" in t
                  and "first item" in t and "| 1" in t, t[:200])
        check("html", "Page title" in read("page.html") and "quokka" in read("page.html"))
        check("doc via soffice", "zebrafish" in read("legacy.doc") and "## " in read("legacy.doc"))
        for name in ("deck.pptx", "deck.odp", "deck.ppt"):
            t = read(name)
            check("%s: slide headings, body, notes" % name,
                  "## Slide 1: Launch plan" in t and "pelican" in t and "Notes:" in t
                  and "walrus" in t and "## Slide 2: Second slide" in t, t[:300])
        for name in ("book.xlsx", "sheet.ods", "legacy.xls"):
            t = read(name)
            check("%s: sheets and rows" % name,
                  "## Sheet Roster" in t and "## Sheet Big" in t and "CS103" in t and "thing1 " in t.replace("|", " "),
                  t[:300])
        t = read("book.xlsx")
        check("sheet over row_cap is capped with a note",
              "capped at 20 data rows" in t and "thing20" in t and "thing21" not in t)
        check("long sheet is chunked under ### Rows", "### Rows 1-" in t)
        check("denylisted columns dropped from xlsx",
              "Email" not in t and "Student ID" not in t and "x1@example.org" not in t
              and "Course" in t and "Score" in t)
        check("drop count reported by filename", res["dropped_columns"].get("book.xlsx") == 2,
              str(res["dropped_columns"]))
        t = read("table.csv")
        check("csv: header and values, Phone dropped",
              "Topic" in t and "alpha" in t and "Phone" not in t and "555-0100" not in t)
        check("tsv", "gamma" in read("table.tsv"))
        t = read("nb.ipynb")
        check("ipynb: markdown, fenced code, outputs dropped",
              "## Notebook head" in t and "```python\nprint('hi')\n```" in t and "SECRETOUT" not in t, t)
        check("json fenced", "```json" in read("conf.json") and '"key"' in read("conf.json"))
        check("yaml fenced", "```yaml" in read("conf.yaml"))
        check("fenced file over 2 MB skipped, not converted",
              not (out / "huge.json.md").exists()
              and any(n == "huge.json" for n, _ in res["skipped"]))
        t = read("mail.eml")
        check("eml: subject, names only, plain body, no attachment",
              "Subject: Budget meeting" in t and "From: Pat Example" in t and "Sam Sample" in t
              and "@" not in t and "Plain body" in t and "ATTACH" not in t, t)
        check("same-stem: docx beside notes.md skipped",
              not (out / "notes.docx.md").exists() and (out / "notes.md").exists()
              and any(n == "notes.docx" for n, _ in res["skipped"]))
        check("unsupported type skipped and listed by name",
              any(n == "binary.zip" for n, _ in res["skipped"]))
        check("failed conversion is counted and does not crash",
              any(e.startswith("broken.docx") for e in res["errors"]) or (out / "broken.docx.md").exists()
              or any(n == "broken.docx" for n, _ in res["skipped"]))
        check("by_type counts", res["by_type"].get(".xlsx") == 1 and res["by_type"].get(".docx", 0) >= 1,
              str(res["by_type"]))

        # a hard failure: a converter that raises is skipped and counted
        (root / "bad.pptx").write_bytes(b"not a pptx")
        res2, _ = run(root, state, opts=opts)
        check("bad pptx: error counted, listed in skipped",
              any(e.startswith("bad.pptx") for e in res2["errors"])
              and any(n == "bad.pptx" for n, _ in res2["skipped"]), str(res2["errors"]))
        check("run continued past the failure", res2["unchanged"] > 5)

        # manifest: an unchanged tree reconverts nothing
        res3, _ = run(root, state, opts=opts)
        check("manifest skips unchanged files", res3["changed"] == [] and res3["unchanged"] > 10,
              "changed=%d unchanged=%d" % (len(res3["changed"]), res3["unchanged"]))
        check("failed file is not retried while unchanged", res3["errors"] == [])

        # a changed file reconverts; a deleted one removes its output
        (root / "table.tsv").write_text("Topic\tValue\ndelta\t4\n")
        (root / "page.html").unlink()
        res4, out4 = run(root, state, opts=opts)
        check("changed file reconverted", len(res4["changed"]) == 1 and "delta" in read("table.tsv"))
        check("deleted source removes output", not (out4 / "page.html.md").exists())

        # same text under a second path is skipped
        sub = root / "copy"
        sub.mkdir()
        (sub / "table.csv").write_text((root / "table.csv").read_text())
        res5, out5 = run(root, state, opts=opts)
        check("byte-identical converted text under a second path skipped",
              not (out5 / "copy" / "table.csv.md").exists()
              and any(r == "same text as another file" for _, r in res5["skipped"]), str(res5["skipped"]))

        # options change forces a reconvert
        res6, _ = run(root, state, opts=dict(row_cap=5))
        check("changing doc_options reconverts", len(res6["changed"]) > 5)
        check("row_cap option honoured", "capped at 5 data rows" in read("book.xlsx"))

        # images are off unless ocr is on
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (400, 120), "white")
        ImageDraw.Draw(img).text((10, 40), "HELLO OCR", fill="black")
        img.save(root / "scan.png")
        res7, out7 = run(root, state, opts=dict(row_cap=5))
        check("images skipped with ocr off", not (out7 / "scan.png.md").exists())
        res7b, out7b = run(root, state, opts=dict(row_cap=5, ocr=True))
        t = (out7b / "scan.png.md").read_text() if (out7b / "scan.png.md").exists() else ""
        check("image OCR with ocr on", "HELLO" in t.upper(), t)
        # custom denylist
        res8, _ = run(root, state, opts=dict(row_cap=5, ocr=True, column_denylist=["score"]))
        t = read("book.xlsx")
        check("custom column_denylist replaces the default", "Score" not in t and "Email" in t)

        # column_keep wins over the denylist for the named column only
        res9, _ = run(root, state, opts=dict(row_cap=5, column_keep=["email"]))
        t = read("book.xlsx")
        check("column_keep keeps one denylisted column and still drops the others",
              "Email" in t and "Student ID" not in t and res9["dropped_columns"].get("book.xlsx") == 1,
              str(res9["dropped_columns"]))

    formula_tests()

    print("\n%d failure(s)" % len(FAILS) if FAILS else "\nall passed")
    return 1 if FAILS else 0


def formula_tests():
    """A workbook written by openpyxl has formulas and no cached values."""
    import os
    from openpyxl import Workbook
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        root, state = tmp / "docs", tmp / "state"
        root.mkdir()
        state.mkdir()
        wb = Workbook()
        ws = wb.active
        ws.title = "Calc"
        ws.append(["Base", "Doubled"])
        for i in (21, 34, 55):
            ws.append([i, "=A%d*2" % (ws.max_row + 1)])
        wb.save(root / "calc.xlsx")
        src = root / "calc.xlsx"
        before = (src.read_bytes(), os.stat(src).st_mtime_ns)
        check("uncached formulas detected", dc.uncached_formulas(src))
        res, out = run(root, state, opts=dict(row_cap=20))
        t = (out / "calc.xlsx.md").read_text() if (out / "calc.xlsx.md").exists() else ""
        check("formulas converted with computed values after recalculation",
              "42" in t and "68" in t and "110" in t, t)
        check("recalculation counted", res["recalculated"] == 1 and res["recalc_failed"] == 0,
              "%s %s" % (res["recalculated"], res["recalc_failed"]))
        check("source file bytes and mtime unchanged",
              before == (src.read_bytes(), os.stat(src).st_mtime_ns))

        # recalculation failure falls back to the uncached read and is counted
        state2 = tmp / "state2"
        state2.mkdir()
        orig = dc.soffice_convert

        def boom(*a, **k):
            raise RuntimeError("soffice failed (1)")
        dc.soffice_convert = boom
        try:
            res2, out2 = run(root, state2, opts=dict(row_cap=20))
        finally:
            dc.soffice_convert = orig
        t2 = (out2 / "calc.xlsx.md").read_text() if (out2 / "calc.xlsx.md").exists() else ""
        check("recalc failure falls back without crashing",
              res2["errors"] == [] and "Calc" in t2 and "42" not in t2 and res2["recalc_failed"] == 1,
              "%s %s" % (res2["errors"], res2["recalc_failed"]))
        check("source still unchanged after failure",
              before == (src.read_bytes(), os.stat(src).st_mtime_ns))

        # a workbook with no formulas is not recalculated
        plain = tmp / "plain"
        plain.mkdir()
        wb = Workbook()
        wb.active.append(["a", "b"])
        wb.active.append([1, 2])
        wb.save(plain / "p.xlsx")
        res3, _ = run(plain, tmp / "state3", opts=dict(row_cap=20))
        check("workbook without formulas is not recalculated",
              res3["recalculated"] == 0 and res3["recalc_failed"] == 0)


if __name__ == "__main__":
    sys.exit(main())
