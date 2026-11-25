"""Board row keys for tests. A row about a session is keyed by the session's ID, never by its name
(views.node_key), so a test names a row by the ID its fixture registered."""
from fleetwatch import views


def ses(sid, helper_sid=None):
    """A Sessions-tab row, or the row of one of its helpers."""
    return views.node_key("ses", ("s", sid), *((("k", ("s", helper_sid))) if helper_sid else ()))


def needs(sid, *more):
    """A Needs-you row; `more` names a child, as in needs(sid, "q", "1")."""
    return views.node_key("needs", ("s", sid), *more)


def names(lst, kind):
    """The session names of a list's visible top-level rows of one kind, top to bottom."""
    return [lst._by_key[k].session for k in lst._order if k.startswith(kind + ":") and k.count(":") == 2]
