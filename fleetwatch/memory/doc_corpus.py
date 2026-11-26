"""Reference-document converter for session indexes.

Turns a session's notes folders, design documents and key files into Markdown under the
index's corpus/docs/<label>/ tree, scrubbed by the shared privacy scanner. Incremental: a
manifest records each source's mtime, size and sha1, and only a changed file is converted.
A deleted source removes its corpus file.

    .md              copied (H1 added when the file has none)
    .yaml .yml .txt  headings escaped, paragraphs kept; output named X.yaml.md
    .tex             \\section{..} lines become headings; output named X.tex.md
    .pdf             pdftotext -layout, one "## Page N" per page; output named X.pdf.md
    rich formats     converted to Markdown (see RICH_EXTS below)
    anything else    skipped, counted and listed by filename in the run report

A PDF is skipped when a same-stem .tex, .md or .txt sits beside it (a compiled copy of a
source that is already indexed). node_modules, .git and dot-folders are never entered.

Spec (from registry.yaml):  {path: "~/dir", include: ["**/*.md"], label: "name"}
"""
import csv
import email
import email.policy
import email.utils
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

CONV_VERSION = 2          # bump when any converter's output changes; forces a full reconvert
TEXT_EXTS = {".md", ".txt", ".tex"}
FENCED_EXTS = {".json", ".yml", ".yaml", ".toml", ".xml"}
PANDOC_EXTS = {".docx", ".odt", ".rtf", ".html", ".htm", ".epub"}
SOFFICE_DOC_EXTS = {".doc"}
SLIDE_EXTS = {".pptx", ".odp", ".ppt"}
SHEET_EXTS = {".xlsx", ".xlsm", ".ods", ".xls"}
DELIM_EXTS = {".csv": ",", ".tsv": "\t"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif"}
RICH_EXTS = (PANDOC_EXTS | SOFFICE_DOC_EXTS | SLIDE_EXTS | SHEET_EXTS | set(DELIM_EXTS)
             | {".ipynb", ".eml"})
SAME_STEM_EXTS = RICH_EXTS | {".pdf"}
KNOWN_EXTS = TEXT_EXTS | FENCED_EXTS | RICH_EXTS | IMAGE_EXTS | {".pdf"}

DEFAULT_OPTS = dict(
    ocr=False,                 # OCR scanned PDFs and images
    ocr_page_cap=50,           # pages OCRed per PDF
    row_cap=500,               # data rows kept per sheet
    timeout=120,               # seconds per converter process
    fenced_max_bytes=2 * 1024 * 1024,
    column_keep=[],            # headers kept even when a column_denylist entry matches
    # The core list; the refresh driver adds the enabled rule packs' headers, or uses
    # settings.toml [docs] column_denylist when set.
    column_denylist=["ip", "ip address", "latitude", "longitude", "location", "email", "e-mail",
                     "name", "first name", "last name", "phone"],
)
CHUNK_CHARS = 1200         # a table chunk stays under the 1500-character search chunk
CELL_MAX = 300
SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv"}

# A docs entry never indexes a file below a folder whose name holds one of these words:
# identity, roster, grade and instructor-only folders hold student records. A folder name is
# split into words at punctuation, camelCase and letter/digit boundaries, and a word matches
# when it starts with one of these ("FinalGrades2026", "gradesheets"; not "upgrade" or
# "graduate"). A file name is split the same way and is never-index only when its first word
# starts with a record-shaped stem (FILE_WORDS: "roster.md", "Gradebook2026.md"), since notes
# are often named for such a topic ("grading-rubric.md", "feedback-on-grades.md"); the
# scanner's rule-pack `private_paths` apply to file and folder names alike.
# `discover` enforces both on every refresh, for the docs folder itself and every path below
# it, so a glob such as **/*.md cannot reach such a folder, and indexes built before the rule
# lose those files on their next refresh.
NEVER_INDEX_WORDS = ("roster", "grade", "grading", "identity", "identities", "instructor")
FILE_WORDS = ("roster", "grade", "identity", "instructor")

NAME_WORD_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")


def name_words(name):
    """The lowercase words of a folder name: split at every character that is not an ASCII
    letter or digit, at camelCase humps and at letter/digit boundaries."""
    return [w.lower() for piece in re.split(r"[^A-Za-z0-9]+", str(name))
            for w in NAME_WORD_RE.findall(piece)]


def folder_word(name):
    """The never-index word a folder name starts one of its words with, or None."""
    for word in name_words(name):
        for bad in NEVER_INDEX_WORDS:
            if word.startswith(bad):
                return bad
    return None


def file_word(name):
    """The record-shaped stem a file name's first word starts with, or None."""
    words = name_words(name)
    return next((bad for bad in FILE_WORDS if words and words[0].startswith(bad)), None)


def rule_hit(text, private_path_re):
    """The text a private_paths rule matches in `text`, or None. A zero-width match is a
    match too, reported by a fixed non-empty label so a caller's truth test sees it."""
    if private_path_re is None:
        return None
    m = private_path_re.search(str(text))
    if m is None:
        return None
    return m.group(0) or "a private_paths rule"


def never_index_hit(parts, private_path_re=None, is_file=True):
    """Why the path made of `parts` is never-index, or None. The words apply to its folder
    parts (every part when `is_file` is false), and FILE_WORDS to the first word of a file's
    name. A private_paths rule is tried against each
    part, each folder part followed by `/`, and the parts joined by `/` with and without a
    leading `/` (a folder ends in `/`), so a rule such as `exports/restricted/` matches."""
    parts = [str(x) for x in parts if str(x) not in ("", os.sep)]
    folders = parts if not is_file else parts[:-1]
    for f in folders:
        hit = folder_word(f)
        if hit:
            return hit
    if is_file and parts:
        hit = file_word(parts[-1])
        if hit:
            return hit
    if private_path_re is None:
        return None
    joined = "/".join(parts) + ("" if is_file else "/")
    for text in parts + [f + "/" for f in folders] + [joined, "/" + joined]:
        hit = rule_hit(text, private_path_re)
        if hit:
            return hit
    return None


def link_below(root, target):
    """The first symbolic link among the folders from `root` (not itself checked) down to
    `target` (checked), compared without resolving, or None. A target outside `root` is
    returned as it is, so a caller refuses it."""
    root, target = Path(os.path.abspath(root)), Path(os.path.abspath(target))
    if not target.is_relative_to(root):
        return target
    p = root
    for part in target.relative_to(root).parts:
        p = p / part
        if p.is_symlink():
            return p
    return None


def safe_corpus_path(corpus_dir, rel, root=None):
    """corpus_dir / rel, resolved, when it stays inside the corpus folder and no folder from
    `root` (default: the corpus folder's parent) down to it is a symbolic link. A linked corpus
    folder would let a refresh write into, or sweep, another index's corpus. Raises ValueError
    otherwise, before anything is written or deleted."""
    link = link_below(Path(corpus_dir).parent if root is None else root, Path(corpus_dir) / rel)
    if link is not None:
        raise ValueError("corpus path %r passes through the symbolic link %s; nothing is written "
                         "or removed through a link" % (str(rel), link))
    base = Path(corpus_dir).resolve()
    dest = (base / rel).resolve()
    if not dest.is_relative_to(base):
        raise ValueError("corpus path %r leaves the corpus folder" % str(rel))
    return dest


def check_label(label):
    """A docs label becomes the first folder of its corpus paths, so it is one plain path
    part: not empty, not . or .., no separator, not absolute."""
    text = str(label)
    if (not text or text in (".", "..") or "/" in text or "\\" in text or "\0" in text
            or os.path.isabs(text)):
        raise ValueError("docs label %r is not a single plain path part" % text)
    return text


class Skip(Exception):
    """A file that is deliberately not converted; the message is the counted reason."""


class NotSelected(ValueError):
    """convert_docs(only=PATH) named a file no docs entry of the index selects."""


def sha1_file(p):
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def escape_headings(text):
    return re.sub(r"(?m)^(#{1,6})(\s)", r"\\\1\2", text)


SECTION_RE = re.compile(r"^\\(chapter|section|subsection|subsubsection)\*?\{(.*)\}\s*$", re.M)
SECTION_LEVEL = {"chapter": "#", "section": "##", "subsection": "###", "subsubsection": "####"}


def tex_to_md(text):
    text = escape_headings(text)
    return SECTION_RE.sub(lambda m: "%s %s" % (SECTION_LEVEL[m.group(1)], m.group(2)), text)


def column_words(ws):
    """Spreadsheet headers and column_denylist / column_keep entries as the column filter
    compares them: lowercase, every run of punctuation or underscores one space, empties
    dropped."""
    out = (re.sub(r"[\W_]+", " ", str(w).lower()).strip() for w in ws or [])
    return [w for w in out if w]


class Ctx:
    """Per-run conversion context: options, the soffice profile directory, counters."""

    def __init__(self, opts=None, state_dir=None):
        self.opts = dict(DEFAULT_OPTS, **(opts or {}))
        self.state_dir = Path(state_dir) if state_dir else Path(tempfile.gettempdir())
        self.dropped = {}          # filename -> columns dropped by the denylist
        self.recalc_failed = 0     # workbooks whose formula recalculation failed
        self.recalculated = 0      # workbooks read from a recalculated temp copy

    def deny_re(self):
        words = column_words(self.opts["column_denylist"])
        if not words:
            return None
        alt = "|".join(re.escape(w) for w in sorted(set(words), key=len, reverse=True))
        return re.compile(r"(?<![a-z0-9])(?:%s)(?![a-z0-9])" % alt)

    def keep_re(self):
        words = column_words(self.opts.get("column_keep"))
        if not words:
            return None
        alt = "|".join(re.escape(w) for w in sorted(set(words), key=len, reverse=True))
        return re.compile(r"(?<![a-z0-9])(?:%s)(?![a-z0-9])" % alt)


def run_cmd(cmd, timeout, **kw):
    """Run a converter in its own process group and kill that group on timeout, so a hung
    soffice child dies with the process this run started and nothing else is touched."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True, **kw)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.communicate()
        raise RuntimeError("%s timed out after %ss" % (Path(cmd[0]).name, timeout))
    if proc.returncode != 0:
        raise RuntimeError("%s failed (%d)" % (Path(cmd[0]).name, proc.returncode))
    return out


def shift_headings(text, by=1):
    """Demote Markdown headings outside code fences so the file's own H1 stays the only H1."""
    out, fence = [], None
    for line in text.split("\n"):
        m = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if m:
            if fence is None:
                fence = m.group(1)[0]
            elif m.group(1)[0] == fence:
                fence = None
        elif fence is None:
            h = re.match(r"^(#{1,6})(\s.*)$", line)
            if h:
                line = "#" * min(6, len(h.group(1)) + by) + h.group(2)
        out.append(line)
    return "\n".join(out)


def fence(text, lang=""):
    ticks = "```"
    while ticks in text:
        ticks += "`"
    return "%s%s\n%s\n%s" % (ticks, lang, text.rstrip("\n"), ticks)


def titled(path, body):
    return "# %s\n\n%s" % (path.stem, body)


# ------------------------------------------------------------------ PDF and OCR

def pages_to_md(pages, label="Page"):
    out = []
    for i, pg in enumerate(pages, start=1):
        pg = re.sub(r"[ \t]{3,}", "  ", pg).strip()
        if pg:
            out.append("## %s %d\n\n%s" % (label, i, escape_headings(pg)))
    return "\n\n".join(out)


def pdf_pages(path, ctx):
    out = run_cmd(["pdftotext", "-layout", str(path), "-"], ctx.opts["timeout"])
    return out.decode("utf-8", "replace").split("\f")


def ocr_images(images, ctx):
    texts = []
    for img in images:
        out = run_cmd(["tesseract", str(img), "-", "-l", "eng"], ctx.opts["timeout"])
        texts.append(out.decode("utf-8", "replace"))
    return texts


def pdf_to_md(path, ctx=None, label="Page"):
    ctx = ctx or Ctx()
    pages = pdf_pages(path, ctx)
    if pages and not pages[-1].strip():
        pages = pages[:-1]
    chars = sum(len(p.strip()) for p in pages)
    if ctx.opts["ocr"] and chars < 40 * max(1, len(pages)):
        with tempfile.TemporaryDirectory() as td:
            run_cmd(["pdftoppm", "-r", "200", "-png", "-l", str(ctx.opts["ocr_page_cap"]),
                     str(path), str(Path(td) / "p")], ctx.opts["timeout"])
            imgs = sorted(Path(td).glob("p*.png"))
            pages = ocr_images(imgs, ctx)
    return pages_to_md(pages, label)


def image_to_md(path, ctx):
    text = ocr_images([path], ctx)[0].strip()
    if not text:
        raise Skip("image with no text")
    return titled(path, escape_headings(text))


# ------------------------------------------------------------------ soffice, pandoc

def soffice_convert(path, fmt, ctx, outdir):
    """Convert with a private soffice profile under the index's state/ so a running
    LibreOffice is never touched; returns the output path."""
    profile = ctx.state_dir / "soffice-profile"
    profile.mkdir(parents=True, exist_ok=True)
    run_cmd(["soffice", "--headless", "--norestore", "-env:UserInstallation=file://%s" % profile,
             "--convert-to", fmt, "--outdir", str(outdir), str(path)], ctx.opts["timeout"])
    out = Path(outdir) / (path.stem + "." + fmt.split(":")[0])
    if not out.exists():
        raise RuntimeError("soffice produced no %s" % fmt)
    return out


def pandoc_md(path, ctx, reader=None):
    cmd = ["pandoc", "-t", "gfm-raw_html", "--wrap=none", "--shift-heading-level-by=1"]
    if reader:
        cmd += ["-f", reader]
    return run_cmd(cmd + [str(path)], ctx.opts["timeout"]).decode("utf-8", "replace")


def textutil_txt(path, ctx):
    """macOS's textutil, the fallback for a .doc pandoc cannot read; elsewhere the file
    counts as a failed conversion."""
    if sys.platform != "darwin":
        raise RuntimeError("textutil is only on macOS")
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "o.txt"
        run_cmd(["textutil", "-convert", "txt", "-output", str(out), str(path)], ctx.opts["timeout"])
        return escape_headings(out.read_text(encoding="utf-8", errors="replace"))


def doc_to_md(path, ctx):
    ext = path.suffix.lower()
    with tempfile.TemporaryDirectory() as td:
        src = path
        try:
            if ext == ".doc":
                src = soffice_convert(path, "docx", ctx, td)
            text = pandoc_md(src, ctx)
        except RuntimeError:
            text = textutil_txt(path, ctx)
    return titled(path, text)


def html_to_md(path, ctx):
    return titled(path, pandoc_md(path, ctx))


# ------------------------------------------------------------------ slides

def shape_texts(shape, skip_title=None):
    out = []
    if shape.shape_type == 6:                      # group
        for s in shape.shapes:
            out += shape_texts(s, skip_title)
    elif getattr(shape, "has_table", False) and shape.has_table:
        for row in shape.table.rows:
            cells = [c.text.strip().replace("\n", " ") for c in row.cells]
            if any(cells):
                out.append(" | ".join(cells))
    elif shape.has_text_frame and shape is not skip_title:
        t = shape.text_frame.text.strip()
        if t:
            out.append(t)
    return out


def pptx_to_md(path, ctx):
    from pptx import Presentation
    prs = Presentation(str(path))
    out = []
    for n, slide in enumerate(prs.slides, start=1):
        title_shape = slide.shapes.title
        title = (title_shape.text_frame.text.strip().replace("\n", " ") if title_shape is not None
                 and title_shape.has_text_frame else "")
        head = "## Slide %d: %s" % (n, title) if title else "## Slide %d" % n
        body = []
        for sh in slide.shapes:
            body += shape_texts(sh, title_shape)
        part = [head]
        if body:
            part.append(escape_headings("\n\n".join(body)))
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text.strip() \
                if slide.notes_slide.notes_text_frame is not None else ""
            if notes:
                part.append("Notes:\n\n" + escape_headings(notes))
        out.append("\n\n".join(part))
    return titled(path, "\n\n".join(out))


def slides_to_md(path, ctx):
    if path.suffix.lower() == ".pptx":
        return pptx_to_md(path, ctx)
    with tempfile.TemporaryDirectory() as td:
        try:
            return pptx_to_md(soffice_convert(path, "pptx", ctx, td), ctx)
        except Exception:
            pdf = soffice_convert(path, "pdf", ctx, td)
            return titled(path, pdf_to_md(pdf, ctx, label="Slide"))


# ------------------------------------------------------------------ spreadsheets

def cell_text(v):
    if v is None:
        return ""
    if isinstance(v, float) and v == int(v) and abs(v) < 1e15:
        v = int(v)
    t = str(v).replace("\r", " ").replace("\n", " ").replace("|", "\\|").strip()
    return t[:CELL_MAX] + "..." if len(t) > CELL_MAX else t


def table_chunks(header, rows, first_row_no):
    """Yield (start, end, markdown) chunks, each under CHUNK_CHARS, repeating the header."""
    head = "| " + " | ".join(header) + " |\n|" + "---|" * len(header) + "\n"
    cur, size, start = [], len(head), first_row_no
    for i, r in enumerate(rows):
        line = "| " + " | ".join(r) + " |\n"
        if cur and size + len(line) > CHUNK_CHARS:
            n = len(cur)
            yield start, start + n - 1, head + "".join(cur)
            cur, size, start = [], len(head), start + n
        cur.append(line)
        size += len(line)
    if cur:
        yield start, start + len(cur) - 1, head + "".join(cur)


def sheet_md(name, rows_iter, fname, ctx):
    """rows_iter yields lists of raw cell values; the first non-empty row is the header."""
    cap = ctx.opts["row_cap"]
    deny = ctx.deny_re()
    keep_cols = ctx.keep_re()
    header, keep, rows, capped = None, None, [], False
    for raw in rows_iter:
        vals = [cell_text(v) for v in raw]
        if not any(vals):
            continue
        if header is None:
            norm = [re.sub(r"[\W_]+", " ", h.lower()).strip() for h in vals]
            drop = {i for i, h in enumerate(norm)
                    if deny and deny.search(h) and not (keep_cols and keep_cols.search(h))}
            if drop:
                ctx.dropped[fname] = ctx.dropped.get(fname, 0) + len(drop)
            width = len(vals)
            keep = [i for i in range(width) if i not in drop]
            header = [vals[i] or "col%d" % (i + 1) for i in keep]
            continue
        if len(rows) >= cap:
            capped = True
            break
        rows.append([vals[i] if i < len(vals) else "" for i in keep])
    if header is None or not keep:
        return ""
    out = ["## Sheet %s" % escape_headings(name).replace("\n", " ")]
    chunks = list(table_chunks(header, rows, 1)) if rows else []
    if not chunks:
        out.append("(no data rows)")
    elif len(chunks) == 1:
        out.append(chunks[0][2].rstrip())
    else:
        for a, b, md in chunks:
            out.append("### Rows %d-%d\n\n%s" % (a, b, md.rstrip()))
    if capped:
        out.append("(Sheet capped at %d data rows; later rows are not indexed.)" % cap)
    return "\n\n".join(out)


def uncached_formulas(path):
    """True when a sheet holds formula cells and every cached value is None (a workbook last
    saved by openpyxl or another writer that never computed them)."""
    import openpyxl
    wf = openpyxl.load_workbook(str(path), read_only=True)
    wv = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    try:
        for sf, sv in zip(wf.worksheets, wv.worksheets):
            formulas = cached = 0
            for rf, rv in zip(sf.iter_rows(values_only=True), sv.iter_rows(values_only=True)):
                for a, b in zip(rf, rv):
                    if isinstance(a, str) and a.startswith("="):
                        formulas += 1
                        cached += b is not None
            if formulas and not cached:
                return True
        return False
    finally:
        wf.close()
        wv.close()


def xlsx_sheets(path, fname, ctx):
    import openpyxl
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        try:
            need = uncached_formulas(path)
        except Exception:
            need = False
        if need:
            # Recalculate a temp copy; the source file is never opened for writing.
            try:
                with tempfile.TemporaryDirectory() as td:
                    src = Path(td) / "in" / ("src" + path.suffix.lower())
                    src.parent.mkdir()
                    (Path(td) / "out").mkdir()
                    shutil.copyfile(path, src)
                    out = soffice_convert(src, "xlsx", ctx, Path(td) / "out")
                    parts = _xlsx_parts(out, fname, ctx)
                ctx.recalculated += 1
                return parts
            except Exception:
                ctx.recalc_failed += 1
    return _xlsx_parts(path, fname, ctx)


def _xlsx_parts(path, fname, ctx):
    import openpyxl
    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    try:
        parts = []
        for ws in wb.worksheets:
            md = sheet_md(ws.title, ws.iter_rows(values_only=True), fname, ctx)
            if md:
                parts.append(md)
        return parts
    finally:
        wb.close()


def sheets_to_md(path, ctx):
    ext = path.suffix.lower()
    if ext in DELIM_EXTS:
        csv.field_size_limit(1 << 24)
        with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
            md = sheet_md(path.stem, csv.reader(f, delimiter=DELIM_EXTS[ext]), path.name, ctx)
        parts = [md] if md else []
    elif ext in (".xlsx", ".xlsm"):
        parts = xlsx_sheets(path, path.name, ctx)
    else:
        with tempfile.TemporaryDirectory() as td:
            parts = xlsx_sheets(soffice_convert(path, "xlsx", ctx, td), path.name, ctx)
    return titled(path, "\n\n".join(parts)) if parts else titled(path, "(no data)")


# ------------------------------------------------------------------ notebooks, data, mail

def ipynb_to_md(path, ctx):
    nb = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    lang = ((nb.get("metadata") or {}).get("language_info") or {}).get("name") or "python"
    out = []
    for c in nb.get("cells", []):
        src = c.get("source", "")
        src = "".join(src) if isinstance(src, list) else src
        if not src.strip():
            continue
        if c.get("cell_type") == "markdown":
            out.append(shift_headings(src.strip()))
        elif c.get("cell_type") == "code":
            out.append(fence(src, lang))
    return titled(path, "\n\n".join(out))


def fenced_to_md(path, ctx):
    if path.stat().st_size > ctx.opts["fenced_max_bytes"]:
        raise Skip("over size limit")
    text = path.read_text(encoding="utf-8", errors="replace")
    return titled(path, fence(text, path.suffix.lower().lstrip(".")))


def display_names(value):
    names = []
    for name, _addr in email.utils.getaddresses([str(value or "")]):
        names.append(name.strip() or "(unnamed)")
    return ", ".join(names)


def eml_to_md(path, ctx):
    with open(path, "rb") as f:
        msg = email.message_from_binary_file(f, policy=email.policy.default)
    lines = []
    if msg["Subject"]:
        lines.append("Subject: %s" % msg["Subject"])
    if msg["Date"]:
        lines.append("Date: %s" % msg["Date"])
    for h in ("From", "To"):
        if msg[h]:
            lines.append("%s: %s" % (h, display_names(msg[h])))
    body = msg.get_body(preferencelist=("plain",))
    if body is not None:
        text = body.get_content()
    else:
        html = msg.get_body(preferencelist=("html",))
        text = ""
        if html is not None:
            with tempfile.TemporaryDirectory() as td:
                hp = Path(td) / "m.html"
                hp.write_text(html.get_content(), encoding="utf-8")
                text = pandoc_md(hp, ctx)
    return titled(path, escape_headings("\n".join(lines)) + "\n\n" + escape_headings(text.strip()))


# ------------------------------------------------------------------ dispatch

def to_markdown(path, ctx=None):
    ctx = ctx or Ctx()
    ext = path.suffix.lower()
    if ext == ".pdf":
        return pdf_to_md(path, ctx)
    if ext in IMAGE_EXTS:
        return image_to_md(path, ctx)
    if ext in (".docx", ".odt", ".rtf", ".doc"):
        return doc_to_md(path, ctx)
    if ext in (".html", ".htm", ".epub"):
        return html_to_md(path, ctx)
    if ext in SLIDE_EXTS:
        return slides_to_md(path, ctx)
    if ext in SHEET_EXTS or ext in DELIM_EXTS:
        return sheets_to_md(path, ctx)
    if ext == ".ipynb":
        return ipynb_to_md(path, ctx)
    if ext in FENCED_EXTS:
        return fenced_to_md(path, ctx)
    if ext == ".eml":
        return eml_to_md(path, ctx)
    text = path.read_text(encoding="utf-8", errors="replace")
    if ext == ".md":
        return text
    if ext == ".tex":
        return tex_to_md(text)
    return escape_headings(text)


def hydrate(path):
    """Read one byte so a OneDrive or Google Drive placeholder downloads; False on failure."""
    try:
        with open(path, "rb") as f:
            f.read(1)
        return True
    except OSError:
        return False


H1_RE = r"(?m)^# \S"


def out_rel(label, rel):
    name = rel.name if rel.suffix.lower() == ".md" else rel.name + ".md"
    return Path(label) / rel.parent / name


NAME_SEP_RE = re.compile(r"[_.]+")


def manifest_key(path):
    """The manifest names a source by a hash of its path, so its file and folder names are
    never stored in state; the source itself is found again by discovery, not by this key."""
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:32]


