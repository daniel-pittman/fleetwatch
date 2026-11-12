"""Hybrid search engine for local text indexes, as a library.

An index is a directory holding search_config.yaml, and optionally synonyms.yaml and
probes.yaml. `Index(dir)` loads one; `build(ix)`, `search(ix, query, opts)` and
`evaluate(ix, opts)` return data and never print or exit. Errors are raised as
fleetwatch.search.errors.SearchError subclasses. The command line lives in fleetwatch.search.cli.

Query syntax is SQLite FTS5: bare words are ANDed, "quoted words" is a phrase,
and NOT / OR work as operators.
"""
import dataclasses
import hashlib
import json
import os
import re
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

try:
    import numpy as np
except ImportError:
    np = None

from .errors import (ConfigError, EmbedderUnavailable, EmbeddingFailed, EmptyCorpus,
                     IndexMissing, IndexStale, ModelMismatch, NoProbesYet, ProbesInvalid,
                     ProbesMissing)

# Bumped whenever the SQLite schema or the stored vector format changes, so an index
# built by an older copy of this tool is reported as stale instead of failing somewhere
# confusing. Borrowed from zenhub-cli's CACHE_VERSION.
INDEX_VERSION = "3"

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
AREA_RE = re.compile(r"^(\d+[a-z]?)\s*[.):]")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]+")
# Stripped out of a query before highlighting and before TF-IDF scoring.
FTS_OPERATORS = {"and", "or", "not", "near"}
STOPWORDS = {
    "the", "and", "for", "are", "but", "not", "you", "all", "any", "can", "had", "her",
    "was", "one", "our", "out", "has", "him", "his", "how", "its", "may", "new", "now",
    "old", "see", "two", "who", "did", "get", "use", "way", "she", "they", "them", "this",
    "that", "with", "from", "have", "been", "were", "will", "would", "there", "their",
    "what", "when", "which", "into", "than", "then", "some", "such", "only", "other",
    "more", "most", "also", "each", "does", "made", "make", "must", "over", "your",
}


# --------------------------------------------------------------------------- config

CHUNKING_DEFAULTS = {"max_chars": 1500, "min_chars": 25}
SIMILARITY_DEFAULTS = {"model": "sentence-transformers/all-MiniLM-L6-v2", "window_chars": 1200,
                       "window_overlap": 300, "max_features": 20000, "rrf_k": 60}

def load_config(index_dir):
    """Read an index directory's search_config.yaml and resolve it (see resolve_config)."""
    return resolve_config(yaml.safe_load((Path(index_dir) / "search_config.yaml").read_text()))


def resolve_config(cfg):
    """Fill the derived keys (leading underscore) of a raw config dict, in place.

    The corpus root key is corpus_root (dnd_root is an older name, still accepted);
    index_path may be absolute or relative to that root. Everything per index, including
    generic_folders, lives in this dict, so two indexes in one process never share it.
    """
    root = cfg.get("corpus_root") or cfg.get("dnd_root")
    if not root:
        raise ConfigError("search_config.yaml names no corpus_root.")
    if not cfg.get("index_path"):
        raise ConfigError("search_config.yaml names no index_path.")
    # Keys a library config may leave out take the values a session index uses; a missing
    # level_folder_pattern means no folder names a level, and no roots means an empty corpus.
    cfg.setdefault("level_folder_pattern", "(?!)")
    cfg.setdefault("roots", [])
    cfg["chunking"] = dict(CHUNKING_DEFAULTS, **(cfg.get("chunking") or {}))
    cfg["similarity"] = dict(SIMILARITY_DEFAULTS, **(cfg.get("similarity") or {}))
    cfg["_corpus_root"] = Path(os.path.expanduser(str(root)))
    ip = Path(os.path.expanduser(str(cfg["index_path"])))
    cfg["_index_path"] = ip if ip.is_absolute() else cfg["_corpus_root"] / ip
    labels = cfg.get("labels") or {}
    cfg["_label_level"] = labels.get("level", "level")
    cfg["_label_level_help"] = labels.get("level_help", "level number from the folder path")
    cfg["_label_kinds"] = labels.get("kinds", "")
    cfg["_generic_folders"] = tuple(cfg.get("generic_folders") or ())
    backend = (cfg.get("similarity") or {}).get("backend", "sentence-transformers")
    if backend not in ("sentence-transformers", "tfidf"):
        raise ConfigError("similarity.backend must be sentence-transformers or tfidf, not %r."
                          % backend)
    cfg["_backend"] = backend
    return cfg


@dataclasses.dataclass
class SearchOptions:
    """What a search selects and how it ranks. Field names match the CLI flags."""
    similar: bool = False       # hybrid: BM25 plus vectors, fused, then reranked
    level: int = None
    kind: str = None            # a leading "!" excludes the kind instead
    path: str = None
    tag: list = None            # every tag must match
    limit: int = 10
    no_rerank: bool = False
    expand: bool = False
    no_expand: bool = False
    exact: bool = False         # no typo correction


def _note(notes, kind, text):
    """Record a side remark for the reader (stderr in the CLI). kind is one of
    literal-phrase, any-word, typos, rerank-failed, rerank-unavailable."""
    if notes is not None:
        notes.append((kind, text))


@dataclasses.dataclass
class SearchResult:
    results: list               # [(row dict, score)], best first
    tag: str                    # score label: "bm25", "rrf " or "ce  "
    header: str                 # model line, expansion and typo notes; None when none
    effective_query: str        # the query after typo correction, for highlighting
    notes: list                 # [(kind, text)]


class Index:
    """One search index: its directory, its resolved config and its own caches.

    The corpus vocabulary (typo correction) and synonyms belong to the index, so a process
    that queries several indexes corrects and expands each against its own. Loaded models
    are shared process-wide (_MODEL_CACHE), since they do not depend on the index.
    """

    def __init__(self, index_dir, config=None):
        self.dir = Path(index_dir).resolve()
        self.cfg = resolve_config(dict(config)) if config is not None else load_config(self.dir)
        self._words = None
        self._synonyms = None

    @property
    def name(self):
        return self.dir.name

    def connect(self):
        return connect(self.cfg)

    def words(self, con):
        """Unstemmed corpus words and their chunk counts, read once per Index."""
        if self._words is None:
            try:
                self._words = dict(con.execute("SELECT word, df FROM words"))
            except sqlite3.OperationalError:
                self._words = {}   # index predates the words table; rebuild to enable correction
        return self._words

    def synonyms(self):
        """term -> set of equivalent terms, built from synonyms.yaml. Empty if absent."""
        if self._synonyms is None:
            path = self.dir / "synonyms.yaml"
            syn = {}
            if path.exists():
                spec = yaml.safe_load(path.read_text()) or {}
                for group in spec.get("groups") or []:
                    members = [str(g).lower().strip() for g in group if str(g).strip()]
                    for m in members:
                        syn.setdefault(m, set()).update(x for x in members if x != m)
            self._synonyms = syn
        return self._synonyms

    def build(self, progress=None):
        return build(self, progress)

    def search(self, query, opts=None, **kw):
        return search(self, query, opts if opts is not None else SearchOptions(**kw))

    def evaluate(self, opts=None, typos=False):
        return evaluate(self, opts, typos)


# ------------------------------------------------------------------------- chunking

