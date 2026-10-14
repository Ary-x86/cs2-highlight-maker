import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cs2hl import usercfg

VDF = '''"users"
{
\t"76561198000000001"
\t{
\t\t"PersonaName"\t\t"a"
\t\t"%s"\t\t"1791939597"
\t}
\t"76561198000000002"
\t{
\t\t"PersonaName"\t\t"b"
\t\t"%s"\t\t"1791937636"
\t}
}
'''


class ActiveAccountTest(unittest.TestCase):
    def check(self, key):
        root = Path(tempfile.mkdtemp())
        (root / "config").mkdir()
        (root / "config/loginusers.vdf").write_text(VDF % (key, key))
        with mock.patch.object(usercfg, "STEAM_ROOT", root):
            self.assertEqual(usercfg.active_account_cfg_dir(), root / "userdata/39734273/730/local/cfg")

    def test_newest_timestamp_any_case(self):
        self.check("Timestamp")
        self.check("timestamp")


if __name__ == "__main__":
    unittest.main()