def is_manifest_key(k):
    return len(k) == 32 and all(c in "0123456789abcdef" for c in k)


def scrub_names(scanner, comps):
    """{component: (spelling, cats, drop)} for the labels, folder names and file stems in
    `comps`. File and folder names are text the corpus path, the title, the search index and
    every report would otherwise carry, so they take the same scan as document text. A name
    is read twice: as written, which the secret, email, id and denylist rules need; then in
    a filename context, with underscores and dots read as spaces and the first pass's
    placeholders in place, so a name beside an id ("Ann Lee 900123456") is seen as a name
    once the id reads [id]. A name with nothing to scrub keeps its exact spelling."""
    comps = sorted(set(comps))
    if not comps:
        return {}
    first = scanner.scan_groups([[c] for c in comps])
    spaced = [NAME_SEP_RE.sub(" ", f[0]).strip() or f[0] for f, _c, _d in first]
    second = scanner.scan_groups([[t] for t in spaced])
    out = {}
    for c, (f1, c1, d1), sp, (f2, c2, d2) in zip(comps, first, spaced, second):
        cats = {}
        for part in (c1, c2):
            for k, n in part.items():
                if not k.startswith("kept-"):   # keep counts describe the body's allowlist use
                    cats[k] = cats.get(k, 0) + n
        spelling = f2[0] if f2[0] != sp else f1[0]
        out[c] = (spelling, cats, d1 or d2)
    return out