def split_markdown(text):
    """Yield (level, heading, body) in document order.

    Each heading owns only the text up to the next heading of ANY level. Ancestry is
    preserved separately as a heading path, so a subsection is findable on its own
    without duplicating its parent's text into the index.
    """
    lines = text.splitlines()
    blocks, cur_head, cur_level, buf, fenced = [], None, 0, [], False
    for line in lines:
        if FENCE_RE.match(line):
            fenced = not fenced
        m = None if fenced else HEADING_RE.match(line)
        if m:
            if cur_head is not None or buf:
                blocks.append((cur_level, cur_head, "\n".join(buf).strip()))
            cur_level, cur_head, buf = len(m.group(1)), m.group(2).strip(), []
        else:
            buf.append(line)
    if cur_head is not None or buf:
        blocks.append((cur_level, cur_head, "\n".join(buf).strip()))
    return blocks


def split_long(body, max_chars):
    """Split an over-long body on blank lines, never mid-paragraph unless forced."""
    if len(body) <= max_chars:
        return [body]
    parts, cur = [], []
    for para in re.split(r"\n\s*\n", body):
        para = para.strip()
        if not para:
            continue
        if len(para) > max_chars:
            if cur:
                parts.append("\n\n".join(cur))
                cur = []
            for i in range(0, len(para), max_chars):
                parts.append(para[i:i + max_chars])
            continue
        if sum(len(c) + 2 for c in cur) + len(para) > max_chars and cur:
            parts.append("\n\n".join(cur))
            cur = []
        cur.append(para)
    if cur:
        parts.append("\n\n".join(cur))
    return parts or [body]


def heading_path_for(stack):
    return " > ".join(h for _, h in stack)


SENTENCE_END = re.compile(r"[.!?]['\")\]]?\s|\n")


def windows(text, size, overlap):
    """Split text into overlapping windows for the similarity backend.

    An embedding model reads a fixed number of tokens and silently drops the rest:
    all-MiniLM-L6-v2 stops at 256 word pieces, roughly 1,000 characters. A room
    description longer than that loses its tail, so a detail in the last sentence
    becomes unfindable by meaning. Overlapping windows keep every sentence inside at
    least one vector, and the overlap stops a detail that straddles a boundary from
    landing at the edge of both.

    Windows end on a sentence boundary when one falls near the cut, so a window rarely
    begins or ends mid-clause.
    """
    if len(text) <= size:
        return [text]
    out, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            snap = None
            for m in SENTENCE_END.finditer(text, start + int(size * 0.7), end):
                snap = m
            if snap:
                end = snap.end()
        piece = text[start:end].strip()
        if piece:
            out.append(piece)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return out or [text]


# ------------------------------------------------------------------------- metadata

def path_metadata(rel_parts, cfg, stem=None):
    """Infer a "level" (via level_folder_pattern) and a region from the folders on the path.

    The file stem is checked too, because Roll20 journal dumps arrive as "Level 6.md"
    inside a folder that carries no level of its own. Folder names in the config's
    generic_folders never name a region.
    """
    pat = re.compile(cfg["level_folder_pattern"])
    generic = cfg.get("_generic_folders", ())
    level, region = None, None
    for part in rel_parts[:-1]:
        m = pat.match(part)
        if m:
            level = int(m.group(1))
            region = (m.group(2) or "").strip() or part
        elif part not in generic and region is None:
            region = part
    if level is None and stem:
        m = pat.match(stem)
        if m:
            level = int(m.group(1))
    return level, region


def classify(cfg, rel_parts, filename, heading, default_kind):
    folders = rel_parts[:-1]
    for rule in cfg.get("kind_rules") or []:
        if rule.get("kind") is None:
            continue
        pat = re.compile(rule["pattern"], re.I)
        field = rule["field"]
        if field == "folder" and any(pat.search(f) for f in folders):
            return rule["kind"]
        if field == "filename" and pat.search(filename):
            return rule["kind"]
        if field == "heading" and heading and pat.search(heading):
            return rule["kind"]
    return default_kind


def area_for(heading):
    if not heading:
        return None
    m = AREA_RE.match(heading.strip())
    return m.group(1).lower() if m else None


# ------------------------------------------------------------------- source scanning

def scan_markdown_file(cfg, root_cfg, fpath, rel):
    rel_parts = rel.parts
    filename = fpath.name
    stem = fpath.stem
    max_chars = cfg["chunking"]["max_chars"]
    min_chars = cfg["chunking"]["min_chars"]
    level, region = path_metadata(rel_parts, cfg, stem)

    text = fpath.read_text(encoding="utf-8", errors="replace")
    tags = file_tags(cfg, text)
    blocks = split_markdown(text)
    file_title = stem
    for lvl, head, _ in blocks:
        if lvl == 1 and head:
            file_title = head
            break

    out, stack = [], []
    for lvl, head, body in blocks:
        if head is not None:
            while stack and stack[-1][0] >= lvl:
                stack.pop()
            stack.append((lvl, head))
        hpath = heading_path_for(stack)
        kind = classify(cfg, rel_parts, filename, head, root_cfg.get("default_kind", "other"))
        area = area_for(head)
        if area is None:
            for _, anc in reversed(stack[:-1] if head is not None else stack):
                a = area_for(anc)
                if a:
                    area = a
                    break
        pieces = split_long(body, max_chars) if body else []
        if not pieces and head:
            pieces = [""]
        for part_i, piece in enumerate(pieces):
            full = ("%s\n%s" % (head, piece)).strip() if head else piece.strip()
            if len(full) < min_chars:
                continue
            out.append(dict(
                root=root_cfg["name"], path=str(rel), file_title=file_title,
                heading=head or "", heading_path=hpath, level=level, region=region,
                area=area, kind=kind, part=part_i, text=full, tags=tags))
    return out


def file_tags(cfg, text):
    """File-level tags from the first line starting with the config's tag_line_prefix.

    Every chunk of the file carries them, so a --tag filter selects whole documents even
    when a document splits into many chunks. Stored space-padded (" a b ") so a filter can
    match one whole tag with LIKE. None when the index sets no prefix or the file has no line.
    """
    prefix = cfg.get("tag_line_prefix")
    if not prefix:
        return None
    for line in text.splitlines():
        if line.startswith(prefix):
            toks = line[len(prefix):].split()
            return (" %s " % " ".join(toks)) if toks else None
    return None


FIELD_RE = re.compile(r"\{(\w+)\}")
DEFAULT_LIBRARY_LINES = ({"template": "Tags: {tags}.", "if": "tags"},)


def library_value(item, field, ranges):
    """An item field as text: a list joined with ", ", a `ranges` field ({min, max}) in that
    range's words, anything else as str() (a missing field reads "None")."""
    v = item.get(field)
    if field in ranges:
        words = ranges[field]
        lo, hi = (v or {}).get("min"), (v or {}).get("max")
        key = "none" if lo is None else "one" if lo == hi else "many"
        return FIELD_RE.sub(lambda m: str({"min": lo, "max": hi}.get(m.group(1))), words[key])
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    return str(v)


