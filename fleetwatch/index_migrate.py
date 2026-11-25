"""`fleetwatch index migrate` and `fleetwatch index verify`.

migrate copies one index from an old single-folder layout (OLD_ROOT/<name>/ plus its registry
entry) into the config and data homes; the old tree is never modified. A session index is split
between the two homes; a plain index stays one folder in the data home (refresh.plain_dir), and
its refresh commands are rewritten to run under whichever Python runs the refresher. verify reports numbers
only (counts, checksums, yes/no), so it is safe on an index whose contents must not be read.
Neither command prints file contents.
"""
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import sys
from pathlib import Path

import yaml

from . import indexes
from .indexes import DATA_ITEMS, IndexesError, tilde

SKIP_COPY = {"search.py", "search_config.yaml", "__pycache__", ".DS_Store"}
SKIP_DIRS = {"refresh.lock.d", ".DS_Store"}
RAW_KEYS = ("corpus_root", "dnd_root", "index_path")
PYTHON_NAME = re.compile(r"python(\d+(\.\d+)*)?")
# A script that adds the old engine folder to sys.path: the folder is not migrated, so the
# import fails once the old tree is gone.
ENGINE_REF = re.compile(r"""parent\s*\.\s*parent\b.*["']engine["']|["']\.\./engine["']""")


# ------------------------------------------------------------------------------ migrate

def _copy(src, dst):
    """Copy a file or folder (modification times kept); returns the file count."""
    if src.is_dir():
        shutil.copytree(src, dst, symlinks=True,
                        ignore=lambda _d, names: [n for n in names if n in SKIP_DIRS])
        return sum(1 for p in dst.rglob("*") if p.is_file() or p.is_symlink())
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return 1


class Rewriter:
    """Rewrites strings that name a path inside the old index folder to the new homes."""

    def __init__(self, old_root, name, cfg_t, data_t):
        self.forms = sorted({str(old_root), tilde(old_root)}, key=len, reverse=True)
        self.name, self.cfg_t, self.data_t = name, cfg_t, data_t
        self.changed = 0
        self.foreign = 0

    def string(self, s, raw_key=None):
        for form in self.forms:
            base = form + "/" + self.name
            if s == base:
                self.changed += 1
                return tilde(self.data_t if raw_key in RAW_KEYS else self.cfg_t)
            if s.startswith(base + "/"):
                rest = s[len(base) + 1:]
                dest = self.data_t if rest.split("/")[0] in DATA_ITEMS else self.cfg_t
                self.changed += 1
                return tilde(dest / rest)
            if s == form or s.startswith(form + "/"):
                self.foreign += 1
        return s

    def walk(self, v, key=None):
        if isinstance(v, str):
            return self.string(v, key)
        if isinstance(v, list):
            return [self.walk(x) for x in v]
        if isinstance(v, dict):
            return {k: self.walk(x, k) for k, x in v.items()}
        return v


def plain_refresh(cmds, name, rw):
    """A plain index's refresh commands for the new layout: (commands, interpreters set to the
    {python} token, build steps set to `fleetwatch index build`, warnings). An interpreter is
    argv[0] named python, python3 or python3.N; arguments between it and the old search.py
    wrapper (such as -I) are kept. -P keeps the index folder, the build's working directory,
    off sys.path, so a script there never shadows a module the build imports."""
    from .memory.refresh import PYTHON_TOKEN
    wrappers = {f + "/" + name + "/search.py" for f in rw.forms} | {"search.py", "./search.py"}
    new, n_py, n_build, warns = [], 0, 0, []
    for k, cmd in enumerate(cmds, 1):
        cmd = [str(a) for a in cmd]
        head = []
        if cmd and PYTHON_NAME.fullmatch(os.path.basename(cmd[0])):
            head, cmd, n_py = [PYTHON_TOKEN], cmd[1:], n_py + 1
        at = next((i for i, a in enumerate(cmd) if a in wrappers), None)
        if at is not None and cmd[at + 1:at + 2] == ["build"]:
            new.append([PYTHON_TOKEN] + rw.walk(cmd[:at])
                       + ["-P", "-m", "fleetwatch", "index", "build", name] + cmd[at + 2:])
            n_py += not head
            n_build += 1
            continue
        if at is not None:
            warns.append("refresh command %d runs the old search.py wrapper for something other "
                         "than build; that file is not copied, so the command fails" % k)
        new.append(head + rw.walk(cmd))
    return new, n_py, n_build, warns


