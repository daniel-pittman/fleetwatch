"""`fleetwatch install`, `fleetwatch uninstall` and `fleetwatch doctor`, over the inventory in dependencies.py.

install writes and starts every scheduled job, creates fleetwatch's folders, and retires each older job a
fleetwatch job replaces (unloaded, its plist moved to the state folder's backup folder, never deleted).
It is idempotent: a job that is current and loaded is left alone. uninstall stops and removes fleetwatch's
own jobs and leaves the backups and every folder in place. doctor checks every entry and exits 1 when
something is wrong; --fix makes only the safe repairs (install what is missing or not loaded, create a
missing folder, retire an older job) and reports everything else.
"""
import argparse
import json
import sys

from . import dependencies as deps
from . import services

MARK = {deps.OK: "ok  ", deps.WARN: "warn", deps.FAIL: "FAIL"}


def _context(out, dry_run=False, manager=None, now=None):
    return deps.Context(manager or services.Manager(), out or sys.stdout, dry_run, now)


def install(ctx):
    """Install every folder and job, then retire the older jobs each installed job replaces. True when every
    step worked. An older job is retired only once its replacement is confirmed loaded; when the replacement
    does not load, the older job keeps running and install fails, so the work is never left undone."""
    ok = True
    m = ctx.manager
    ctx.say("folders%s:" % (" (dry run, nothing changes)" if ctx.dry_run else ""))
    if all(f.path().is_dir() for f in deps.FOLDERS if f.create):
        ctx.say("  in place")
    for f in deps.FOLDERS:
        ok = f.install(ctx) and ok
    for job in deps.JOBS:
        ctx.say("%s:" % job.name)
        done = job.install(ctx)
        ok = done and ok
        if not job.legacy or not m.darwin or (job.live_own(m) and not ctx.dry_run):
            continue                  # no older job (they are launchd agents), or the job's own install retired it
        if ctx.dry_run or (done and m.loaded(job.manager_label(m)) is True):
            for label in job.legacy:
                ok = deps.retire(label, ctx) and ok
        elif done and m.available:
            ok = False
            ctx.say("  %s is not loaded, so %s left as %s" % (job.label, ", ".join(job.legacy),
                                                            "it is" if len(job.legacy) == 1 else "they are"))
        elif not done:
            ctx.say("  the older job%s %s left as %s" % ("" if len(job.legacy) == 1 else "s", ", ".join(job.legacy),
                                                         "it is" if len(job.legacy) == 1 else "they are"))
        else:
            for label in job.legacy:
                deps.retire(label, ctx)    # says why it cannot run
    return ok


def uninstall(ctx):
    ok = True
    for job in deps.JOBS:
        ctx.say("%s:" % job.name)
        if not job.installed(ctx.manager) and ctx.manager.loaded(job.manager_label(ctx.manager)) is not True:
            ctx.say("  not installed")
            continue
        ok = job.uninstall(ctx) and ok
    backups = deps.backup_dir()
    if backups.is_dir() and any(backups.iterdir()):
        ctx.say("older jobs retired by install are kept in %s; to bring one back, move its plist to %s and load "
                "it" % (deps._home(backups), deps._home(deps.schedule.launch_agents_dir())))
    ctx.say("folders, the archive and the indexes are left in place")
    return ok


def checks(ctx):
    rows = []
    for entry in deps.INVENTORY:
        rows += entry.check(ctx)
    rows += deps.duplicate_checks(ctx)
    rows += deps.archive_checks(ctx)
    try:
        rows += deps.index_checks(ctx)
    except Exception as e:    # an index registry that cannot be read must not stop the other checks
        rows.append(deps.Check("indexes", "failure streaks", deps.WARN, "could not be read: %s"
                               % deps._first_line(e)))
    return rows


