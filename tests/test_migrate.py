"""The one-time move of fleet's old folders to fleetwatch. Temp directories only."""
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from fleetwatch import migrate  # noqa: E402


def _put(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)


class MigrateFolderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old = os.path.join(self.tmp.name, "fleet")
        self.new = os.path.join(self.tmp.name, "fleetwatch")

    def test_fresh_install_does_nothing(self):
        self.assertFalse(migrate.migrate_folder(self.old, self.new))
        self.assertFalse(os.path.lexists(self.old))
        self.assertFalse(os.path.lexists(self.new))

    def test_old_only_moves_and_leaves_a_symlink(self):
        _put(os.path.join(self.old, "a.json"), "data")
        self.assertTrue(migrate.migrate_folder(self.old, self.new))
        self.assertTrue(os.path.islink(self.old))
        self.assertEqual(os.readlink(self.old), self.new)
        with open(os.path.join(self.new, "a.json")) as fh:
            self.assertEqual(fh.read(), "data")
        # A write through the old path lands in the new folder.
        _put(os.path.join(self.old, "b.json"), "more")
        self.assertTrue(os.path.exists(os.path.join(self.new, "b.json")))

    def test_second_run_is_a_no_op(self):
        _put(os.path.join(self.old, "a.json"))
        self.assertTrue(migrate.migrate_folder(self.old, self.new))
        self.assertFalse(migrate.migrate_folder(self.old, self.new))

    def test_both_present_leaves_the_old_alone(self):
        _put(os.path.join(self.old, "a.json"), "old")
        _put(os.path.join(self.new, "b.json"), "new")
        self.assertFalse(migrate.migrate_folder(self.old, self.new))
        self.assertFalse(os.path.islink(self.old))
        self.assertEqual(os.listdir(self.old), ["a.json"])
        self.assertEqual(os.listdir(self.new), ["b.json"])

    def test_old_already_a_symlink_is_not_moved(self):
        elsewhere = os.path.join(self.tmp.name, "elsewhere")
        os.makedirs(elsewhere)
        os.symlink(elsewhere, self.old)
        self.assertFalse(migrate.migrate_folder(self.old, self.new))
        self.assertEqual(os.readlink(self.old), elsewhere)
        self.assertFalse(os.path.lexists(self.new))

    def test_concurrent_first_runs_end_on_the_new_folder(self):
        for i in range(20):
            for name in ("fleet", "fleetwatch"):
                p = os.path.join(self.tmp.name, "r%d" % i, name)
                if name == "fleet":
                    _put(os.path.join(p, "a.json"))
            old = os.path.join(self.tmp.name, "r%d" % i, "fleet")
            new = os.path.join(self.tmp.name, "r%d" % i, "fleetwatch")
            results, errors = [], []
            barrier = threading.Barrier(4)

            def run():
                try:
                    barrier.wait()
                    results.append(migrate.migrate_folder(old, new))
                except Exception as exc:  # pragma: no cover
                    errors.append(exc)

            threads = [threading.Thread(target=run) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            self.assertEqual(results.count(True), 1)
            self.assertTrue(os.path.exists(os.path.join(new, "a.json")))
            self.assertTrue(os.path.islink(old))


class MigrateDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        self.config = os.path.join(self.home, ".config")
        self.state = os.path.join(self.home, ".claude", "state")
        self.share = os.path.join(self.home, ".local", "share")
        for parent in (self.config, self.state, self.share):
            _put(os.path.join(parent, "fleet", "f.txt"))
        _put(os.path.join(self.config, "fleet", "groups.toml"))
        _put(os.path.join(self.state, "fleet", "alpha.json"), '{"session": "Alpha", "sessionId": "s1"}')
        os.makedirs(os.path.join(self.share, "fleet", "indexes", "demo", "state"))

    def test_moves_all_three_default_folders(self):
        moved = migrate.migrate_defaults(environ={}, home=self.home)
        self.assertEqual(len(moved), 3)
        for parent in (self.config, self.state, self.share):
            self.assertTrue(os.path.isfile(os.path.join(parent, "fleetwatch", "f.txt")))
            self.assertTrue(os.path.islink(os.path.join(parent, "fleet")))

    def test_a_fleet_folder_of_another_program_is_never_moved(self):
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        home = other.name
        for parent, f, text in ((".config", "fleet.conf", "x"), (".claude/state", "ships.json", '{"ship": 1}'),
                                (".local/share", "indexes/notes.txt", "x")):
            _put(os.path.join(home, parent, "fleet", f), text)
        self.assertEqual(migrate.migrate_defaults(environ={}, home=home), [])
        for parent in (".config", ".claude/state", ".local/share"):
            self.assertFalse(os.path.islink(os.path.join(home, parent, "fleet")))
            self.assertFalse(os.path.lexists(os.path.join(home, parent, "fleetwatch")))

    def test_environment_overrides_are_never_touched(self):
        env = {"FLEETWATCH_CONFIG_DIR": "/x", "FLEETWATCH_STATUS_DIR": "/y"}
        moved = migrate.migrate_defaults(environ=env, home=self.home)
        self.assertEqual(moved, [os.path.join(self.share, "fleetwatch")])
        self.assertFalse(os.path.islink(os.path.join(self.config, "fleet")))
        self.assertFalse(os.path.islink(os.path.join(self.state, "fleet")))

    def test_no_migrate_switch_turns_it_off(self):
        self.assertEqual(migrate.migrate_defaults(environ={"FLEETWATCH_NO_MIGRATE": "1"}, home=self.home), [])
        self.assertFalse(os.path.islink(os.path.join(self.config, "fleet")))

    def test_xdg_data_home_decides_the_data_folder(self):
        xdg = os.path.join(self.home, "xdg")
        _put(os.path.join(xdg, "fleet", "f.txt"))
        os.makedirs(os.path.join(xdg, "fleet", "indexes", "demo", "corpus"))
        migrate.migrate_defaults(environ={"XDG_DATA_HOME": xdg}, home=self.home)
        self.assertTrue(os.path.isfile(os.path.join(xdg, "fleetwatch", "f.txt")))
        self.assertFalse(os.path.islink(os.path.join(self.share, "fleet")))



IMPORT_ALL = """
import importlib, pkgutil, sys
import fleetwatch
skipped = []
for m in pkgutil.walk_packages(fleetwatch.__path__, "fleetwatch."):
    try:
        importlib.import_module(m.name)
    except ImportError as e:
        if m.name.startswith("fleetwatch.memory"):
            raise
        skipped.append(m.name)
print("imported; optional modules skipped: %d" % len(skipped))
"""


class ImportMovesNothingTests(unittest.TestCase):
    """Importing any fleetwatch module, the indexer's refresh module included, must never move
    a folder: only a command-line entry point migrates."""

    def test_importing_every_module_leaves_old_folders_in_place(self):
        import subprocess
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = os.path.realpath(tmp.name)
        olds = [os.path.join(home, ".config", "fleet"), os.path.join(home, ".claude", "state", "fleet"),
                os.path.join(home, ".local", "share", "fleet")]
        for d in olds:
            _put(os.path.join(d, "f.txt"))
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("FLEETWATCH_", "FLEET_", "XDG_"))}
        env.update(HOME=home, PYTHONPATH=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
        p = subprocess.run([sys.executable, "-c", IMPORT_ALL], env=env, capture_output=True, text=True,
                           timeout=300, cwd=home)
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        for d in olds:
            self.assertFalse(os.path.islink(d), d)
            self.assertTrue(os.path.isfile(os.path.join(d, "f.txt")), d)
            self.assertFalse(os.path.lexists(d[:-len("fleet")] + "fleetwatch"), d)



ZONE_AFTER_MIGRATE = """
from datetime import datetime
import fleetwatch.__main__
from fleetwatch import data, migrate
migrate.migrate_defaults()
print(datetime.fromtimestamp(1_780_000_000, data.LOCAL_TZ).utcoffset().total_seconds())
"""


class ZoneReadAfterMigrationTests(unittest.TestCase):
    """The local zone comes from the settings.toml the migration has just moved into place,
    not from the folder that existed when fleetwatch was imported."""

    def test_the_timezone_setting_in_a_migrated_folder_is_used(self):
        import subprocess
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = os.path.realpath(tmp.name)
        _put(os.path.join(home, ".config", "fleet", "settings.toml"), 'timezone = "Pacific/Kiritimati"\n')
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("FLEETWATCH_", "FLEET_", "XDG_")) and k != "TZ"}
        env.update(HOME=home, PYTHONPATH=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
        p = subprocess.run([sys.executable, "-c", ZONE_AFTER_MIGRATE], env=env, capture_output=True,
                           text=True, timeout=120, cwd=home)
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        self.assertEqual(float(p.stdout.split()[-1]), 14 * 3600)


if __name__ == "__main__":
    unittest.main()
