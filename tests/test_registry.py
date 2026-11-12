"""The refresh driver's registry loader: registry.yaml inline entries versus registry.d
drop-ins, ambiguity, and a drop-in that does not parse."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from fleetwatch.memory import refresh


class Registry(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.reg = self.root / "registry.yaml"
        self.dir = self.root / "registry.d"
        self.dir.mkdir()

    def load(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            reg = refresh.load_registry(self.reg, self.dir)
        return reg, err.getvalue()

    def test_inline_only(self):
        self.reg.write_text("defaults: {tag: true}\nindexes:\n  alpha: {group: research}\n")
        self.dir.rmdir()
        reg, err = self.load()
        self.assertEqual(reg["indexes"], {"alpha": {"group": "research"}})
        self.assertEqual(reg["defaults"], {"tag": True})
        self.assertEqual(err, "")

    def test_dropin_wins_over_inline_with_a_warning(self):
        self.reg.write_text("indexes:\n  alpha: {group: research}\n  beta: {group: oss}\n")
        (self.dir / "alpha.yaml").write_text("indexes:\n  alpha: {group: personal}\n")
        (self.dir / "gamma.yaml").write_text("indexes:\n  gamma: {group: global}\n")
        reg, err = self.load()
        self.assertEqual(reg["indexes"], {"alpha": {"group": "personal"}, "beta": {"group": "oss"},
                                          "gamma": {"group": "global"}})
        self.assertIn("'alpha' is in registry.yaml and registry.d/alpha.yaml; using registry.d", err)

    def test_name_in_two_dropins_is_skipped_others_load(self):
        self.reg.write_text("indexes: {}\n")
        (self.dir / "a.yaml").write_text("indexes:\n  shared: {group: oss}\n  only-a: {group: oss}\n")
        (self.dir / "b.yaml").write_text("indexes:\n  shared: {group: research}\n")
        reg, err = self.load()
        self.assertEqual(reg["indexes"], {"only-a": {"group": "oss"}})
        self.assertIn("'shared' is defined in both registry.d/a.yaml and registry.d/b.yaml; skipped", err)

    def test_unparseable_dropin_is_skipped(self):
        self.reg.write_text("indexes:\n  alpha: {group: research}\n")
        (self.dir / "bad.yaml").write_text("indexes: [unclosed\n")
        (self.dir / "good.yaml").write_text("indexes:\n  delta: {group: oss}\n")
        reg, err = self.load()
        self.assertEqual(sorted(reg["indexes"]), ["alpha", "delta"])
        self.assertIn("registry.d/bad.yaml does not parse, skipped", err)

    def test_unknown_index_is_an_exception(self):
        with self.assertRaises(refresh.UnknownIndex) as cm:
            refresh.selected({"indexes": {"alpha": {}}}, "omega")
        self.assertEqual(str(cm.exception), "no index 'omega' in registry (have: alpha)")
        self.assertEqual(refresh.selected({"indexes": {"alpha": {}, "beta": {}}}, None),
                         ["alpha", "beta"])


if __name__ == "__main__":
    unittest.main()