def name_parts(rel):
    """(stem, suffix) of a file name. Only a known document type's suffix is a suffix; any
    other text after the last dot ("Report.Lastname") is part of the name and is scanned."""
    if rel.suffix.lower() in KNOWN_EXTS:
        return rel.stem, rel.suffix
    return rel.name, ""


def corpus_names(scanner, found):
    """For each discovered source {key: (path, label, rel)}, its corpus path (relative to the
    corpus folder), the scrubbed spelling of its file name for reports, the categories its
    names were scrubbed under, and a drop reason when a name pairs an anonymous id with a
    person. Names with nothing to scrub claim their paths first, so they keep their exact
    spelling; a path already claimed (compared without case, as macOS file systems do) gets
    a counter, in key order."""
    comps = set()
    for p, label, rel in found.values():
        comps.add(label)
        comps.update(rel.parent.parts)
        comps.add(name_parts(rel)[0])
    names = scrub_names(scanner, comps)
    plans = {}
    for key in sorted(found):
        p, label, rel = found[key]
        stem, suffix = name_parts(rel)
        parts = [label] + list(rel.parent.parts) + [stem]
        cats, drop, changed = {}, None, False
        spelled = []
        for c in parts:
            sp, cc, dd = names[c]
            spelled.append(sp)
            changed = changed or sp != c
            drop = drop or dd
            for k, n in cc.items():
                cats[k] = cats.get(k, 0) + n
        plans[key] = (changed, spelled, suffix, cats, drop)
    out, taken = {}, set()
    for key in sorted(plans, key=lambda k: (plans[k][0], k)):
        changed, spelled, suffix, cats, drop = plans[key]
        stem, n = spelled[-1], 1
        dest = out_rel(spelled[0], Path(*spelled[1:-1], stem + suffix))
        while str(dest).lower() in taken:
            n += 1
            dest = out_rel(spelled[0], Path(*spelled[1:-1], "%s (%d)%s" % (stem, n, suffix)))
        taken.add(str(dest).lower())
        out[key] = dict(out=dest, name=stem + suffix, title=stem, cats=cats, drop=drop)
    return out