def fix(ctx, rows):
    """The safe repairs for the failures in `rows`. Returns the names of what it repaired."""
    failing = {(r.item, r.check) for r in rows if r.status == deps.FAIL}
    done = []
    for f in deps.FOLDERS:
        if f.create and (f.name, "exists") in failing:
            if f.install(ctx):
                done.append(f.name)
    for job in deps.JOBS:
        if {(job.name, "installed"), (job.name, "loaded")} & failing:
            ctx.say("%s:" % job.name)
            if job.install(ctx):
                done.append(job.name)
        elif (job.name, "running") in failing:
            ctx.say("%s:" % job.name)
            if job.restart(ctx):
                done.append(job.name)
        replacement_loaded = ctx.manager.loaded(job.manager_label(ctx.manager)) is True
        if (job.name, "no older job") in failing and replacement_loaded:
            for label in job.legacy:
                ctx.say("%s, older job %s:" % (job.name, label))
                if deps.retire(label, ctx):
                    done.append(label)
    return done


def report(rows, out):
    width = max((len(r.item) for r in rows), default=0)
    for r in rows:
        line = "%s  %-*s  %s: %s" % (MARK[r.status], width, r.item, r.check, r.detail)
        print(line.rstrip(), file=out)
        if r.status != deps.OK and r.fix:
            print("      %*s  fix: %s" % (width, "", r.fix), file=out)
    bad = sum(1 for r in rows if r.status == deps.FAIL)
    warn = sum(1 for r in rows if r.status == deps.WARN)
    print("%s; %d warning%s" % ("%d problem%s" % (bad, "" if bad == 1 else "s") if bad else "no problems",
                                 warn, "" if warn == 1 else "s"), file=out)


def install_main(argv, out=None, manager=None):
    p = argparse.ArgumentParser(prog="fleetwatch install",
                                description="Install and start every scheduled job, and retire the older jobs "
                                            "they replace (unloaded, plist kept in the state folder).")
    p.add_argument("--dry-run", action="store_true", help="say what would change and change nothing")
    args = p.parse_args(argv)
    ctx = _context(out, args.dry_run, manager)
    ok = install(ctx)
    if not args.dry_run:
        ctx.say("next: fleetwatch doctor")
    return 0 if ok else 1


def uninstall_main(argv, out=None, manager=None):
    p = argparse.ArgumentParser(prog="fleetwatch uninstall",
                                description="Stop and remove every fleetwatch scheduled job. Folders, the archive, "
                                            "the indexes and retired older jobs' backups stay.")
    p.add_argument("--dry-run", action="store_true", help="say what would change and change nothing")
    args = p.parse_args(argv)
    return 0 if uninstall(_context(out, args.dry_run, manager)) else 1


def doctor_main(argv, out=None, manager=None, now=None):
    p = argparse.ArgumentParser(prog="fleetwatch doctor",
                                description="Check every scheduled job, folder and program fleetwatch relies on, "
                                            "the archive and the indexes. Exits 1 when something is wrong.")
    p.add_argument("--json", action="store_true", help="print the checks as JSON")
    p.add_argument("--fix", action="store_true",
                   help="install what is missing or not loaded, create missing folders, retire older jobs; "
                        "report the rest")
    p.add_argument("--inventory", action="store_true", help="print the inventory as Markdown (docs/dependencies.md)")
    args = p.parse_args(argv)
    out = out or sys.stdout
    if args.inventory:
        print(deps.markdown(), end="", file=out)
        return 0
    ctx = _context(out, manager=manager, now=now)
    rows = checks(ctx)
    repaired = []
    if args.fix and any(r.status == deps.FAIL for r in rows):
        if args.json:
            ctx.out = sys.stderr
        repaired = fix(ctx, rows)
        ctx.out = out
        rows = checks(ctx)
    bad = any(r.status == deps.FAIL for r in rows)
    if args.json:
        print(json.dumps({"ok": not bad, "fixed": repaired, "checks": [r.as_dict() for r in rows]}, indent=1),
              file=out)
    else:
        if args.fix:
            print("fixed: %s" % (", ".join(repaired) or "nothing"), file=out)
        report(rows, out)
    return 1 if bad else 0
