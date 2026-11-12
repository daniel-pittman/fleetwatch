"""Fuzzy ranking for the jump-to-session box. Pure functions, no I/O."""
import re

WORD = re.compile(r"[a-z0-9]+")


def _tier(tok, field):
    """0 prefix, 1 word-start or initials prefix, 2 substring, 3 subsequence, else None."""
    f = field.lower()
    if f.startswith(tok):
        return 0
    words = WORD.findall(f)
    if any(w.startswith(tok) for w in words) or "".join(w[0] for w in words).startswith(tok):
        return 1
    if tok in f:
        return 2
    it = iter(f)
    if all(c in it for c in tok):
        return 3
    return None


def score(query, fields):
    """Worst token tier, then the sum of tiers (lower is better); None when a token matches nothing.
    An empty query scores (0, 0) for everything."""
    toks = query.lower().split()
    tiers = []
    for tok in toks:
        best = [t for t in (_tier(tok, f) for f in fields if f) if t is not None]
        if not best:
            return None
        tiers.append(min(best))
    return (max(tiers), sum(tiers)) if tiers else (0, 0)


def rank(query, entries):
    """entries: dicts with name, persona, updated, and optionally `fields`, the texts to match instead
    of the name and persona. Best matches first; ties by recent activity."""
    scored = []
    for e in entries:
        s = score(query, e.get("fields") or [e["name"], e.get("persona") or ""])
        if s is not None:
            scored.append((s, -(e.get("updated") or 0), e["name"].lower(), e))
    scored.sort(key=lambda t: t[:3])
    return [t[3] for t in scored]
