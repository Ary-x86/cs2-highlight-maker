import tempfile
import unittest
from pathlib import Path

from cs2hl import videocfg

SAMPLE = '''"video.cfg"
{
\t"setting.defaultres"\t\t"1920"
\t"setting.fullscreen"\t\t"1"
\t"setting.shaderquality"\t\t"0"
}
'''


class VideoSettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = self.tmp / "cfg"
        self.cfg.mkdir()
        self.file = self.cfg / videocfg.VIDEO_FILE
        self.vs = videocfg.VideoSettings(self.cfg, self.tmp / "backup")

    def test_apply_then_restore_is_byte_identical(self):
        self.file.write_text(SAMPLE)
        self.vs.apply(videocfg.HIGH_QUALITY)
        values = videocfg.read(self.file)
        self.assertEqual(values["setting.shaderquality"], "1")
        self.assertEqual(values["setting.videocfg_ao_detail"], "3")
        self.assertEqual(values["setting.defaultres"], "1920")
        self.file.write_text(SAMPLE.replace('"1"', '"0"'))  # CS2 rewrites it on exit
        self.vs.restore()
        self.assertEqual(self.file.read_text(), SAMPLE)
        self.vs.restore()  # second call is a no-op
        self.assertEqual(self.file.read_text(), SAMPLE)

    def test_crashed_run_is_undone_by_next_apply(self):
        self.file.write_text(SAMPLE)
        self.vs.apply({"setting.fullscreen": "0"})
        again = videocfg.VideoSettings(self.cfg, self.tmp / "backup")
        again.apply({})
        again.restore()
        self.assertEqual(self.file.read_text(), SAMPLE)

    def test_missing_file_gets_fallback_display(self):
        self.vs.apply({})
        self.file.write_text(SAMPLE.replace('"setting.fullscreen"\t\t"1"', '"setting.fullscreen"\t\t"0"'))
        self.vs.restore()
        values = videocfg.read(self.file)
        for key, value in videocfg.FALLBACK_DISPLAY.items():
            self.assertEqual(values[key], value)

    def test_msaa_is_replaced_while_recording(self):
        self.file.write_text(SAMPLE.replace("}", '\t"setting.msaa_samples"\t\t"4"\n}'))
        self.assertEqual(videocfg.recording_changes(self.cfg, False), videocfg.NO_MSAA)
        self.file.write_text(SAMPLE)
        self.assertEqual(videocfg.recording_changes(self.cfg, False), {})
        self.assertEqual(videocfg.recording_changes(self.cfg, True)["setting.msaa_samples"], "0")


if __name__ == "__main__":
    unittest.main()