def engine_refs(folder, old_root):
    """(relative path, line number) of every line in a copied .py script that refers to the old
    engine folder. Line numbers only: a script's text is never printed."""
    lits = {str(old_root / "engine"), tilde(old_root / "engine")}
    hits = []
    for p in sorted(Path(folder).rglob("*.py")):
        rel = p.relative_to(folder)
        if rel.parts[0] in DATA_ITEMS:
            continue
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        for no, line in enumerate(text.splitlines(), 1):
            if ENGINE_REF.search(line) or any(lit in line for lit in lits):
                hits.append((rel, no))
    return hits


def migrate(name, old_root, force=False, out=None):
    out = out or sys.stdout
    r = indexes.refresh()
    settings = r.SETTINGS
    os.umask(r.PRIVATE_UMASK)
    if r.index_name_problem(name):
        raise IndexesError(r.index_name_problem(name))
    old_root = Path(os.path.expanduser(old_root)).resolve()
    if not old_root.is_dir():
        raise IndexesError("%s is not a folder" % old_root)
    cfg_root, data_root = Path(r.CONFIG_ROOT).resolve(), Path(r.ROOT).resolve()
    if old_root in (cfg_root, data_root):
        raise IndexesError("--from must be the OLD root, not this installation's own folder")
    old_reg = r.load_registry(old_root / "registry.yaml", old_root / "registry.d")
    if name not in old_reg["indexes"]:
        raise IndexesError("the old registry has no index %r (have: %s)"
                           % (name, ", ".join(sorted(old_reg["indexes"]))))
    entry = old_reg["indexes"][name]
    old_dir = old_root / name
    if not old_dir.is_dir():
        raise IndexesError("%s has no folder %r" % (old_root, name))

    plain = entry.get("kind") == "plain"
    cfg_t, data_t = cfg_root / name, data_root / name
    home_t = data_t if plain else cfg_t      # where the index's config files go
    entry_file = Path(r.REGISTRY_DIR) / (name + ".yaml")
    new_reg = r.load_registry()
    present = [p for p in dict.fromkeys((entry_file, cfg_t, data_t)) if p.exists() or p.is_symlink()]
    other = [n for n in indexes.names_clash(new_reg, name) if n != name]
    if other:
        raise IndexesError("index %r would share its folders with the registered index %r "
                           "(names compare without case); nothing was changed" % (name, other[0]))
    if name in new_reg["indexes"] and entry_file not in present:
        raise IndexesError("index %r is already defined in the new registry.yaml; remove that "
                           "entry first" % name)
    if present and not force:
        raise IndexesError("refusing to overwrite: %s exists (use --force to replace the new "
                           "copy; the old tree is never touched)" % ", ".join(str(p) for p in present))
    group = entry.get("group")
    old_groups = old_reg.get("groups") or {}
    known = old_groups if not r.REGISTRY.exists() else (new_reg.get("groups") or {})
    if group and group not in known:
        raise IndexesError("group %r is not declared in the new registry.yaml" % group)
    # --force replaces only this index's own entry file and folders, each directly under
    # the registry.d, config or data root.
    homes = {Path(r.REGISTRY_DIR).resolve(), cfg_root, data_root}
    for p in present:
        if p.parent.resolve() not in homes or p.name not in (name, name + ".yaml"):
            raise IndexesError("refusing to replace %s: it is not this index's own folder or "
                               "entry file" % p)
    for p in present if force else ():
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink()

    files_cfg = files_data = 0
    # Copies keep their source modes, so the owner-only data folders are what keep a
    # migrated corpus private.
    r.private_dirs(data_root, data_t)
    for child in sorted(old_dir.iterdir()):
        if child.name in SKIP_COPY:
            continue
        is_data = child.name in DATA_ITEMS
        dst = (data_t if is_data else home_t) / child.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        n = _copy(child, dst)
        if is_data:
            files_data += n
        else:
            files_cfg += n

    created_registry = not r.REGISTRY.exists()
    if created_registry:
        r.REGISTRY.parent.mkdir(parents=True, exist_ok=True)
        r.REGISTRY.write_text(yaml.safe_dump(
            {"defaults": old_reg.get("defaults") or dict(r.BUILTIN_DEFAULTS), "groups": old_groups},
            sort_keys=False))
    rw = Rewriter(old_root, name, home_t, data_t)
    new_entry = {k: rw.walk(v, k) for k, v in entry.items() if not (plain and k == "refresh")}
    warns, cmd_note = [], None
    if plain and entry.get("refresh"):
        cmds, n_py, n_build, warns = plain_refresh(entry["refresh"], name, rw)
        new_entry = {k: (cmds if k == "refresh" else new_entry[k]) for k in entry}
        cmd_note = "%d of %d run under {python}; %d build step(s) now `fleetwatch index build %s`" % (
            n_py, len(cmds), n_build, name)
    priv = new_entry.get("privacy") or {}
    missing = [rel for rel in (priv.get("allowlist"), priv.get("denylist"), new_entry.get("tags"))
               if rel and not (cfg_root / rel).exists()]
    if missing:
        raise IndexesError("the entry names %d config file(s) outside this index's folder or "
                           "missing from it; copy them first, then rerun with --force" % len(missing))
    entry_file.parent.mkdir(parents=True, exist_ok=True)
    entry_file.write_text("# Registry entry for %s (migrated from the old layout).\n" % name
                          + yaml.safe_dump({"indexes": {name: new_entry}}, sort_keys=False))
    how = _write_search_config(name, new_entry, old_dir, home_t, data_t, rw, settings)
    refs = engine_refs(home_t, old_root) if home_t.is_dir() else []
    print("index: %s" % name, file=out)
    if plain:
        print("index folder: %s (scripts, config and data together)" % tilde(data_t), file=out)
    print("config files copied: %d" % files_cfg, file=out)
    print("data files copied: %d" % files_data, file=out)
    print("registry entry: %s%s" % (tilde(entry_file), " (registry.yaml created)" if created_registry else ""),
          file=out)
    print("paths rewritten: %d; references to other old-root folders left as they were: %d"
          % (rw.changed, rw.foreign), file=out)
    print("search config: %s" % how, file=out)
    if cmd_note:
        print("refresh commands: %s" % cmd_note, file=out)
    for w in warns:
        print("warning: %s" % w, file=out)
    for rel, no in refs:
        print("warning: %s line %d refers to the old engine folder %s, which is not migrated; "
              "the script fails until it imports that module from somewhere else"
              % (tilde(home_t / rel), no, tilde(old_root / "engine")), file=out)
    print("next: fleetwatch index verify %s --compare-old %s" % (name, tilde(old_root)), file=out)
    return 0