def scan_yaml_library(cfg, root_cfg, fpath, rel):
    """One chunk per entry of a YAML file's `items`. The text is the item's `title` and
    `description`, then one line per entry of the root's `lines`: a `template` whose {field}
    names are filled from the item, skipped when its `if` field is empty. `ranges` maps a
    {min, max} field to words for one value, a span and none, e.g.
    {one: "size {min}", many: "sizes {min} to {max}", none: "size not stated"}. Without
    `lines` the item's tags follow the description."""
    data = yaml.safe_load(fpath.read_text(encoding="utf-8"))
    lines = root_cfg.get("lines") or DEFAULT_LIBRARY_LINES
    ranges = root_cfg.get("ranges") or {}
    out = []
    for item in data.get("items", []):
        bits = [item.get("title", ""), item.get("description", "")]
        for ln in lines:
            if ln.get("if") and not item.get(ln["if"]):
                continue
            bits.append(FIELD_RE.sub(lambda m: library_value(item, m.group(1), ranges), ln["template"]))
        out.append(dict(
            root=root_cfg["name"], path=str(rel), file_title=root_cfg.get("file_title", "library.yaml"),
            heading=item.get("title", ""), heading_path=item.get("title", ""),
            # Item fields such as a range are descriptive, not folder levels, so they stay
            # out of the level column that --level filters on.
            level=None, region=None, area=None,
            kind=root_cfg.get("default_kind", "catalog"), part=0,
            text="\n".join(b for b in bits if b)))
    return out


MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def normalize_for_hash(heading, text):
    """Collapse a chunk to its bare words, so two copies of the same room text match.

    Markdown link targets are dropped because the same module text is exported with
    different Roll20 journal URLs in each copy, and case and punctuation are dropped
    because one export escapes characters the other leaves alone.
    """
    blob = "%s %s" % (heading or "", text or "")
    blob = MD_LINK_RE.sub(r"\1", blob).lower()
    return re.sub(r"[^a-z0-9]+", " ", blob).strip()


def dedupe_chunks(chunks, cfg):
    """Drop chunks whose text already appeared in a higher priority source.

    The corpus mirrors module text: a level folder's Exploring file and the Roll20
    journal dump of the same level carry the same rooms word for word, so without this
    every such hit appears twice. The surviving copy records where else that text lives
    in `also_in`, so nothing is silently lost.

    Short chunks are left alone. Two unrelated rooms can share a one line Treasure note,
    and merging those would lose a real result rather than a duplicate.

    `prefer_newest_level: true` keeps the copy with the highest level among equal priority,
    so text repeated across catalog years surfaces as the newest year with the older years in
    `also_in`, rather than as whichever year the scan met first.
    """
    dd = cfg.get("dedupe") or {}
    if not dd.get("enabled", True):
        return chunks, 0
    min_chars = dd.get("min_chars", 120)
    priority = [str(x) for x in (dd.get("priority") or [])]
    newest = bool(dd.get("prefer_newest_level"))

    def rank(path):
        low = path.lower()
        for i, frag in enumerate(priority):
            if frag.lower() in low:
                return i
        return len(priority)

    def recency(c):
        return -c["level"] if newest and c.get("level") is not None else 0

    order = sorted(range(len(chunks)), key=lambda i: (rank(chunks[i]["path"]), recency(chunks[i]), i))
    seen, keep, dropped = {}, [True] * len(chunks), 0
    for i in order:
        c = chunks[i]
        norm = normalize_for_hash(c["heading"], c["text"])
        if len(norm) < min_chars:
            continue
        digest = hashlib.sha1(norm.encode("utf-8")).hexdigest()
        owner = seen.get(digest)
        if owner is None:
            seen[digest] = i
            continue
        keep[i] = False
        dropped += 1
        others = chunks[owner].setdefault("also_in", [])
        if c["path"] != chunks[owner]["path"] and c["path"] not in others:
            others.append(c["path"])
    return [c for i, c in enumerate(chunks) if keep[i]], dropped


def collect_chunks(cfg):
    chunks, per_root, skipped = [], {}, []
    croot = cfg["_corpus_root"]
    real_root = croot.resolve()
    for root_cfg in cfg["roots"]:
        base = croot / root_cfg["path"]
        if not base.exists():
            skipped.append(root_cfg["path"])
            per_root[root_cfg["name"]] = 0
            continue
        before = len(chunks)
        if root_cfg.get("loader") == "yaml_library":
            chunks.extend(scan_yaml_library(cfg, root_cfg, base, base.relative_to(croot)))
        else:
            exts = {e.lower() for e in root_cfg.get("extensions", [".md"])}
            for fpath in sorted(base.rglob("*")):
                if not fpath.is_file() or fpath.suffix.lower() not in exts:
                    continue
                # Only the corpus is indexed: nothing under a hidden folder (.git, .trash,
                # .claude), and no link to a file outside the corpus root.
                if any(part.startswith(".") for part in fpath.relative_to(base).parts):
                    continue
                if not fpath.resolve().is_relative_to(real_root):
                    continue
                chunks.extend(scan_markdown_file(cfg, root_cfg, fpath,
                                                 fpath.relative_to(croot)))
        per_root[root_cfg["name"]] = len(chunks) - before
    return chunks, per_root, skipped


# ------------------------------------------------------------------------- backends

# Encodes on stdin-supplied text using whichever interpreter can import
# sentence-transformers. Reads a JSON list of strings, writes raw float32.
# A -c child puts its working directory first on sys.path, and an embedding_pythons entry
# may predate -P, so each child source drops that entry before any other import, and runs
# in an empty temporary folder.
_SAFE_PATH = "import sys\nsys.path[:] = [p for p in sys.path if p]\n"

_ENCODER_SRC = _SAFE_PATH + r"""
import json, os, sys
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
import numpy as np
from sentence_transformers import SentenceTransformer
inp, outp, model_name = sys.argv[1], sys.argv[2], sys.argv[3]
with open(inp) as fh:
    texts = json.load(fh)
model = SentenceTransformer(model_name)
vecs = model.encode(texts, batch_size=64, show_progress_bar=False,
                    normalize_embeddings=True)
np.asarray(vecs, dtype="float32").tofile(outp)
"""


# Loaded models, keyed by name. Loading one costs seconds, so a process that runs many
# queries (eval, or any future long-lived caller) must not pay that per query. This is
# the same singleton pattern zenhub-cli uses for its embedder.
_MODEL_CACHE = {}


def _load_model(name, cross=False):
    key = ("cross" if cross else "bi", name)
    if key not in _MODEL_CACHE:
        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
        os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
        if cross:
            from sentence_transformers import CrossEncoder
            _MODEL_CACHE[key] = CrossEncoder(name)
        else:
            from sentence_transformers import SentenceTransformer
            _MODEL_CACHE[key] = SentenceTransformer(name)
    return _MODEL_CACHE[key]


def hub_cache_dir():
    """The Hugging Face hub cache folder, by huggingface_hub's own precedence."""
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.path.expanduser(os.environ["HF_HUB_CACHE"]))
    home = os.environ.get("HF_HOME") or os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache"), "huggingface")
    return Path(os.path.expanduser(home)) / "hub"


def model_cached(name):
    """True when model `name` is a local folder or has a downloaded snapshot in the hub
    cache. sentence-transformers reads a bare name as sentence-transformers/<name>."""
    if Path(os.path.expanduser(name)).is_dir():
        return True
    ids = [name] if "/" in name else ["sentence-transformers/" + name, name]
    for mid in ids:
        snaps = hub_cache_dir() / ("models--" + mid.replace("/", "--")) / "snapshots"
        if snaps.is_dir() and any(any(s.iterdir()) for s in snaps.iterdir() if s.is_dir()):
            return True
    return False


