"""The optional extras carry what their code imports in the fleetwatch process itself."""
import os
import re
import tomllib
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def extras():
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as fh:
        return tomllib.load(fh)["project"]["optional-dependencies"]


def pins(reqs):
    return {re.split(r"[=<>@ ]", r, 1)[0].lower(): r for r in reqs}


class Extras(unittest.TestCase):
    def test_memory_carries_numpy_at_the_search_pin(self):
        # The engine imports numpy in the host process even when another interpreter computes
        # the embeddings, so an install with only the memory extra must have it.
        ex = extras()
        self.assertIn("numpy", pins(ex["memory"]))
        self.assertEqual(pins(ex["memory"])["numpy"], pins(ex["search"])["numpy"])

    def test_every_extra_requirement_is_pinned(self):
        for name, reqs in extras().items():
            for r in reqs:
                self.assertTrue("==" in r or " @ " in r, "%s: %s" % (name, r))


if __name__ == "__main__":
    unittest.main()