def _strip(cfg):
    return {k: v for k, v in cfg.items() if k not in RAW_KEYS}


def _write_search_config(name, entry, old_dir, cfg_t, data_t, rw, settings):
    """Plain index: copy its search_config.yaml with paths rewritten. Session index: the
    config is generated; any top-level key where the old config differs goes in
    search_overrides.yaml, so the effective config equals the old one. A key the generator
    adds that the old config lacks cannot be overridden away, so that index keeps a copy of
    its old config instead."""
    old_path = old_dir / "search_config.yaml"
    old = yaml.safe_load(old_path.read_text()) if old_path.exists() else None
    session = bool(entry.get("transcripts"))
    if old is None:
        return "none in the old index" if not session else "generated from the registry entry"
    cfg_t.mkdir(parents=True, exist_ok=True)

    def copy_old():
        new = rw.walk(old)
        for k in ("corpus_root", "dnd_root"):
            if k in old and rw.string(old[k], "corpus_root") != old[k]:
                new[k] = rw.string(old[k], "corpus_root")
        (cfg_t / "search_config.yaml").write_text(yaml.safe_dump(new, sort_keys=False))

    if not session:
        copy_old()
        return "copied, paths rewritten"
    gen = indexes.session_config(name, entry, settings)
    old_c, gen_c = _strip(old), _strip(gen)
    surplus = sorted(set(gen_c) - set(old_c))
    if surplus:
        copy_old()
        return "copied from the old config (the generator adds %d key(s) it lacks)" % len(surplus)
    over = {k: v for k, v in old_c.items() if gen_c.get(k) != v}
    if "index_path" in old:
        # An absolute or ~ path into the old folder moves with the data like any other path;
        # it needs no override when it lands where the generated relative path points.
        ip = rw.string(str(old["index_path"]), "index_path")
        same = Path(os.path.expanduser(ip)) == data_t / gen["index_path"]
        if ip != gen["index_path"] and not same:
            over["index_path"] = ip
    if over:
        (cfg_t / indexes.OVERRIDES).write_text(yaml.safe_dump(over, sort_keys=False))
    return "generated from the registry entry; %d key(s) overridden to match the old config%s" % (
        len(over), " (%s)" % ", ".join(sorted(over)) if over else "")