_HUB_NOTICE = []


def prepare_hub(cfg):
    """Load models offline once the configured ones are cached, so a search or build never
    downloads mid-run; before that, allow the one download and say so on stderr. Runs before
    huggingface_hub is imported (it reads HF_HUB_OFFLINE once, at import), and an
    HF_HUB_OFFLINE the caller set always wins. Children inherit the decision."""
    if "HF_HUB_OFFLINE" in os.environ or cfg.get("_backend") == "tfidf":
        return
    sim = cfg.get("similarity") or {}
    names = [n for n in (sim.get("model"), sim.get("reranker")) if n]
    if all(model_cached(n) for n in names):
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    elif not _HUB_NOTICE:
        _HUB_NOTICE.append(True)
        sys.stderr.write("fleetwatch: the search models are not downloaded yet; fetching them "
                         "once into the Hugging Face cache (about 1 GB)\n")


def find_embedder(cfg):
    """Locate something that can produce sentence embeddings.

    Prefers an in-process import, then falls back to any configured interpreter that
    can import sentence-transformers. The fallback exists because the heavy dependency
    (torch, roughly 400 MB) usually lives in another tool's virtualenv rather than in
    the one this script runs under, and borrowing it beats duplicating it.

    Returns ("inproc", None), ("subprocess", interpreter_path), or None.
    """
    prepare_hub(cfg)
    try:
        import sentence_transformers  # noqa: F401
        return ("inproc", None)
    except Exception:
        pass
    for cand in cfg["similarity"].get("embedding_pythons") or []:
        exe = Path(os.path.expanduser(cand))
        if not exe.exists():
            continue
        with tempfile.TemporaryDirectory() as tmpdir:
            probe = subprocess.run([str(exe), "-c", _SAFE_PATH + "import sentence_transformers"],
                                   capture_output=True, cwd=tmpdir)
        if probe.returncode == 0:
            return ("subprocess", str(exe))
    return None


def encode_texts(cfg, texts, embedder):
    """Embed a list of strings, returning an (N, D) float32 array of unit vectors."""
    if np is None:
        raise EmbedderUnavailable("Embeddings need numpy. Install the `search` extra, or list a "
                                  "Python that has it under similarity.embedding_pythons.")
    model_name = cfg["similarity"]["model"]
    mode, exe = embedder
    if mode == "inproc":
        model = _load_model(model_name)
        return np.asarray(model.encode(texts, batch_size=64, show_progress_bar=False,
                                       normalize_embeddings=True), dtype="float32")
    with tempfile.TemporaryDirectory() as tmpdir:
        inp = Path(tmpdir) / "in.json"
        outp = Path(tmpdir) / "out.f32"
        inp.write_text(json.dumps(texts))
        prepare_hub(cfg)
        env = dict(os.environ)
        proc = subprocess.run([exe, "-c", _ENCODER_SRC, str(inp), str(outp), model_name],
                              capture_output=True, env=env, cwd=tmpdir)
        if proc.returncode != 0 or not outp.exists():
            raise EmbeddingFailed("Embedding failed using %s:\n%s"
                                  % (exe, proc.stderr.decode("utf-8", "replace")[-2000:]))
        flat = np.fromfile(outp, dtype="float32")
    return flat.reshape(len(texts), -1)


_RERANKER_SRC = _SAFE_PATH + r"""
import json, os, sys
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
import numpy as np
from sentence_transformers import CrossEncoder
inp, outp, model_name = sys.argv[1], sys.argv[2], sys.argv[3]
with open(inp) as fh:
    pairs = json.load(fh)
model = CrossEncoder(model_name)
scores = model.predict(pairs, show_progress_bar=False)
np.asarray(scores, dtype="float32").tofile(outp)
"""


def rerank_scores(cfg, query, texts, embedder, notes=None):
    """Score each (query, chunk) pair with a cross-encoder.

    A bi-encoder embeds the query and the chunk separately, so it never sees them
    together. A cross-encoder reads the pair as one input, which is slower but far
    better at judging whether a passage actually answers the query. It is used only on
    the handful of candidates that retrieval already surfaced.

    Returns None when the reranker subprocess fails, with the reason added to `notes`;
    the caller keeps retrieval order.
    """
    name = cfg["similarity"].get("reranker")
    if not name or not texts:
        return None
    mode, exe = embedder
    pairs = [[query, x] for x in texts]
    if mode == "inproc":
        return np.asarray(_load_model(name, cross=True).predict(pairs,
                                                                show_progress_bar=False),
                          dtype="float32")
    with tempfile.TemporaryDirectory() as tmpdir:
        inp = Path(tmpdir) / "pairs.json"
        outp = Path(tmpdir) / "scores.f32"
        inp.write_text(json.dumps(pairs))
        prepare_hub(cfg)
        env = dict(os.environ)
        proc = subprocess.run([exe, "-c", _RERANKER_SRC, str(inp), str(outp), name],
                              capture_output=True, env=env, cwd=tmpdir)
        if proc.returncode != 0 or not outp.exists():
            _note(notes, "rerank-failed", "Reranking failed, falling back to retrieval order:\n%s"
                  % proc.stderr.decode("utf-8", "replace")[-600:])
            return None
        return np.fromfile(outp, dtype="float32")


def tokenize(text):
    return [w.lower() for w in WORD_RE.findall(text)]


def build_tfidf(texts, max_features):
    """Sparse TF-IDF with L2 normalized rows. Returns (vocab, idf, rows)."""
    import math
    df = {}
    docs = []
    for t in texts:
        counts = {}
        for w in tokenize(t):
            if len(w) < 3 or w in STOPWORDS:
                continue
            counts[w] = counts.get(w, 0) + 1
        docs.append(counts)
        for w in counts:
            df[w] = df.get(w, 0) + 1
    keep = sorted(df.items(), key=lambda kv: (-kv[1], kv[0]))[:max_features]
    vocab = {w: i for i, (w, _) in enumerate(keep)}
    n = max(len(texts), 1)
    idf = {w: math.log((1 + n) / (1 + df[w])) + 1.0 for w in vocab}
    rows = []
    for counts in docs:
        idxs, vals = [], []
        for w, c in counts.items():
            if w not in vocab:
                continue
            idxs.append(vocab[w])
            vals.append((1.0 + math.log(c)) * idf[w])
        if vals:
            norm = sum(v * v for v in vals) ** 0.5
            vals = [v / norm for v in vals]
        rows.append((idxs, vals))
    return vocab, idf, rows


def tfidf_query_vector(query_terms, vocab, idf):
    import math
    counts = {}
    for w in query_terms:
        if w in vocab:
            counts[w] = counts.get(w, 0) + 1
    vec = {vocab[w]: (1.0 + math.log(c)) * idf[w] for w, c in counts.items()}
    norm = sum(v * v for v in vec.values()) ** 0.5
    return {k: v / norm for k, v in vec.items()} if norm else {}


# ---------------------------------------------------------------------------- build

