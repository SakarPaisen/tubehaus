"""Unit tests for the parts of the TubeHaus server that don't need the network.

    python3 -m unittest discover -s tests
"""
import base64
import importlib.util
import os
import pathlib
import re
import tempfile
import unittest

STATE = tempfile.mkdtemp(prefix="tubehaus-test-")
os.environ.update(HT_STATE=STATE, HT_YT_PROXY="", HT_WARP_ROTATE="0")
spec = importlib.util.spec_from_file_location(
    "tubehaus_server", pathlib.Path(__file__).resolve().parent.parent / "server" / "tubehaus_server.py")
ts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ts)


class BotDetection(unittest.TestCase):
    def test_bot_wall(self):
        d = {"playabilityStatus": {"status": "LOGIN_REQUIRED",
                                   "reason": "Sign in to confirm you're not a bot"}}
        self.assertTrue(ts.yt_blocked(d))

    def test_login_required_without_reason_is_the_bot_wall(self):
        self.assertTrue(ts.yt_blocked({"playabilityStatus": {"status": "LOGIN_REQUIRED"}}))

    def test_age_gate_is_not_a_bot_wall(self):
        d = {"playabilityStatus": {"status": "LOGIN_REQUIRED",
                                   "reason": "Sign in to confirm your age"}}
        self.assertFalse(ts.yt_blocked(d))

    def test_unavailable_video_is_not_a_bot_wall(self):
        self.assertFalse(ts.yt_blocked({"playabilityStatus": {"status": "ERROR", "reason": "Video unavailable"}}))

    def test_html_body(self):
        self.assertTrue(ts.yt_blocked(body="Sign in to confirm you’re not a bot".encode()))
        self.assertFalse(ts.yt_blocked(body=b"<html>ok</html>"))


class LiveWindow(unittest.TestCase):
    def playlist(self, last):
        lines = ["#EXTM3U", "#EXT-X-MEDIA-SEQUENCE:%d" % (last - 2)]
        for sq in range(last - 2, last + 1):
            lines += ["#EXTINF:1.0,", "https://rr1.googlevideo.com/videoplayback/sq/%d/goap/x" % sq]
        return "\n".join(lines) + "\n"

    def decode(self, line):
        b64 = line[len(ts.LIVE_BASE):-len(".ts")]
        return base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)).decode()

    def test_window_is_rebuilt_ending_at_the_newest_segment(self):
        out = ts.live_window(self.playlist(500))
        segs = [l for l in out.splitlines() if l.startswith(ts.LIVE_BASE)]
        self.assertEqual(len(segs), ts.LIVE_WINDOW)
        first = 500 - ts.LIVE_WINDOW + 1
        self.assertIn("#EXT-X-MEDIA-SEQUENCE:%d" % first, out)
        self.assertEqual([ts._sq(self.decode(s)) for s in segs], list(range(first, 501)))

    def test_window_never_goes_below_zero(self):
        out = ts.live_window(self.playlist(5))
        self.assertIn("#EXT-X-MEDIA-SEQUENCE:0", out)
        self.assertEqual(sum(1 for l in out.splitlines() if l.startswith(ts.LIVE_BASE)), 6)

    def test_playlist_without_sq_is_only_proxied(self):
        text = "#EXTM3U\n#EXTINF:1.0,\nhttps://example.com/seg1.ts\n"
        out = ts.live_window(text)
        seg = [l for l in out.splitlines() if l.startswith(ts.LIVE_BASE)]
        self.assertEqual([self.decode(s) for s in seg], ["https://example.com/seg1.ts"])


class TransportStream(unittest.TestCase):
    @staticmethod
    def packet(pts):
        b = bytes([0x21 | ((pts >> 29) & 0x0E), (pts >> 22) & 0xFF, 0x01 | ((pts >> 14) & 0xFE),
                   (pts >> 7) & 0xFF, 0x01 | ((pts << 1) & 0xFE)])
        pes = b"\x00\x00\x01\xe0\x00\x00\x80\x80\x05" + b
        pk = bytes([0x47, 0x40, 0x100 >> 8 & 0x1F, 0x10]) + pes
        return pk + b"\xff" * (188 - len(pk))

    def test_first_video_pts(self):
        for pts in (0, 90000, 2**32 + 12345):
            self.assertEqual(ts.ts_first_pts(self.packet(pts)), pts)

    def test_no_video_packet(self):
        self.assertIsNone(ts.ts_first_pts(b"\x47" + b"\x00" * 187))


class Subtitles(unittest.TestCase):
    def test_vtt_cues(self):
        vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHello\nworld\n\n00:00:03.000 --> 00:00:04.000\nBye\n"
        self.assertEqual(ts._vtt_cues(vtt), [("00:00:01.000 --> 00:00:02.000", ["Hello", "world"]),
                                             ("00:00:03.000 --> 00:00:04.000", ["Bye"])])

    def test_subtitle_cache_path_cannot_escape(self):
        for vid, tl in (("../../etc/x", "en"), ("jNQXAC9IVRw", "../../x"), ("jNQXAC9IVRw", "en/../..")):
            with self.assertRaises(ValueError):
                ts.sub_vtt(vid, tl)

    def test_cc_time(self):
        self.assertAlmostEqual(ts._cc_time("01:02:03.500"), 3723.5)


class Library(unittest.TestCase):
    def setUp(self):
        if os.path.exists(ts.YTLIB):
            os.remove(ts.YTLIB)

    def test_resume_position_and_history(self):
        meta = {"id": "jNQXAC9IVRw", "title": "Me at the zoo", "author": "jawed", "len": 19}
        code, _ = ts.ytlib_post("/api/p/yt/pos", {"id": "jNQXAC9IVRw", "t": 7, "len": 19, "meta": meta})
        self.assertEqual(code, 200)
        self.assertEqual(ts.ytlib_query("/api/p/yt/pos", {"ids": "jNQXAC9IVRw"}), {"jNQXAC9IVRw": [7, 19]})
        hist = ts.ytlib_query("/api/p/yt/hist", {})
        self.assertEqual(hist["total"], 1)
        self.assertEqual(hist["items"][0]["title"], "Me at the zoo")

    def test_bad_ids_are_rejected(self):
        self.assertEqual(ts.ytlib_post("/api/p/yt/pos", {"id": "../../etc", "t": 1})[0], 400)
        self.assertEqual(ts.ytlib_post("/api/p/yt/sub", {"id": "not-a-channel", "on": True})[0], 400)
        self.assertEqual(ts.ytlib_query("/api/p/yt/pos", {"ids": "bad,also bad"}), {})

    def test_history_search_and_delete(self):
        for vid, title in (("aaaaaaaaaaa", "Cooking pasta"), ("bbbbbbbbbbb", "Live news")):
            ts.ytlib_post("/api/p/yt/pos", {"id": vid, "t": 1, "len": 60, "meta": {"id": vid, "title": title}})
        self.assertEqual(ts.ytlib_query("/api/p/yt/hist", {"q": "pasta"})["total"], 1)
        ts.ytlib_post("/api/p/yt/hist/del", {"id": "aaaaaaaaaaa"})
        self.assertEqual([x["id"] for x in ts.ytlib_query("/api/p/yt/hist", {})["items"]], ["bbbbbbbbbbb"])


class Health(unittest.TestCase):
    def test_health(self):
        h = ts.health()
        self.assertTrue(h["ok"])
        self.assertFalse(h["warp_proxy"])


if __name__ == "__main__":
    unittest.main()