# ------------------------------------------------------------------------------- verify

def corpus_digest(d):
    """(sha256 prefix, file count) of an index's corpus tree: relative path plus file hash.
    The same digest the baseline records."""
    c = Path(d) / "corpus"
    if not c.is_dir():
        return None, 0
    h, n = hashlib.sha256(), 0
    for p in sorted(c.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(c)).encode() + b"\0" + hashlib.sha256(p.read_bytes()).digest())
            n += 1
    return h.hexdigest()[:16], n


def _lines(p):
    try:
        with open(p, "rb") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return None


def counts(data_dir):
    """(exchanges, tags): the two counts the baseline records."""
    sess = Path(data_dir) / "state" / "sessions"
    ex = sum(_lines(p) or 0 for p in sess.glob("*.jsonl")) if sess.is_dir() else None
    return ex, _lines(Path(data_dir) / "state" / "tags.jsonl")


def stored_versions(data_dir, sig, scan_version, tag_version):
    """How many stored parse cursors and tag records carry the versions the current code
    computes: (cursors, cursors equal, tag records, tag records current)."""
    cur = eq = 0
    for p in (Path(data_dir) / "state" / "sessions").glob("*.cursor.json"):
        try:
            got = json.loads(p.read_text()).get("scan_version")
        except (OSError, ValueError):
            continue
        cur += 1
        eq += got == sig
    recs = ok = 0
    tp = Path(data_dir) / "state" / "tags.jsonl"
    if tp.exists():
        for line in tp.read_text().split("\n"):  # JSONL: newline only, not splitlines()
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            recs += 1
            ok += rec.get("sv") in (None, scan_version) and rec.get("pv") in (None, tag_version)
    return cur, eq, recs, ok


def eval_numbers(loc):
    """(mrr, found, probes) from the index's own eval with its output discarded; None when
    the index has no probes.yaml; "empty" when it lists no probes yet; "error" when no MRR
    line came back."""
    if not (loc.cfg_dir / "probes.yaml").exists():
        return None
    try:
        if not (yaml.safe_load((loc.cfg_dir / "probes.yaml").read_text()) or {}).get("probes"):
            return "empty"
    except (yaml.YAMLError, AttributeError):
        return "error"
    from . import search_cmd
    buf, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        try:
            search_cmd.run_index(loc, ["eval"])
        except SystemExit:
            pass
    m = re.search(r"MRR ([0-9.]+) over (\d+) probes, (\d+) found", buf.getvalue())
    return (float(m.group(1)), int(m.group(3)), int(m.group(2))) if m else "error"