SCHEMA = """
CREATE TABLE chunks (
  id INTEGER PRIMARY KEY, root TEXT, path TEXT, file_title TEXT, heading TEXT,
  heading_path TEXT, level INTEGER, region TEXT, area TEXT, kind TEXT,
  part INTEGER, text TEXT, also_in TEXT, tags TEXT
);
CREATE INDEX chunks_level ON chunks(level);
CREATE INDEX chunks_kind ON chunks(kind);
CREATE VIRTUAL TABLE chunks_fts USING fts5(
  heading, heading_path, text, content='chunks', content_rowid='id',
  tokenize='porter unicode61'
);
CREATE TABLE vectors (chunk_id INTEGER, window INTEGER, idx BLOB, val BLOB,
  PRIMARY KEY (chunk_id, window));
CREATE TABLE vocab (term TEXT PRIMARY KEY, idx INTEGER, idf REAL);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE words (word TEXT PRIMARY KEY, df INTEGER);
"""


def build(ix, progress=None):
    """Rebuild the index's SQLite file from its corpus roots and return a report dict.

    The vector backend is the config's similarity.backend: sentence-transformers (the
    default) raises EmbedderUnavailable when nothing can embed, rather than quietly writing
    a TF-IDF index that ranks worse; tfidf builds keyword vectors and needs no model.
    `progress(text)`, when given, hears about the slow embedding step before it starts.
    """
    cfg = ix.cfg
    t0 = time.time()
    chunks, per_root, skipped = collect_chunks(cfg)
    if not chunks:
        raise EmptyCorpus("No chunks found. Check the roots in search_config.yaml.")
    chunks, deduped = dedupe_chunks(chunks, cfg)
    for c in chunks:
        c["also_in"] = json.dumps(c["also_in"]) if c.get("also_in") else None
        c.setdefault("tags", None)

    win = cfg["similarity"]["window_chars"]
    ovl = cfg["similarity"]["window_overlap"]
    texts, owners = [], []
    for ci, c in enumerate(chunks):
        for wi, piece in enumerate(windows(c["text"], win, ovl)):
            texts.append(piece)
            owners.append((ci, wi))
    multi = sum(1 for c in chunks if len(windows(c["text"], win, ovl)) > 1)

    embedder = None
    if cfg["_backend"] != "tfidf":
        embedder = find_embedder(cfg) if np is not None else None
        if embedder is None:
            raise EmbedderUnavailable(
                "No interpreter that can import sentence-transformers was found, so this index\n"
                "cannot be embedded. Install the search extra, check similarity.embedding_pythons\n"
                "in search_config.yaml, or set similarity.backend: tfidf to build keyword vectors.")

    db_path = cfg["_index_path"]
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = db_path.with_suffix(".sqlite.tmp")
    if tmp.exists():
        tmp.unlink()
    con = sqlite3.connect(tmp)
    try:
        con.executescript(SCHEMA)
        con.executemany(
            "INSERT INTO chunks (root, path, file_title, heading, heading_path, level, region,"
            " area, kind, part, text, also_in, tags) VALUES (:root,:path,:file_title,:heading,"
            ":heading_path,:level,:region,:area,:kind,:part,:text,:also_in,:tags)", chunks)
        con.execute("INSERT INTO chunks_fts (rowid, heading, heading_path, text) "
                    "SELECT id, heading, heading_path, text FROM chunks")
        # Unstemmed words and their chunk frequency, for typo correction. The FTS table holds
        # Porter stems, which a misspelled query word cannot be compared against reliably.
        df = {}
        for c in chunks:
            for w in set(tokenize("%s %s" % (c["heading"] or "", c["text"]))):
                df[w] = df.get(w, 0) + 1
        con.executemany("INSERT INTO words VALUES (?,?)", df.items())

        if embedder is not None:
            via = "in process" if embedder[0] == "inproc" else embedder[1]
            backend = "sentence-transformers:%s (via %s)" % (cfg["similarity"]["model"], via)
            if progress:
                progress("Embedding %d chunks with %s ..." % (len(texts), cfg["similarity"]["model"]))
            vecs = encode_texts(cfg, texts, embedder)
            ids = [r[0] for r in con.execute("SELECT id FROM chunks ORDER BY id")]
            con.executemany("INSERT INTO vectors (chunk_id, window, idx, val) VALUES (?,?,?,?)",
                            [(ids[ci], wi, None, np.asarray(v, dtype="float32").tobytes())
                             for (ci, wi), v in zip(owners, vecs)])
            con.execute("INSERT INTO meta VALUES ('dim', ?)", (str(len(vecs[0])),))
        else:
            backend = "tfidf"
            vocab, idf, rows = build_tfidf(texts, cfg["similarity"]["max_features"])
            con.executemany("INSERT INTO vocab VALUES (?,?,?)",
                            [(w, i, idf[w]) for w, i in vocab.items()])
            ids = [r[0] for r in con.execute("SELECT id FROM chunks ORDER BY id")]
            con.executemany(
                "INSERT INTO vectors (chunk_id, window, idx, val) VALUES (?,?,?,?)",
                [(ids[ci], wi, struct.pack("<%di" % len(ix_), *ix_),
                  struct.pack("<%df" % len(vl), *vl))
                 for (ci, wi), (ix_, vl) in zip(owners, rows)])
        con.execute("INSERT INTO meta VALUES ('version', ?)", (INDEX_VERSION,))
        con.execute("INSERT INTO meta VALUES ('model', ?)", (cfg["similarity"]["model"],))
        con.execute("INSERT INTO meta VALUES ('backend', ?)", (backend,))
        con.execute("INSERT INTO meta VALUES ('deduped', ?)", (str(deduped),))
        con.execute("INSERT INTO meta VALUES ('built', ?)",
                    (time.strftime("%Y-%m-%d %H:%M:%S"),))
        con.commit()
        con.execute("VACUUM")
    except BaseException:
        con.close()
        tmp.unlink(missing_ok=True)
        raise
    con.close()
    os.replace(tmp, db_path)
    ix._words = None        # the vocabulary belongs to the file just replaced

    counts = {}
    for c in chunks:
        counts[c["kind"]] = counts.get(c["kind"], 0) + 1
    return dict(chunks=len(chunks), elapsed=time.time() - t0, windows=len(texts), multi=multi,
                backend=backend, deduped=deduped, index_path=db_path,
                size_mb=db_path.stat().st_size / 1e6, per_root=per_root, by_kind=counts,
                skipped_roots=skipped)


# --------------------------------------------------------------------------- search

def connect(cfg):
    db = cfg["_index_path"]
    if not db.exists():
        raise IndexMissing("No index at %s. Run: python search.py build" % db)
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row

    # Dedupe keeps one copy of text repeated across level folders (a course unchanged across
    # catalog years), so a --level filter must also accept a chunk whose dropped copies lived
    # at that level, or it would miss every unchanged item.
    def also_has_level(also_in, level):
        for p in json.loads(also_in) if also_in else []:
            if path_metadata(Path(p).parts, cfg, Path(p).stem)[0] == level:
                return 1
        return 0
    con.create_function("also_has_level", 2, also_has_level, deterministic=True)
    if get_meta(con, "version") != INDEX_VERSION:
        con.close()
        raise IndexStale("Index at %s was built by an older version of this tool. Rebuild it:\n"
                         "  python search.py build" % db)
    return con


