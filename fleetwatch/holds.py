"""Snooze and done for to-dos: the board's own memory, never a write to a session's status file.

An item is held by a key of the session's ID plus a hash of what it says (see data.item_key). A snoozed item
hides until its date, then comes back. A done item stays hidden for as long as the session keeps reporting
the same item; once a status leaves the item out or changes it, the key no longer matches and the hold is
dropped, so the item shows again if it returns.
"""
from datetime import date, datetime, time as time_cls, timedelta

from .data import LOCAL_TZ

SNOOZED, DONE = "snoozed", "done"


def day_start(day):
    """Epoch of local midnight at the start of a date."""
    return datetime.combine(day, time_cls(0), tzinfo=LOCAL_TZ).timestamp()


def tomorrow(now):
    return datetime.fromtimestamp(now, LOCAL_TZ).date() + timedelta(days=1)


def next_monday(now):
    """The Monday after today; a Monday itself gives the one a week on."""
    today = datetime.fromtimestamp(now, LOCAL_TZ).date()
    return today + timedelta(days=7 - today.weekday())


def parse_date(text, now):
    """The date typed into the snooze box: YYYY-MM-DD, later than today. ValueError names what is wrong."""
    try:
        day = date.fromisoformat(text.strip())
    except ValueError:
        raise ValueError("type a date as YYYY-MM-DD") from None
    if day <= datetime.fromtimestamp(now, LOCAL_TZ).date():
        raise ValueError("pick a day after today")
    return day


class Holds:
    """{key: {"until": epoch}} for snoozed items and {key: {"done": true}} for done ones."""

    def __init__(self, raw=None):
        self.items = {}
        for key, val in (raw or {}).items():
            if not isinstance(key, str) or not isinstance(val, dict):
                continue
            until = val.get("until")
            if isinstance(until, (int, float)) and not isinstance(until, bool):
                self.items[key] = {"until": float(until)}
            elif val.get("done") is True:
                self.items[key] = {"done": True}

    def as_dict(self):
        return {k: dict(v) for k, v in self.items.items()}

    def status(self, key, now):
        """None (visible), (SNOOZED, until) or (DONE, None)."""
        h = self.items.get(key)
        if h is None:
            return None
        if "done" in h:
            return DONE, None
        return (SNOOZED, h["until"]) if h["until"] > now else None

    def row_status(self, keys, now):
        """The hold shared by every key of a merged row, or None when any of them is visible."""
        found = [self.status(k, now) for k in keys]
        return found[0] if keys and all(found) else None

    def snooze(self, keys, until):
        for k in keys:
            self.items[k] = {"until": float(until)}

    def done(self, keys):
        for k in keys:
            self.items[k] = {"done": True}

    def clear(self, keys):
        for k in keys:
            self.items.pop(k, None)

    def prune(self, present, now):
        """Forget holds that no longer apply: a snooze whose date has passed, and a hold on an item the
        sessions no longer report. True when anything was dropped."""
        gone = [k for k, h in self.items.items()
                if ("until" in h and h["until"] <= now) or k not in present]
        for k in gone:
            del self.items[k]
        return bool(gone)

    def counts(self, rows, now):
        """(snoozed, done) over the held rows."""
        c = [self.row_status(r["keys"], now) for r in rows]
        return sum(1 for s in c if s and s[0] == SNOOZED), sum(1 for s in c if s and s[0] == DONE)