def _yn(b):
    return "yes" if b else "no"


def allowlist_flags(path):
    """Booleans only: (pairing off, keep literals present, keep_patterns present)."""
    try:
        spec = yaml.safe_load(Path(path).read_text()) or {}
    except (OSError, yaml.YAMLError):
        return None
    return (spec.get("anon_id_pairing", True) is False, bool(spec.get("keep")),
            bool(spec.get("keep_patterns")))


def verify(name, baseline=None, compare_old=None, out=None):
    out = out or sys.stdout
    """Print numbers for an index; returns the number of mismatches and failures."""
    r = indexes.refresh()
    from .memory import session_parse, session_tagger
    from .memory.privacy_scan import RuleSet, Scanner
    loc = indexes.locate(indexes.load_registry(), name)
    entry = loc.entry
    bad = 0

    def say(key, value):
        print("%s: %s" % (key, value), file=out)

    def flag(key, ok, extra=""):
        nonlocal bad
        bad += not ok
        say(key, "ok" if ok else "MISMATCH" + (" (%s)" % extra if extra else ""))

    say("index", name)
    say("group", entry.get("group"))
    scanner = tc = sig = None
    if loc.is_session:
        priv = entry.get("privacy") or {}
        try:
            scanner = Scanner(r.CONFIG_ROOT / priv["allowlist"],
                              r.CONFIG_ROOT / priv["denylist"] if priv.get("denylist") else None,
                              RuleSet(r.SETTINGS.rule_packs))
            problems = r.protection_problems(entry.get("group"), scanner,
                                             doc_options=entry.get("doc_options"))
            say("protection", "ok" if not problems else "REFUSED: " + "; ".join(problems))
            bad += bool(problems)
            tc = session_tagger.TagConfig(yaml.safe_load((r.CONFIG_ROOT / entry["tags"]).read_text()),
                                          r.SETTINGS.default_guidance,
                                          r.source_name(entry.get("transcripts") or {}))
        except Exception as e:
            say("protection", "FAILED: scanner or tags did not load (%s)" % type(e).__name__)
            bad += 1
            scanner = tc = None
        flags = allowlist_flags(r.CONFIG_ROOT / (entry.get("privacy") or {}).get("allowlist", "-"))
        if flags is not None:
            say("allowlist anon_id_pairing false", _yn(flags[0]))
            say("allowlist keep literals", _yn(flags[1]))
            say("allowlist keep_patterns", _yn(flags[2]))
        say("registry doc_options.column_denylist override",
            _yn("column_denylist" in (entry.get("doc_options") or {})))

    ev = eval_numbers(loc)
    mrr = found = probes = None
    if ev == "error":
        say("eval", "FAILED (no MRR line)")
        bad += 1
    elif ev is None:
        say("eval", "no probes.yaml")
    elif ev == "empty":
        say("eval", "no probes yet")
    else:
        mrr, found, probes = ev
        say("eval MRR", "%.3f" % mrr)
        say("eval found/probes", "%d/%d" % (found, probes))
    ex, tags = counts(loc.data_dir)
    sha, nfiles = corpus_digest(loc.data_dir)
    say("exchanges", ex)
    say("tags", tags)
    say("corpus files", nfiles)
    say("corpus checksum", sha)

    if scanner is not None:
        sig = "%s|%s" % (scanner.version, session_parse.opts_sig(
            session_parse.parse_opts(entry.get("transcripts"))))
        cur, eq, recs, ok = stored_versions(loc.data_dir, sig, scanner.version, tc.version)
        say("stored parse signature equal", "%d of %d cursors" % (eq, cur))
        say("stored tag versions current", "%d of %d records" % (ok, recs))
        bad += (eq != cur) + (ok != recs)

    if baseline:
        base = json.loads(Path(os.path.expanduser(baseline)).read_text()).get(name)
        if base is None:
            say("baseline", "no entry for this index")
            bad += 1
        else:
            for key, mine in (("corpus_sha", sha), ("corpus_files", nfiles), ("exchanges", ex),
                              ("tags", tags), ("probes", probes), ("found", found)):
                if base.get(key) is not None:
                    flag("baseline %s" % key, base[key] == mine, "%s vs %s" % (base[key], mine))
            if base.get("mrr") is not None:
                flag("baseline MRR", mrr is not None and abs(base["mrr"] - mrr) < 0.0005,
                     "%s vs %s" % (base["mrr"], "%.3f" % mrr if mrr is not None else "none"))

    if compare_old:
        _compare_old(loc, name, Path(os.path.expanduser(compare_old)).resolve(), scanner, tc, sig,
                     (sha, nfiles, ex, tags), say, flag)
    say("result", "OK" if not bad else "%d problem(s)" % bad)
    return bad