def get_meta(con, key, default=None):
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def filter_sql(args):
    clauses, params = [], []
    if args.level is not None:
        clauses.append("(c.level = ? OR also_has_level(c.also_in, ?))")
        params += [args.level, args.level]
    # A leading "!" excludes one kind instead of selecting it.
    if args.kind:
        negate = args.kind.startswith("!")
        clauses.append("c.kind != ?" if negate else "c.kind = ?")
        params.append(args.kind[1:] if negate else args.kind)
    if args.path:
        clauses.append("c.path LIKE ?")
        params.append("%" + args.path + "%")
    # Every --tag must match (AND). "facet:value" matches that tag exactly, a bare "value"
    # matches it under any facet, and a trailing "*" matches by prefix. Needs an index built
    # with tag_line_prefix set; older indexes have no tags column.
    for tag in getattr(args, "tag", None) or []:
        tag = tag.strip()
        star = tag.endswith("*")
        tag = tag.rstrip("*")
        if ":" in tag:
            pat = "% " + tag + ("%" if star else " %")
        else:
            pat = "%:" + tag + ("%" if star else " %")
        clauses.append("c.tags LIKE ?")
        params.append(pat)
    return clauses, params


def has_fts_syntax(query):
    """True when the user wrote FTS5 operators, so the query must be passed through as is."""
    if any(ch in query for ch in '"*():^'):
        return True
    return any(tok in ("AND", "OR", "NOT", "NEAR") for tok in query.split())


def expansion_terms(ix, query):
    """Curated alternatives for the words in a query, from the index's synonyms.yaml.

    Matches one, two and three word windows so multi-word entries like "surprise attack"
    are found. Returns terms the user did not type, ready to be searched at lower weight.
    """
    syn = ix.synonyms()
    if not syn:
        return []
    words = tokenize(query)
    typed, found = set(words), []
    for size in (3, 2, 1):
        for i in range(len(words) - size + 1):
            phrase = " ".join(words[i:i + size])
            for alt in sorted(syn.get(phrase, ())):
                if alt not in typed and alt not in found:
                    found.append(alt)
    return found