def has_sibling_source(p):
    """A compiled or exported copy beside its source: the source is already indexed."""
    exts = (".tex", ".md", ".txt") if p.suffix.lower() == ".pdf" else (".md", ".txt")
    return any((p.with_suffix(e)).exists() for e in exts)


def supported_exts(opts):
    exts = TEXT_EXTS | FENCED_EXTS | RICH_EXTS | {".pdf"}
    return exts | IMAGE_EXTS if opts.get("ocr") else exts


def discover(spec, opts=None, skipped=None, private_path_re=None, never=None):
    """Yield (source Path, label, relative Path) for every file the spec selects. A selected
    file that is not converted is appended to `skipped` as ((source, label, relative), reason);
    convert_docs reports it under its scrubbed name. A file whose full path, as listed or as
    a link resolves, is never-index (never_index_hit: the words on its folders, which include
    the docs folder and the folders above it, and `private_path_re`) is not yielded; each one
    is appended to `never` (a list) when given, so the caller can report the count."""
    opts = dict(DEFAULT_OPTS, **(opts or {}))
    exts = supported_exts(opts)
    base = Path(os.path.expanduser(spec["path"]))
    label = check_label(spec.get("label") or base.name)
    seen = set()
    if not base.exists():
        return
    root = base.resolve()
    for pattern in spec.get("include") or ["**/*.md"]:
        for p in sorted(base.glob(pattern)):
            if not p.is_file() or p in seen:
                continue
            rel = p.relative_to(base)
            if any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts):
                continue
            seen.add(p)
            # A link to a file outside the folder would index text the folder's audience was
            # never given (a cloned repo or an unpacked archive can carry such links).
            real = p.resolve()
            if not real.is_relative_to(root):
                if skipped is not None:
                    skipped.append(((p, label, rel), "symlink outside the folder"))
                continue
            # The check covers the path as listed and the path the link resolves to, each
            # from the file system root, so the docs folder itself and a root link are checked.
            if (never_index_hit(base.absolute().parts + rel.parts, private_path_re)
                    or never_index_hit(real.parts, private_path_re)):
                if never is not None:
                    never.append(p)
                continue
            ext = p.suffix.lower()
            if ext not in exts:
                if skipped is not None:
                    skipped.append(((p, label, rel), "unsupported type"))
                continue
            if ext in SAME_STEM_EXTS and has_sibling_source(p):
                if skipped is not None:
                    skipped.append(((p, label, rel), "same-stem source beside it"))
                continue
            yield p, label, rel


