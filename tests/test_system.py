import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cs2hl import config, system


class FindInstallsTest(unittest.TestCase):
    def test_library_from_libraryfolders(self):
        steam = Path(tempfile.mkdtemp())
        lib = Path(tempfile.mkdtemp()) / "Steam Library"
        game = lib / "steamapps/common/Counter-Strike Global Offensive"
        (game / system.CS2_LAUNCHER).parent.mkdir(parents=True)
        (game / system.CS2_LAUNCHER).write_text("")
        (lib / "steamapps/appmanifest_730.acf").write_text('"AppState"\n{\n\t"LastUpdated"\t\t"1700000000"\n}\n')
        (steam / "steamapps").mkdir()
        # Steam escapes backslashes in Windows paths
        escaped = str(lib).replace("\\", "\\\\")
        (steam / "steamapps/libraryfolders.vdf").write_text(f'"libraryfolders"\n{{\n\t"0"\n\t{{\n\t\t"path"\t\t"{escaped}"\n\t}}\n}}\n')
        with mock.patch.object(config, "STEAM_ROOT", steam):
            installs = config.find_cs2_installs()
        self.assertEqual(installs, [{"path": str(game), "updated": 1700000000}])

    def test_work_dir_is_per_user(self):
        self.assertTrue(str(system.default_work_dir()).startswith(str(Path.home())) or system.IS_WINDOWS)


class LinkTest(unittest.TestCase):
    def test_link_and_remove_keeps_target(self):
        root = Path(tempfile.mkdtemp())
        target = root / "target"
        target.mkdir()
        (target / "frame.jpg").write_text("x")
        link = root / "link"
        system.link_dir(link, target)
        self.assertTrue(system.is_link(link))
        self.assertTrue((link / "frame.jpg").exists())
        system.remove_link(link)
        self.assertFalse(link.exists())
        self.assertTrue((target / "frame.jpg").exists())


if __name__ == "__main__":
    unittest.main()