def edit_distance(a, b, cap):
    """Optimal string alignment distance (adjacent swaps cost 1), or cap+1 once it exceeds cap."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev2, prev = None, list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        if min(cur) > cap:
            return cap + 1
        prev2, prev = prev, cur
    return prev[-1]


def nearest_word(w, words, cap):
    """Closest corpus word within cap edits: fewest edits, then longer (a dropped letter is
    the commonest typo), then the word in the most chunks. None if nothing is close."""
    best, key = None, None
    for cand, n in words.items():
        if cand == w or abs(len(cand) - len(w)) > cap:
            continue
        d = edit_distance(w, cand, cap)
        if d <= cap:
            k = (d, 0 if len(cand) >= len(w) else 1, -n)
            if key is None or k < key:
                best, key = (cand, n), k
    return best


# A word in this many chunks or fewer counts as rare, and is also searched as its common
# neighbour when that neighbour is at least RARE_RATIO times as frequent and in RARE_MIN_DF
# chunks. This catches typos that occur in the corpus itself (a web page that misspells
# "cybersecurity") while still matching the rare word as typed, so real names survive.
RARE_DF, RARE_RATIO, RARE_MIN_DF = 5, 50, 10


def correct_typos(ix, con, query):
    """Correct misspelled query words against the corpus vocabulary.

    Returns (hard_query, soft_query, hard_fixes, soft_fixes). Hard fixes replace words
    that occur nowhere in the corpus. Soft fixes apply to rare words that have a far more
    common neighbour; soft_query carries both kinds so the caller can search it alongside
    hard_query. Words under four letters are left alone (too many near neighbours). The
    allowed distance is one edit up to six letters and two beyond. The vocabulary is the
    index's own (ix.words).
    """
    if has_fts_syntax(query):
        return query, query, [], []
    words = ix.words(con)
    if not words:
        return query, query, [], []
    hard, soft = [], []
    for w in dict.fromkeys(tokenize(query)):
        if len(w) < 4 or w in STOPWORDS:
            continue
        df = words.get(w, 0)
        if df > RARE_DF:
            continue
        near = nearest_word(w, words, 1 if len(w) <= 6 else 2)
        if not near:
            continue
        if df == 0:
            hard.append((w, near[0]))
        elif near[1] >= max(RARE_MIN_DF, RARE_RATIO * df):
            soft.append((w, near[0]))

    def apply(q, fixes):
        for typed, fix in fixes:
            q = re.sub(r"(?i)\b%s\b" % re.escape(typed), fix, q)
        return q
    hard_q = apply(query, hard)
    return hard_q, apply(hard_q, soft), hard, soft


def as_fts_term(term):
    """Quote a multi-word alternative so FTS5 reads it as a phrase."""
    return '"%s"' % term if " " in term else term


def content_terms(query):
    """The words worth matching on: no stopwords, no one or two letter fragments."""
    return [t for t in tokenize(query)
            if len(t) >= 3 and t not in STOPWORDS and t not in FTS_OPERATORS]


def bm25_search(con, query, args, limit, loose=False, notes=None):
    """BM25 keyword search.

    FTS5 ANDs bare words, so a question phrased as a sentence matches nothing once its
    stopwords are required. `loose` (and a zero-result retry) drops to an any-word OR,
    which is what makes the keyword half of a hybrid search contribute anything.
    """
    clauses, params = filter_sql(args)
    where = " AND ".join(["chunks_fts MATCH ?"] + clauses)
    sql = ("SELECT c.*, bm25(chunks_fts, 8.0, 4.0, 1.0) AS score "
           "FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid "
           "WHERE %s ORDER BY score LIMIT ?" % where)

    def run(match):
        try:
            return con.execute(sql, [match] + params + [limit]).fetchall()
        except sqlite3.OperationalError:
            # The query used characters FTS5 treats as syntax. Retry as a literal phrase.
            safe = '"%s"' % match.replace('"', " ")
            _note(notes, "literal-phrase", "Note: treated the query as a literal phrase, since "
                  "FTS5 rejected its syntax.")
            return con.execute(sql, [safe] + params + [limit]).fetchall()

    verbatim = has_fts_syntax(query)
    terms = content_terms(query)
    if loose and not verbatim and len(terms) > 1:
        rows = run(" OR ".join(terms))
    else:
        rows = run(query)
        if not rows and not verbatim and len(terms) > 1:
            rows = run(" OR ".join(terms))
            if rows:
                _note(notes, "any-word",
                      "Note: no chunk contains every word, so these match any of them.\n")
    # bm25() returns negative numbers where more negative is better. Flip for display.
    return [(dict(r), -r["score"]) for r in rows]


def vector_search(con, cfg, query, args, limit):
    if np is None:
        raise EmbedderUnavailable("Similarity needs numpy. Install the `search` extra, or list a "
                                  "Python that has it under similarity.embedding_pythons.")
    backend = get_meta(con, "backend", "tfidf")
    indexed_model = get_meta(con, "model")
    if backend.startswith("sentence-transformers") and indexed_model \
            and indexed_model != cfg["similarity"]["model"]:
        raise ModelMismatch("This index was built with %s but search_config.yaml now names %s.\n"
                            "Vectors from different models are not comparable. Rebuild:\n"
                            "  python search.py build" % (indexed_model, cfg["similarity"]["model"]))
    clauses, params = filter_sql(args)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = con.execute("SELECT c.*, v.window AS vwin, v.idx AS vidx, v.val AS vval "
                       "FROM chunks c JOIN vectors v ON v.chunk_id = c.id" + where,
                       params).fetchall()
    if not rows:
        return []

    if backend.startswith("sentence-transformers"):
        embedder = find_embedder(cfg)
        if embedder is None:
            raise EmbedderUnavailable(
                "This index holds embeddings, but no interpreter that can import\n"
                "sentence-transformers was found. Check similarity.embedding_pythons\n"
                "in search_config.yaml, or set similarity.backend: tfidf and rebuild:\n"
                "  python search.py build")
        # bge models expect an instruction prefix on the QUERY only, never on documents.
        prefix = cfg["similarity"].get("query_prefix") or ""
        qv = encode_texts(cfg, [prefix + query], embedder)[0]
        mat = np.vstack([np.frombuffer(r["vval"], dtype="float32") for r in rows])
        scores = mat @ qv
    else:
        vocab = {r["term"]: (r["idx"], r["idf"])
                 for r in con.execute("SELECT term, idx, idf FROM vocab")}
        v_idx = {t: v[0] for t, v in vocab.items()}
        v_idf = {t: v[1] for t, v in vocab.items()}
        terms = [t for t in tokenize(query) if t not in FTS_OPERATORS]
        qvec = tfidf_query_vector(terms, v_idx, v_idf)
        if not qvec:
            return []
        scores = np.zeros(len(rows), dtype="float32")
        for i, r in enumerate(rows):
            if not r["vidx"]:
                continue
            idxs = np.frombuffer(r["vidx"], dtype="<i4")
            vals = np.frombuffer(r["vval"], dtype="<f4")
            s = 0.0
            for j, col in enumerate(idxs):
                qw = qvec.get(int(col))
                if qw:
                    s += qw * float(vals[j])
            scores[i] = s

    # One row per window. A chunk scores as its best window, so a detail in the tail of
    # a long room is not diluted by the rest of the room.
    best = {}
    for i, r in enumerate(rows):
        cid = r["id"]
        if cid not in best or scores[i] > best[cid][0]:
            best[cid] = (float(scores[i]), r)
    ranked = sorted(best.values(), key=lambda kv: -kv[0])[:limit]
    return [(dict(r), s) for s, r in ranked if s > 0]


def rrf_merge(lists, k, limit):
    """Weighted reciprocal rank fusion over several ranked result lists.

    Each entry is (results, weight). Weight below 1.0 lets a list contribute without
    being able to outvote the others, which is how expanded synonyms are kept
    subordinate to the words the user actually typed.
    """
    scores, keep = {}, {}
    for results, weight in lists:
        for rank, (row, _) in enumerate(results, start=1):
            cid = row["id"]
            scores[cid] = scores.get(cid, 0.0) + weight / (k + rank)
            keep.setdefault(cid, row)
    best = sorted(scores.items(), key=lambda kv: -kv[1])[:limit]
    return [(keep[cid], score) for cid, score in best]


# --------------------------------------------------------------------------- output

def query_terms_for_highlight(query):
    """Words worth marking in a snippet. A quoted phrase is kept whole; bare stopwords
    and one or two letter fragments are dropped, since bracketing every "a" and "is"
    makes the snippet unreadable."""
    terms = []
    for phrase, word in re.findall(r'"([^"]+)"|(\S+)', query):
        token = (phrase or word).strip("()*")
        if not token or token.upper() in ("AND", "OR", "NOT", "NEAR"):
            continue
        if not phrase and (len(token) < 3 or token.lower() in STOPWORDS):
            continue
        terms.append(token)
    return terms


def snippet(text, query, width=200):
    flat = re.sub(r"\s+", " ", text).strip()
    terms = query_terms_for_highlight(query)
    pos, hit = 0, None
    for t in terms:
        m = re.search(re.escape(t), flat, re.I)
        if m and (hit is None or m.start() < hit.start()):
            hit = m
    if hit:
        pos = max(0, hit.start() - width // 3)
    out = flat[pos:pos + width]
    if pos:
        out = "..." + out
    if pos + width < len(flat):
        out = out + "..."
    for t in sorted(set(terms), key=len, reverse=True):
        out = re.sub("(%s)" % re.escape(t), r"[\1]", out, flags=re.I)
    return out


def result_dicts(results):
    """Results as plain dicts: the machine-readable form (search.py --json) and what
    session tools read back."""
    return [
        {"rank": i, "score": round(s, 4), "level": r["level"], "area": r["area"],
         "kind": r["kind"], "path": r["path"], "heading": r["heading"],
         "heading_path": r["heading_path"], "region": r["region"],
         "text": r["text"],
         "also_in": json.loads(r["also_in"]) if r.get("also_in") else [],
         "tags": (r.get("tags") or "").split()}
        for i, (r, s) in enumerate(results, start=1)]


# ---------------------------------------------------------------------------- query

def run_query(ix, con, query, args, limit, notes=None):
    """Retrieve, then optionally rerank. Returns (results, tag, header, effective_query).

    Misspelled words are corrected first (unless exact), and the corrected query is used
    for every stage and for highlighting; effective_query records it. Side remarks for the
    reader go to `notes` as (kind, text).
    """
    cfg = ix.cfg
    fixes, soft, soft_q, typed = [], [], query, query
    if not getattr(args, "exact", False):
        query, soft_q, fixes, soft = correct_typos(ix, con, query)
    reranker = cfg["similarity"].get("reranker")
    want_rerank = bool(reranker) and not args.no_rerank and args.similar
    pool = cfg["similarity"].get("rerank_pool", 50) if want_rerank else max(limit * 3, 30)

    # Identifiers the query names (exact_heading_pattern, e.g. course codes). Case-sensitive,
    # so "Fall 2025" is not taken for a code; "CS3600" still means "CS 3600".
    pat = cfg.get("exact_heading_pattern")
    norm = lambda s: re.sub(r"\s+", " ", re.sub(r"([A-Za-z])\s*(\d)", r"\1 \2", s)).strip().upper()
    wanted = {norm(m.group(0)) for m in re.finditer(pat, typed + " " + query)} if pat else set()

    expanded = []
    if args.similar:
        bm = bm25_search(con, query, args, pool, loose=True, notes=notes)
        vec = vector_search(con, cfg, soft_q, args, pool)
        lists = [(bm, 1.0), (vec, 1.0)]
        if soft:
            lists.append((bm25_search(con, soft_q, args, pool, loose=True, notes=notes), 1.0))
        # Expansion is keyword-side only, never on a phrase or operator query, and always
        # at a lower weight than the literal words.
        expand_on = getattr(args, "expand", False) or (
            cfg["similarity"].get("expand_by_default", False) and not args.no_expand)
        if expand_on and not has_fts_syntax(query):
            expanded = expansion_terms(ix, query)
            if expanded:
                alt = " OR ".join(as_fts_term(x) for x in expanded)
                lists.append((bm25_search(con, alt, args, pool, loose=False, notes=notes),
                              cfg["similarity"].get("expansion_weight", 0.4)))
        results = rrf_merge(lists, cfg["similarity"]["rrf_k"], pool)
        tag = "rrf "
    else:
        results = bm25_search(con, query, args, pool, notes=notes)
        tag = "bm25"
        if soft:
            results = rrf_merge([(results, 1.0),
                                 (bm25_search(con, soft_q, args, pool, notes=notes), 1.0)],
                                cfg["similarity"]["rrf_k"], pool)
            tag = "rrf "

    # A named identifier's own entry can miss the pool when the other query words favor the
    # many entries that repeat them, so an exact phrase search for each joins the pool.
    if wanted:
        present = {(r["path"], r["heading"], r.get("part")) for r, _ in results}
        for w in wanted:
            for r, s in bm25_search(con, '"%s"' % w, args, 10, notes=notes):
                if (r["path"], r["heading"], r.get("part")) not in present:
                    present.add((r["path"], r["heading"], r.get("part")))
                    results.append((r, s))

    header = None
    if want_rerank and results:
        embedder = find_embedder(cfg)
        if embedder is None:
            if cfg["_backend"] != "tfidf":
                raise EmbedderUnavailable(
                    "Reranking needs sentence-transformers, and no interpreter that can import\n"
                    "it was found. Check similarity.embedding_pythons in search_config.yaml,\n"
                    "or run with --no-rerank.")
            _note(notes, "rerank-unavailable",
                  "Reranking needs sentence-transformers; returning retrieval order.")
        else:
            scores = rerank_scores(cfg, soft_q, [r["text"] for r, _ in results], embedder, notes)
            if scores is not None:
                order = np.argsort(-scores)
                results = [(results[i][0], float(scores[i])) for i in order]
                tag = "ce  "
                header = "model %s, reranked by %s" % (
                    get_meta(con, "model", "tfidf"), reranker)
    # The reranker judges topic, not identity: "prerequisites for AES 3600" scores courses that
    # list AES 3600 as a prerequisite above AES 3600 itself, sometimes out of the top 20. When
    # the query names an identifier matching the index's exact_heading_pattern (a course code),
    # results whose heading carries it go first, keeping their order otherwise.
    if wanted and results:
        rx = [re.compile(r"(?<![A-Z0-9])%s(?![A-Z0-9])" % re.escape(w)) for w in wanted]

        def named(r):
            h = norm(r["heading_path"] or r["heading"] or "")
            return any(x.search(h) for x in rx)
        results = sorted(results, key=lambda rs: 0 if named(rs[0]) else 1)
    if header is None and args.similar:
        header = "model %s, no reranker" % get_meta(con, "model", "tfidf")
    if expanded and header:
        header += "\nalso searched: %s" % ", ".join(expanded)
    typo_notes = []
    if fixes:
        typo_notes.append("corrected: %s" % ", ".join("%s -> %s" % f for f in fixes))
    if soft:
        typo_notes.append("also searched: %s (rare as typed)" % ", ".join("%s -> %s" % f for f in soft))
    if typo_notes:
        note = "; ".join(typo_notes) + " (use --exact to search as typed)"
        header = note if header is None else header + "\n" + note
        _note(notes, "typos", note)
    return results[:limit], tag, header, soft_q


def search(ix, query, opts=None):
    """Run one query against an index. Returns a SearchResult; never prints."""
    opts = opts or SearchOptions()
    notes = []
    con = ix.connect()
    try:
        results, tag, header, effective = run_query(ix, con, query, opts, opts.limit, notes)
    finally:
        con.close()
    return SearchResult(results, tag, header, effective, notes)


# ----------------------------------------------------------------------------- eval

# The keys probe_matches checks. Any other key would be ignored and the probe would match the
# first result, so check_probes refuses it when probes.yaml loads.
PROBE_EXPECT_KEYS = ("level", "area", "path_contains", "heading_contains")


def check_probes(probes):
    """Raise ProbesInvalid for a probe without a query or whose expect block is empty, is not
    a mapping, or names a key outside PROBE_EXPECT_KEYS."""
    for i, probe in enumerate(probes, start=1):
        if not isinstance(probe, dict) or not probe.get("query"):
            raise ProbesInvalid("probes.yaml probe %d has no query." % i)
        expect = probe.get("expect")
        if not isinstance(expect, dict) or not expect:
            raise ProbesInvalid("probes.yaml probe %d (%r) needs an expect block with one of: %s."
                                % (i, probe["query"], ", ".join(PROBE_EXPECT_KEYS)))
        unknown = sorted(str(k) for k in expect if k not in PROBE_EXPECT_KEYS)
        if unknown:
            raise ProbesInvalid("probes.yaml probe %d (%r): unknown expect key %s; use %s."
                                % (i, probe["query"], ", ".join(unknown), ", ".join(PROBE_EXPECT_KEYS)))


def probe_matches(result, expect):
    if "level" in expect and result.get("level") != expect["level"]:
        return False
    if "area" in expect and (result.get("area") or "") != str(expect["area"]):
        return False
    if "path_contains" in expect and \
            expect["path_contains"].lower() not in (result.get("path") or "").lower():
        return False
    if "heading_contains" in expect and \
            expect["heading_contains"].lower() not in (result.get("heading_path") or "").lower():
        return False
    return True


def misspell(query):
    """Deterministic typo: drop the middle letter of the two longest words of five or more
    letters. Dropped letters are the commonest real typo, and a fixed rule keeps runs
    comparable."""
    words = sorted({w for w in WORD_RE.findall(query) if len(w) >= 5}, key=lambda w: (-len(w), w))[:2]
    for w in words:
        k = len(w) // 2
        query = re.sub(r"\b%s\b" % re.escape(w), w[:k] + w[k + 1:], query, count=1)
    return query


def evaluate(ix, opts=None, typos=False):
    """Run the index's fixed probe set (probes.yaml) and report each probe's rank and the
    MRR. Every probe runs as a hybrid (similar) search with `opts`' filters.

    Returns dict(label, probes, rows=[(query, rank or None, seconds)], mrr, found, total_s,
    typos, notes).
    """
    probe_path = ix.dir / "probes.yaml"
    if not probe_path.exists():
        raise ProbesMissing("No probes.yaml beside search.py.")
    spec = yaml.safe_load(probe_path.read_text())
    limit = (spec.get("settings") or {}).get("limit", 20)
    probes = spec.get("probes") or []
    if not probes:
        raise NoProbesYet("No probes yet: add a query and the result that should answer it to "
                          "probes.yaml, then run eval again.")
    check_probes(probes)

    opts = dataclasses.replace(opts or SearchOptions(), similar=True)
    con = ix.connect()
    notes = []
    try:
        reranker = ix.cfg["similarity"].get("reranker")
        label = "%s%s" % (get_meta(con, "model", "tfidf"),
                          "" if (reranker and not opts.no_rerank) else " (no rerank)")
        rows, rr_total, t0 = [], 0.0, time.time()
        for probe in probes:
            q = misspell(probe["query"]) if typos else probe["query"]
            qt = time.time()
            results, _, _, _ = run_query(ix, con, q, opts, limit, notes)
            elapsed = time.time() - qt
            rank = None
            for i, (r, _s) in enumerate(results, start=1):
                if probe_matches(r, probe["expect"]):
                    rank = i
                    break
            rr_total += (1.0 / rank) if rank else 0.0
            rows.append((q, rank, elapsed))
    finally:
        con.close()
    return dict(label=label, probes=len(probes), rows=rows, mrr=rr_total / len(probes),
                found=sum(1 for _, r, _ in rows if r), total_s=time.time() - t0, typos=typos,
                notes=notes)
