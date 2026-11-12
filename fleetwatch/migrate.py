"""One-time move of the folders an earlier release kept under the old program name.

Each default folder (config, status files, indexer data) moves from `.../fleet` to `.../fleetwatch`
with one atomic rename, when it holds what fleet kept there (looks_like_fleet), and a symlink left at the old path keeps an older binary that is still
running on the same files. A folder chosen by an environment variable is never touched, and
FLEETWATCH_NO_MIGRATE=1 turns the whole move off.
"""
import errno
import glob
import json
import os
import stat

OLD, NEW = "fleet", "fleetwatch"
# Files only fleet's own config folder holds; a `fleet` folder without one belongs to some
# other program and is never moved.
CONFIG_MARKERS = ("groups.toml", "state.json", "prune.toml", "parked.json", "resume-cache.json",
                  "settings.toml")
STATUS_SAMPLE = 20
STATUS_MAX_BYTES = 256 * 1024


def _is_real_dir(path):
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def migrate_folder(old, new):
    """Move the real directory `old` to `new` and leave a symlink at `old`. True when this call moved it.

    Does nothing when `new` exists (it wins; `old` is left alone) or when `old` is missing or is a
    symlink. Two processes racing here are safe: rename is atomic, so exactly one wins, and the loser
    finds `new` in place and returns False. The symlink follows the rename by a moment, so an old
    binary that writes in that gap creates its own `old` folder and the symlink is skipped.
    """
    if os.path.lexists(new) or not _is_real_dir(old):
        return False
    try:
        os.rename(old, new)
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ENOTEMPTY, errno.EEXIST, errno.ENOTDIR, errno.EISDIR):
            return False
        raise
    try:
        os.symlink(new, old)
    except FileExistsError:
        pass
    return True


def looks_like_fleet(path, kind):
    """True when the folder at `path` holds what fleet kept there: a config file of its own
    (config), a status file naming a sessionId (status), or an index with corpus or state
    folders (data)."""
    if kind == "config":
        return any(os.path.isfile(os.path.join(path, m)) for m in CONFIG_MARKERS)
    if kind == "status":
        for f in sorted(glob.glob(os.path.join(path, "*.json")))[:STATUS_SAMPLE]:
            try:
                if os.path.getsize(f) > STATUS_MAX_BYTES:
                    continue
                with open(f, encoding="utf-8") as fh:
                    d = json.load(fh)
            except (OSError, ValueError):
                continue
            if isinstance(d, dict) and isinstance(d.get("sessionId"), str):
                return True
        return False
    return any(os.path.isdir(os.path.join(d, sub)) for d in glob.glob(os.path.join(path, "indexes", "*"))
               for sub in ("corpus", "state"))


def migrate_defaults(environ=None, home=None):
    """Migrate the config, status and data folders that sit at their default paths."""
    env = os.environ if environ is None else environ
    if env.get("FLEETWATCH_NO_MIGRATE"):
        return []
    home = home or os.path.expanduser("~")
    base = env.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    pairs = []
    if not env.get("FLEETWATCH_CONFIG_DIR"):
        pairs.append((os.path.join(home, ".config"), "config"))
    if not env.get("FLEETWATCH_STATUS_DIR"):
        pairs.append((os.path.join(home, ".claude", "state"), "status"))
    pairs.append((base, "data"))
    moved = []
    for parent, kind in pairs:
        old, new = os.path.join(parent, OLD), os.path.join(parent, NEW)
        try:
            if _is_real_dir(old) and looks_like_fleet(old, kind) and migrate_folder(old, new):
                moved.append(new)
        except OSError:
            continue
    return moved
