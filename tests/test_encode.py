import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cs2hl import encode
from cs2hl.game import Clip, Cs2


def command(take, **kw):
    """The ffmpeg command encode_clip would run."""
    with mock.patch.object(encode.subprocess, "run") as run:
        encode.encode_clip([take], Path("out.mp4"), 240, 4, kw.pop("codec", "h264_nvenc"), 23, **kw)
    return run.call_args.args[0]


class EncodeTest(unittest.TestCase):
    def setUp(self):
        self.take = encode.Take(["-i", "f%08d.jpg"], 480, Path("missing.wav"))

    def test_blur_strength_widens_the_blend(self):
        self.assertEqual([encode.blend_frames(4, s) for s in (1.0, 1.25, 1.5, 1.75, 2.0)], [4, 5, 6, 7, 8])
        self.assertEqual(encode.blend_frames(1, 2.0), 1)  # Smooth mode has no blur
        graph = command(self.take, blur=1.5)[command(self.take, blur=1.5).index("-filter_complex") + 1]
        self.assertIn("tmix=frames=6,fps=60", graph)

    def test_compact_is_small_compatible_h264(self):
        cmd = command(self.take, profile="compact", compact_level=3, source_height=1440)
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("scale=-2:720:", graph)
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "libx264")
        self.assertEqual(cmd[cmd.index("-pix_fmt") + 1], "yuv420p")
        self.assertEqual(cmd[cmd.index("-b:a") + 1], "128k")

    def test_compact_never_upscales(self):
        cmd = command(self.take, profile="compact", compact_level=5, source_height=1080)
        self.assertNotIn("scale=-2:", cmd[cmd.index("-filter_complex") + 1])

    def test_clean_encodes_sharper_and_full_colour_only_where_supported(self):
        cmd = command(self.take, profile="clean", full_color=True)
        self.assertEqual(cmd[cmd.index("-cq") + 1], "17")
        self.assertEqual(cmd[cmd.index("-pix_fmt") + 1], "yuv444p")
        amf = command(self.take, profile="clean", full_color=True, codec="h264_amf")
        self.assertIn("format=yuv420p", amf[amf.index("-filter_complex") + 1])

    def test_standard_writes_bt709(self):
        cmd = command(self.take)
        self.assertEqual(cmd[cmd.index("-colorspace") + 1], "bt709")
        self.assertIn("in_color_matrix=bt601:in_range=pc", cmd[cmd.index("-filter_complex") + 1])


class PackerTest(unittest.TestCase):
    def test_converts_finished_frames_in_batches(self):
        with tempfile.TemporaryDirectory() as tmp:
            take = Path(tmp) / "20261015_1"
            take.mkdir()
            for i in range(encode.PACK_BATCH + 5):
                (take / f"h00s00{i:08d}.tga").write_bytes(b"")
            packer = encode.FramePacker(Path(tmp), 240)
            calls = []
            packer._convert = lambda pack, first, count: (calls.append((first, count)), setattr(pack, "frames", pack.frames + count), pack.segments.append(take / "x.mkv"))
            packer.sweep(final=False)  # one full batch; the rest waits (newest frame may be half written)
            self.assertEqual(calls, [(0, encode.PACK_BATCH)])
            for i in range(encode.PACK_BATCH):
                (take / f"h00s00{i:08d}.tga").unlink()
            packer.sweep(final=True)
            self.assertEqual(calls, [(0, encode.PACK_BATCH), (encode.PACK_BATCH, 5)])
            t = packer.take("h00s00")
            self.assertEqual((t.frames, t.image_sequence), (encode.PACK_BATCH + 5, False))


class RecordFormatTest(unittest.TestCase):
    def test_clean_records_tga(self):
        acts = Cs2.actions([Clip("h00s00", 1000, 2000, 3)], 240, image_format="tga")
        cmds = [a["cmd"] for a in acts[0]["actions"]]
        self.assertIn("startmovie h00s00 tga wav", cmds)
        self.assertIn("startmovie h00s00 jpg wav", [a["cmd"] for a in Cs2.actions([Clip("h00s00", 1000, 2000, 3)], 240)[0]["actions"]])


if __name__ == "__main__":
    unittest.main()
