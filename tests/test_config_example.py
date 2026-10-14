import tomllib
import unittest
from dataclasses import fields
from pathlib import Path

from cs2hl import config

EXAMPLE = Path(__file__).parent.parent / "config.example.toml"


class ExampleConfigTest(unittest.TestCase):
    def test_example_matches_defaults(self):
        data = tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))
        defaults = config.Config().to_dict()
        self.assertEqual(set(data), {f.name for f in fields(config.Config)})
        self.assertEqual(data, defaults)


if __name__ == "__main__":
    unittest.main()
