"""Provider labels carried by session exchanges."""
import re
from itertools import islice
from pathlib import Path

PROVIDERS = ("claude", "codex", "antigravity")
EXCHANGE_ID_RE = re.compile(r"^(?:S[0-9a-f]{6,12}-)?(?:EX|AQ)-\d{4,}$")


def is_exchange_path(path):
    path = Path(path)
    return (path.parts[:2] == ("corpus", "session") and path.suffix == ".md"
            and EXCHANGE_ID_RE.fullmatch(path.name.split(".", 1)[0]) is not None)


def exchange_provider(lines):
    """Read the optional Source field between Session and Tools in the exchange header."""
    source = next(islice(lines, 4, 5), "").rstrip("\r\n")
    if not source.startswith("Source: "):
        return "claude"
    provider = source.removeprefix("Source: ").partition(" | Folder: ")[0]
    return provider if provider in PROVIDERS else None


def format_counts(counts):
    return ", ".join("%s %d" % (p, counts[p]) for p in PROVIDERS
                     if type(counts.get(p)) is int and counts[p] >= 0) or "none"