def options_key(opts):
    return hashlib.sha1(json.dumps([CONV_VERSION, opts], sort_keys=True).encode()).hexdigest()[:12]


def convert_docs(specs, corpus_dir, manifest_path, scanner, only=None, full=False, opts=None,
                 root=None):
    """Convert changed documents. `only` (a path) limits the run to that one source file and
    skips the deletion sweep. Returns dict(changed, removed, unchanged, errors, categories,
    skipped, by_type, dropped_columns, dropped). `opts` overrides DEFAULT_OPTS (the index's
    doc_options).

    File and folder names are scrubbed like text (scrub_names): the corpus path, an untitled
    document's title and every name in the result use the scrubbed spelling, and the
    manifest keys sources by a hash of their path. `changed`, `dropped` and the corpus side of
    `removed` are corpus paths; `skipped` and `errors` name files by their scrubbed names.

    A document the scanner drops (an anonymous id paired with a name, in its text or in its
    names) is never written: its manifest entry records the reason as `skip`, it is listed in
    `skipped` and `dropped`, its scan categories are counted, and an earlier copy of it in the
    corpus is removed.

    `root` is the index's data folder (default: the corpus folder's parent). When the corpus
    folder or a folder between `root` and it is a symbolic link, nothing is converted, written
    or swept (ValueError)."""
    root = Path(corpus_dir).parent if root is None else Path(root)
    link = link_below(root, corpus_dir)
    if link is not None:
        raise ValueError("the corpus folder %s passes through the symbolic link %s; nothing was "
                         "converted, written or removed" % (corpus_dir, link))
    ctx = Ctx(opts, manifest_path.parent)
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    stored_version = manifest.get("_scan_version")
    conv_key = options_key(ctx.opts)
    # Files scrubbed or converted under older rules are all reconverted. A one-file run under
    # new rules converts its file afresh and leaves the stored version, so the next full run
    # still reconverts the rest.
    rules_changed = stored_version != scanner.version or manifest.get("_conv") != conv_key
    if only is None and rules_changed:
        full = True
    found_skipped = []
    files = {}
    private_re = getattr(getattr(scanner, "rules", None), "private_path_re", None)
    never = []
    for spec in specs:
        for p, label, rel in discover(spec, ctx.opts, found_skipped, private_re, never):
            files[manifest_key(p)] = (p, label, rel)
    named = corpus_names(scanner, dict(files, **{manifest_key(f[0]): f for f, _w in found_skipped}))
    skipped = [(named[manifest_key(f[0])]["name"], why) for f, why in found_skipped]
    stored = manifest.get("files", {})
    # A manifest written before names were scrubbed is keyed by source path; it is rekeyed
    # here so no source path stays in state.
    stored = {(k if is_manifest_key(k) else manifest_key(k)): e for k, e in stored.items()}
    entries = {} if full or rules_changed else stored
    new_entries = dict(stored) if only else {}
    res = dict(changed=[], removed=[], unchanged=0, errors=[], categories={}, skipped=skipped,
               by_type={}, dropped_columns=ctx.dropped, recalculated=0, recalc_failed=0,
               dropped=[], never_index=len(never))
    targets = files
    if only:
        want = str(Path(os.path.expanduser(only)).resolve())
        targets = {k: v for k, v in files.items() if str(v[0].resolve()) == want}
        if not targets:
            raise NotSelected("%s is not selected by any docs entry of this index" % only)
    todo = []
    for key, (p, label, rel) in targets.items():
        st = p.stat()
        nm = named[key]
        old = entries.get(key)
        # An entry whose corpus file sits under another spelling (written before names were
        # scrubbed, or before a scan rule changed its name) is converted again, so the old
        # file is swept and its title rewritten.
        if old and old.get("out") and old["out"] != str(nm["out"]):
            old = None
        if old and nm["drop"] and not old.get("skip"):
            old = None      # its name now drops it; convert so the drop removes its file
        if old and old["mtime"] == st.st_mtime and old["size"] == st.st_size:
            new_entries[key] = old
            res["unchanged"] += 1
            if old.get("skip"):
                skipped.append((nm["name"], old["skip"]))
            continue
        if not hydrate(p):
            skipped.append((nm["name"], "cloud-only file not available"))
            if key in entries:
                new_entries[key] = entries[key]
            continue
        sha = sha1_file(p)
        if old and old["sha1"] == sha:
            new_entries[key] = dict(old, mtime=st.st_mtime, size=st.st_size)
            res["unchanged"] += 1
            if old.get("skip"):
                skipped.append((nm["name"], old["skip"]))
            continue
        todo.append((key, p, label, rel, st, sha))
    texts, kept = [], []
    for key, p, label, rel, st, sha in todo:
        nm = named[key]
        try:
            md = to_markdown(p, ctx)
        except Skip as e:
            skipped.append((nm["name"], str(e)))
            new_entries[key] = dict(mtime=st.st_mtime, size=st.st_size, sha1=sha, out=None, skip=str(e))
            continue
        except Exception as e:
            res["errors"].append("%s: %s" % (nm["name"], type(e).__name__))
            skipped.append((nm["name"], "conversion failed"))
            new_entries[key] = dict(mtime=st.st_mtime, size=st.st_size, sha1=sha, out=None,
                                    skip="conversion failed")
            continue
        if not md.strip():
            md = "(empty)"
        texts.append(md)
        kept.append((key, p, label, rel, st, sha))
    if texts:
        seen_text = {e["text_sha"]: k for k, e in new_entries.items() if e.get("text_sha") and k in files}
        scanned = scanner.scan_groups([[t] for t in texts])
        for (key, p, label, rel, st, sha), (fields, cats, drop) in zip(kept, scanned):
            nm = named[key]
            out = str(nm["out"])
            # A document with no H1 gets its scrubbed file stem as one.
            untitled = not re.search(H1_RE, fields[0].rstrip() + "\n")
            cats = dict(cats)
            for c, n in nm["cats"].items():
                cats[c] = cats.get(c, 0) + n
            drop = drop or nm["drop"]
            if drop:
                why = "dropped: %s" % drop
                skipped.append((nm["name"], why))
                res["dropped"].append(out)
                for c, n in cats.items():
                    res["categories"][c] = res["categories"].get(c, 0) + n
                old_out = (stored.get(key) or {}).get("out")
                new_entries[key] = dict(mtime=st.st_mtime, size=st.st_size, sha1=sha, out=None,
                                        scan=cats, skip=why)
                if old_out and safe_corpus_path(corpus_dir, old_out, root).exists():
                    safe_corpus_path(corpus_dir, old_out, root).unlink()
                    res["removed"].append(old_out)
                continue
            body = fields[0].rstrip() + "\n"
            if untitled:
                body = "# %s\n\n%s" % (nm["title"], body)
            tsha = hashlib.sha1(body.encode("utf-8")).hexdigest()
            if p.suffix.lower() != ".md" and seen_text.get(tsha, key) != key:
                skipped.append((nm["name"], "same text as another file"))
                new_entries[key] = dict(mtime=st.st_mtime, size=st.st_size, sha1=sha, out=None,
                                        skip="same text as another file")
                continue
            seen_text[tsha] = key
            dest = safe_corpus_path(corpus_dir, out, root)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(body)
            # A one-file run skips the sweep, so this file's copy under its old spelling (an
            # older rule left more of its name) is removed here.
            old_out = (stored.get(key) or {}).get("out")
            if (only and old_out and old_out != out and safe_corpus_path(corpus_dir, old_out, root).exists()
                    and not any(e.get("out") == old_out for k, e in new_entries.items() if k != key)):
                safe_corpus_path(corpus_dir, old_out, root).unlink()
                res["removed"].append(old_out)
            new_entries[key] = dict(mtime=st.st_mtime, size=st.st_size, sha1=sha,
                                    out=out, scan=cats, text_sha=tsha)
            res["changed"].append(out)
            ext = p.suffix.lower()
            res["by_type"][ext] = res["by_type"].get(ext, 0) + 1
            for c, n in cats.items():
                res["categories"][c] = res["categories"].get(c, 0) + n
    if only is None:
        wanted = {e["out"] for k, e in new_entries.items() if k in files and e.get("out")}
        for k, e in list(new_entries.items()):
            if k not in files:
                res["removed"].append(e.get("out") or k)
                del new_entries[k]
        if corpus_dir.exists():
            for p in corpus_dir.rglob("*.md"):
                rp = str(p.relative_to(corpus_dir))
                if rp not in wanted:
                    p.unlink()
                    if rp not in res["removed"]:
                        res["removed"].append(rp)
    res["recalculated"], res["recalc_failed"] = ctx.recalculated, ctx.recalc_failed
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(dict(_scan_version=stored_version if only else scanner.version,
                                             _conv=manifest.get("_conv") if only else conv_key,
                                             files=new_entries),
                                        indent=1))
    return res
