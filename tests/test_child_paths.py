"""A module file in the folder fleetwatch runs from is never imported, by the launcher or by
any Python child it starts (the engine build, the embedder probe, the encoder, the reranker)."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("FLEETWATCH_NO_MIGRATE", "1")

from fleetwatch import settings
from fleetwatch.memory import refresh
from fleetwatch.search import engine

REPO = Path(__file__).resolve().parent.parent


class PlantedModules(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.base = Path(t.name).resolve()
        self.untrusted = self.base / "untrusted_repo"
        self.untrusted.mkdir()
        self.marker = self.base / "planted-ran"
        for mod in ("yaml", "json", "sentence_transformers"):
            (self.untrusted / (mod + ".py")).write_text(
                "open(%r, 'a').write(%r)\n" % (str(self.marker), mod + "\n"))
        cwd = os.getcwd()
        os.chdir(self.untrusted)
        self.addCleanup(os.chdir, cwd)
        env = dict(os.environ, PYTHONPATH=str(REPO), FLEETWATCH_NO_MIGRATE="1")
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        self.addCleanup(self.env.stop)

    def planted(self):
        return self.marker.read_text().split() if self.marker.exists() else []

    def test_the_launcher_does_not_import_from_its_working_directory(self):
        home = self.base / "home"
        (home / ".venvs" / "fleetwatch" / "bin").mkdir(parents=True)
        os.symlink(sys.executable, home / ".venvs" / "fleetwatch" / "bin" / "python")
        env = dict(os.environ, HOME=str(home), FLEETWATCH_CONFIG_DIR=str(self.base / "cfg"),
                   XDG_DATA_HOME=str(self.base / "data"), XDG_STATE_HOME=str(self.base / "state"))
        subprocess.run(["sh", str(REPO / "bin" / "fleetwatch"), "index", "list"], cwd=self.untrusted,
                       env=env, capture_output=True, timeout=120)
        self.assertEqual(self.planted(), [])

    def test_the_engine_build_child_does_not_import_from_the_callers_folder(self):
        old = refresh.SETTINGS
        self.addCleanup(refresh.configure, old)
        refresh.configure(settings.indexer(raw={"paths": {"data_dir": str(self.base / "data")}},
                                           cfg_dir=self.base / "cfg"))
        c = refresh.CONFIG_ROOT / "demo"
        c.mkdir(parents=True)
        (c / "allow.yaml").write_text("version: 1\nperson_names: false\n")
        (c / "tags.yaml").write_text("description: d\nareas: {a: a}\nkinds: {k: k}\n")
        ix = refresh.Index("demo", {"group": "research", "privacy": {"allowlist": "demo/allow.yaml"},
                                    "tags": "demo/tags.yaml"}, {})
        ix.dir.mkdir(parents=True)
        try:
            ix.build()
        except RuntimeError:
            pass        # the demo index has no corpus; only the imports matter here
        self.assertEqual(self.planted(), [])

    def test_the_embedder_probe_does_not_import_from_the_callers_folder(self):
        cfg = {"similarity": {"embedding_pythons": [sys.executable]}}
        with mock.patch.dict(sys.modules, {"sentence_transformers": None}):
            engine.find_embedder(cfg)
        self.assertEqual(self.planted(), [])

    def test_the_encoder_and_reranker_children_do_not_import_from_the_callers_folder(self):
        if engine.np is None:
            self.skipTest("numpy unavailable")
        cfg = {"similarity": {"model": "m", "reranker": "r"}}
        with self.assertRaises(Exception):
            engine.encode_texts(cfg, ["x"], ("subprocess", sys.executable))
        engine.rerank_scores(cfg, "q", ["x"], ("subprocess", sys.executable), notes=[])
        self.assertEqual(self.planted(), [])


if __name__ == "__main__":
    unittest.main()