def _compare_old(loc, name, old_root, scanner, tc, sig, mine, say, flag):
    from .memory import session_parse, session_tagger
    from .memory.privacy_scan import RuleSet, Scanner
    r = indexes.refresh()
    sha, nfiles, ex, tags = mine
    old_reg = r.load_registry(old_root / "registry.yaml", old_root / "registry.d")
    old_dir = old_root / name
    if name not in old_reg["indexes"] or not old_dir.is_dir():
        flag("old index found", False)
        return
    old_entry = old_reg["indexes"][name]
    osha, ofiles = corpus_digest(old_dir)
    oex, otags = counts(old_dir)
    flag("old corpus checksum equal", osha == sha, "%s vs %s" % (osha, sha))
    flag("old corpus file count equal", ofiles == nfiles)
    flag("old exchange count equal", oex == ex)
    flag("old tag count equal", otags == tags)
    if scanner is not None and tc is not None:
        priv = old_entry.get("privacy") or {}
        try:
            oscan = Scanner(old_root / priv["allowlist"],
                            old_root / priv["denylist"] if priv.get("denylist") else None,
                            RuleSet(r.SETTINGS.rule_packs))
            otc = session_tagger.TagConfig(yaml.safe_load((old_root / old_entry["tags"]).read_text()),
                                           r.SETTINGS.default_guidance,
                                           source=r.source_name(old_entry.get("transcripts")))
        except Exception as e:
            # A YAML error quotes the offending line, which may be an allowlisted name;
            # verify reports numbers and names of checks only.
            flag("old scanner and tags loaded", False, type(e).__name__)
            return
        osig = "%s|%s" % (oscan.version, session_parse.opts_sig(
            session_parse.parse_opts(old_entry.get("transcripts"))))
        flag("Scanner.version equal to the old index's", oscan.version == scanner.version)
        flag("TagConfig.version equal to the old index's", otc.version == tc.version)
        flag("parse signature equal to the old index's", osig == sig)
        cur, eq, recs, ok = stored_versions(old_dir, sig, scanner.version, tc.version)
        flag("old stored parse signatures match the new code", eq == cur, "%d of %d" % (eq, cur))
        flag("old stored tag versions match the new code", ok == recs, "%d of %d" % (ok, recs))
    old_cfg = old_dir / "search_config.yaml"
    if old_cfg.exists():
        new_cfg, theirs = indexes.search_config(loc), yaml.safe_load(old_cfg.read_text())
        # Paths aside: the index_path itself moves with the data, but the database file it
        # names must stay the same.
        flag("effective search config equal to the old one, paths aside",
             _strip(new_cfg) == _strip(theirs)
             and Path(str(new_cfg["index_path"])).name == Path(str(theirs.get("index_path", ""))).name)
