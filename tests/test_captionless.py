"""Offline tests for captionless. Nothing here touches a real video site or a real speech-to-text service.

Run them with:  python -m unittest discover -s tests -v

The end-to-end tests start the script as a separate program and point it at a small fake
speech-to-text server that runs inside the test. If the packages the script needs are not installed,
the script sets itself up first (in a temporary folder), exactly as it does for a new user.
"""

import importlib.util
import json
import math
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "skills" / "transcribe" / "scripts" / "captionless.py"

spec = importlib.util.spec_from_file_location("captionless", SCRIPT)
cl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cl)

HAVE_AUDIO = importlib.util.find_spec("av") is not None and importlib.util.find_spec("numpy") is not None
TMP = None      # one temporary folder for the whole run
SERVER = None   # the fake speech-to-text server


def setUpModule():
    global TMP, SERVER
    TMP = Path(tempfile.mkdtemp(prefix="captionless-test-"))
    SERVER = FakeSpeechServer()


def tearDownModule():
    SERVER.close()
    shutil.rmtree(TMP, ignore_errors=True)


# --------------------------------------------------------------------------- helpers

class FakeSpeechServer:
    """Answers POST /v1/audio/transcriptions the way an OpenAI-compatible server does."""

    def __init__(self):
        owner = self
        self.calls, self.fail, self.lock = [], set(), threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("content-length") or 0))
                m = re.search(rb'filename="piece(\d+)\.(\w+)"', body)
                piece = int(m.group(1)) if m else -1
                with owner.lock:
                    owner.calls.append(dict(path=self.path, piece=piece, auth=self.headers.get("authorization"),
                                            verbose=b"verbose_json" in body, ext=m.group(2).decode() if m else None))
                if piece in owner.fail:
                    status, extra = 429, [("retry-after", "7200")]
                    payload = {"error": {"message": "Rate limit reached. Please try again in 2h0m0s."}}
                else:
                    status, extra = 200, []
                    text = f"Piece {piece} says hello."
                    payload = {"language": "english", "text": text, "segments": [
                        {"start": 1.0, "end": 3.0, "text": text, "avg_logprob": -0.2, "no_speech_prob": 0.01,
                         "compression_ratio": 1.2}]}
                data = json.dumps(payload).encode()
                self.send_response(status)
                for k, v in [("content-type", "application/json"), ("content-length", str(len(data)))] + extra:
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def make_wav(path, seconds):
    """A tone with a short silence every ten seconds, so there are quiet moments to cut at."""
    rate = 16000
    tone = struct.pack(f"<{rate}h", *(int(9000 * math.sin(2 * math.pi * 220 * i / rate)) for i in range(rate)))
    quiet = b"\x00\x00" * rate
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        for s in range(seconds):
            w.writeframes(quiet if s % 10 == 9 else tone)
    return path


def run_script(*args, home, out, **env):
    """Run captionless as a user would. Returns (exit code, stdout, stderr)."""
    full = dict(os.environ, CAPTIONLESS_HOME=str(home), CAPTIONLESS_OUT=str(out), PYTHONIOENCODING="utf-8",
                CAPTIONLESS_BASE_URL=SERVER.url, CAPTIONLESS_API_KEY="test-key", CAPTIONLESS_PROVIDER="custom", **env)
    r = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], env=full, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=900)
    return r.returncode, r.stdout, r.stderr


def segments(*rows):
    return [{"start": float(a), "end": float(b), "text": t} for a, b, t in rows]


def save_video(out, title="A talk", vid="abcdefghijk", chapters=None, rows=None, channel="Some Channel"):
    rows = rows or [(0, 20, "Welcome to the talk."), (20, 50, "We begin with the Jon Doe story."),
                    (50, 100, "Then the pricing model is explained."), (100, 130, "Thanks for watching.")]
    meta = dict(kind="url", id=vid, title=title, source=f"https://www.youtube.com/watch?v={vid}", channel=channel,
                duration=rows[-1][1], language="en", engine="Test engine", chapters=chapters)
    return cl.write_outputs(segments(*rows), meta, out, {})


# --------------------------------------------------------------------------- pure functions

class Helpers(unittest.TestCase):
    def test_timestamps_and_names(self):
        self.assertEqual(cl.fmt_ts(75), "01:15")
        self.assertEqual(cl.fmt_ts(3675), "1:01:15")
        self.assertEqual(cl.fmt_ts(3675.5, srt=True), "01:01:15,500")
        self.assertEqual(cl.safe_name('What? A "test": part 1/2.'), "What A test part 1 - 2")
        self.assertEqual(cl.youtube_id("https://www.youtube.com/watch?v=J-yDVWHyqYQ&list=PL1&index=2"), "J-yDVWHyqYQ")
        self.assertEqual(cl.youtube_id("https://youtu.be/J-yDVWHyqYQ?t=5"), "J-yDVWHyqYQ")
        self.assertTrue(cl.is_playlist_url("https://www.youtube.com/playlist?list=PL1"))
        self.assertFalse(cl.is_playlist_url("https://www.youtube.com/watch?v=J-yDVWHyqYQ&list=PL1"))

    def test_languages_and_cleanup(self):
        self.assertEqual(cl.spoken_language("en-US"), "en")
        self.assertEqual(cl.spoken_language("iw"), "he")
        self.assertIsNone(cl.spoken_language("xx"))
        self.assertEqual(cl.clean_text("Thank you. Subtitles by the Amara.org community"), "Thank you")
        self.assertTrue(cl.looks_unreliable("m" * 40))
        self.assertFalse(cl.looks_unreliable("A perfectly ordinary sentence about nothing much."))
        self.assertEqual(cl.normalize("Café  مدرسة"), cl.normalize("cafe مدرسه"))

    def test_caption_parsers(self):
        json3 = json.dumps({"events": [{"tStartMs": 1000, "dDurationMs": 2000, "segs": [{"utf8": "Hello "},
                                                                                       {"utf8": "there"}]},
                                       {"tStartMs": 5000}]})
        self.assertEqual(cl.parse_json3(json3), [{"start": 1.0, "end": 3.0, "text": "Hello there"}])
        vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:03.500\n<c>Hello</c> &amp; welcome\n\n1\n00:01:00.000 --> 00:01:02.000\nBye\n"
        self.assertEqual(cl.parse_vtt(vtt), [{"start": 1.0, "end": 3.5, "text": "Hello & welcome"},
                                             {"start": 60.0, "end": 62.0, "text": "Bye"}])

    def test_rate_limit_wait(self):
        class R:
            def __init__(self, headers, text):
                self.headers, self.text = headers, text
        self.assertEqual(cl.retry_after(R({"retry-after": "12"}, "")), 12.0)
        self.assertEqual(cl.retry_after(R({}, "Please try again in 1h2m3.5s.")), 3723.5)
        self.assertEqual(cl.retry_after(R({}, '{"retryDelay": "41s"}')), 41.0)
        self.assertIsNone(cl.retry_after(R({}, "nope")))

    def test_formats_option(self):
        self.assertEqual(cl.parse_formats("md, VTT"), {"md", "vtt"})
        self.assertEqual(cl.parse_formats("txt,srt"), set())
        self.assertEqual(cl.parse_formats(None), set())
        with self.assertRaises(cl.UserError):
            cl.parse_formats("docx")


# --------------------------------------------------------------------------- the library on disk

class Library(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp(dir=TMP))

    def test_chapters_become_sections(self):
        folder = save_video(self.out, chapters=[(0.0, "Intro"), (45.0, "Pricing"), (500.0, "Never spoken")])
        text = (folder / "transcript.txt").read_text(encoding="utf-8")
        body = text.split(cl.SEPARATOR, 1)[1]
        self.assertEqual(re.findall(r"^## (.+)$", body, re.M), ["Intro", "Pricing"])
        self.assertLess(body.index("Jon Doe"), body.index("## Pricing"))
        self.assertLess(body.index("## Pricing"), body.index("pricing model"))
        self.assertIn("[00:50] Then the pricing model", body)       # a chapter starts a new paragraph
        self.assertEqual(folder.name, "A talk [abcdefghijk]")
        self.assertEqual(folder.parent.name, "Some Channel")

    def test_no_chapters_no_headings(self):
        folder = save_video(self.out)
        self.assertNotIn("## ", (folder / "transcript.txt").read_text(encoding="utf-8"))

    def test_markdown_and_vtt(self):
        folder = save_video(self.out, chapters=[(0.0, "Intro"), (45.0, "Pricing")])
        made = cl.write_extras(folder, {"md", "vtt"})
        self.assertEqual(sorted(p.name for p in made), ["subtitles.vtt", "transcript.md"])
        md = (folder / "transcript.md").read_text(encoding="utf-8")
        self.assertTrue(md.startswith("# A talk\n"))
        self.assertIn("- **Channel:** Some Channel", md)
        self.assertIn("## Pricing", md)
        self.assertIn("**[00:50](https://www.youtube.com/watch?v=abcdefghijk&t=50s)** Then the pricing", md)
        vtt = (folder / "subtitles.vtt").read_text(encoding="utf-8")
        self.assertTrue(vtt.startswith("WEBVTT\n\n1\n00:00:00.000 --> 00:00:20.000\nWelcome to the talk."))
        self.assertEqual(vtt.count("-->"), 4)
        self.assertEqual(cl.parse_vtt(vtt)[1]["start"], 20.0)

    def test_corrections_reach_every_file(self):
        folder = save_video(self.out)
        cl.write_extras(folder, {"md", "vtt"})
        (folder.parent / "corrections.txt").write_text("# names\nJon Doe => John Doe\n", encoding="utf-8")
        self.assertEqual(cl.cmd_fix(["-o", str(self.out)]), 0)
        for name in ("transcript.txt", "subtitles.srt", "transcript.md", "subtitles.vtt"):
            text = (folder / name).read_text(encoding="utf-8")
            self.assertIn("John Doe", text, name)
            self.assertNotIn("Jon Doe", text, name)
        # a new transcript in the same channel picks the rule up by itself
        other = save_video(self.out, title="Second", vid="zzzzzzzzzzz")
        self.assertIn("John Doe", (other / "transcript.txt").read_text(encoding="utf-8"))

    def test_export_command(self):
        folder = save_video(self.out)
        self.assertEqual(cl.cmd_export(["--formats", "md", "-o", str(self.out)]), 0)
        self.assertTrue((folder / "transcript.md").is_file())
        self.assertFalse((folder / "subtitles.vtt").exists())

    def test_search(self):
        save_video(self.out)
        save_video(self.out, title="Other", vid="00000000000", channel="Another",
                   rows=[(0, 10, "Nothing relevant here."), (10, 20, "Just the weather.")])
        found = cl.search_library(self.out, "PRICING model")
        self.assertIn("1 match(es)", found)
        self.assertIn("https://www.youtube.com/watch?v=abcdefghijk&t=50s", found)
        self.assertIn("No matches", cl.search_library(self.out, "pricing", channel="another"))
        self.assertIn("No matches", cl.search_library(self.out, "zebra"))

    def test_find_video_and_parts(self):
        folder = save_video(self.out)
        save_video(self.out, title="A talk, part two", vid="11111111111")
        self.assertEqual(cl.find_video(self.out, "abcdefghijk"), folder)
        self.assertEqual(cl.find_video(self.out, "https://youtu.be/abcdefghijk"), folder)
        self.assertEqual(cl.find_video(self.out, str(folder)), folder)
        self.assertEqual(cl.find_video(self.out, "part two").name, "A talk, part two [11111111111]")
        with self.assertRaises(cl.UserError):
            cl.find_video(self.out, "a talk")          # two videos match
        with self.assertRaises(cl.UserError):
            cl.find_video(self.out, "no such video")
        part = cl.transcript_part(folder)
        self.assertIn("Video id: abcdefghijk", part)
        self.assertIn("[00:00] Welcome to the talk.", part)
        self.assertNotIn("There is more", part)

    def test_long_transcripts_come_in_parts(self):
        rows = [(i * 60, i * 60 + 59, f"Sentence number {i} " + "word " * 200 + "end.") for i in range(60)]
        folder = save_video(self.out, rows=rows)
        first = cl.transcript_part(folder)
        m = re.search(r'offset=(\d+)', first)
        self.assertTrue(m, "a long transcript must say where the next part starts")
        rest, offset, seen = first, int(m.group(1)), 1
        while True:
            rest = cl.transcript_part(folder, offset)
            seen += 1
            m = re.search(r'offset=(\d+)', rest)
            if not m:
                break
            self.assertGreater(int(m.group(1)), offset)
            offset = int(m.group(1))
        self.assertIn("Sentence number 59 ", rest)
        self.assertGreater(seen, 2)


# --------------------------------------------------------------------------- watching channels

class Watch(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp(dir=TMP))
        self.home = Path(tempfile.mkdtemp(dir=TMP))
        self.saved = {k: getattr(cl, k) for k in ("HOME", "watch_listing", "run_items", "prepare", "ensure_deps")}
        cl.HOME = self.home
        cl.ensure_deps = lambda *a, **k: None
        cl.prepare = lambda args: self.out
        self.listed = [dict(kind="url", value=f"https://www.youtube.com/watch?v={i}", id=i, title=f"Video {i}")
                       for i in ("old00000001", "old00000002")]
        cl.watch_listing = lambda url, args: ("My Channel", list(self.listed), True)
        self.ran = []

        def fake_run(items, args, out_dir):
            self.ran.append([i["id"] for i in items])
            failures = []
            for i in items:
                if i["id"].startswith("bad"):
                    failures.append((i, "Video unavailable"))
                elif i["id"].startswith("soon"):
                    failures.append((i, "Premieres in 3 days"))
                else:
                    save_video(out_dir, title=i["title"], vid=i["id"])
            return (1 if failures else 0), failures

        cl.run_items = fake_run

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(cl, k, v)

    def add(self, vid):
        self.listed.insert(0, dict(kind="url", value=f"https://www.youtube.com/watch?v={vid}", id=vid,
                                   title=f"Video {vid}"))

    def test_only_new_videos_are_transcribed(self):
        url = "https://www.youtube.com/@mychannel"
        self.assertEqual(cl.cmd_watch(["add", url]), 0)
        self.assertEqual(cl.cmd_watch(["add", url]), 0)                 # adding twice changes nothing
        self.assertEqual(len(cl.load_watch()), 1)
        self.assertEqual(cl.cmd_watch(["run"]), 0)
        self.assertEqual(self.ran, [])                                  # what was already there is left alone

        self.add("new00000001")
        self.add("new00000002")
        self.assertEqual(cl.cmd_watch(["run"]), 0)
        self.assertEqual(self.ran, [["new00000001", "new00000002"]])    # oldest of the new ones first
        self.assertEqual(cl.cmd_watch(["run"]), 0)
        self.assertEqual(len(self.ran), 1)                              # nothing is done twice

    def test_failures_are_retried_three_times_and_premieres_wait(self):
        cl.cmd_watch(["add", "https://www.youtube.com/@mychannel"])
        self.add("bad00000001")
        self.add("soon0000001")
        for _ in range(5):
            self.assertEqual(cl.cmd_watch(["run"]), 1)
        tried = [sum("bad00000001" in r for r in self.ran), sum("soon0000001" in r for r in self.ran)]
        self.assertEqual(tried, [3, 5])

    def test_list_and_remove(self):
        cl.cmd_watch(["add", "https://www.youtube.com/@mychannel"])
        self.assertEqual(cl.cmd_watch(["remove", "1"]), 0)
        self.assertEqual(cl.load_watch(), [])
        with self.assertRaises(cl.UserError):
            cl.cmd_watch(["remove", "7"])

    def test_channel_links(self):
        for good in ("https://www.youtube.com/@name", "https://youtube.com/channel/UC123/", "https://www.youtube.com/c/x"):
            self.assertTrue(cl.YT_CHANNEL.match(good), good)
        for other in ("https://www.youtube.com/@name/streams", "https://www.youtube.com/playlist?list=PL1"):
            self.assertFalse(cl.YT_CHANNEL.match(other), other)


# --------------------------------------------------------------------------- audio

@unittest.skipUnless(HAVE_AUDIO, "needs the av and numpy packages")
class Audio(unittest.TestCase):
    def test_decode_cut_encode(self):
        import numpy as np

        wav = make_wav(TMP / "audio-test.wav", 200)
        audio = cl.decode_audio(str(wav))
        self.assertEqual(audio.dtype, np.int16)
        self.assertAlmostEqual(len(audio) / cl.SR, 200, delta=0.5)
        cuts = cl.find_cuts(audio, 60)
        self.assertEqual((cuts[0], cuts[-1]), (0, len(audio)))
        self.assertEqual(len(cuts), 4)
        for c in cuts[1:-1]:                    # every cut lands in one of the silent seconds
            self.assertEqual(int(c / cl.SR) % 10, 9, f"cut at {c / cl.SR:.2f}s is not in a quiet second")
        for codec, name in (("opus", "p.ogg"), ("mp3", "p.mp3")):
            piece = TMP / name
            cl.encode_audio(audio[cuts[0]:cuts[1]], piece, codec)
            back = cl.decode_audio(str(piece))
            self.assertAlmostEqual(len(back), cuts[1], delta=cl.SR)
            self.assertLess(piece.stat().st_size, 1_000_000)


# --------------------------------------------------------------------------- the whole program

class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = TMP / "home"
        cls.wav = make_wav(TMP / "My recording.wav", 200)

    def setUp(self):
        self.out = Path(tempfile.mkdtemp(dir=TMP))
        SERVER.calls.clear()
        SERVER.fail.clear()

    def test_version_help_doctor(self):
        code, out, _ = run_script("--version", home=self.home, out=self.out)
        self.assertEqual((code, out.strip()), (0, f"captionless {cl.__version__}"))
        code, out, _ = run_script("--help", home=self.home, out=self.out)
        self.assertEqual(code, 0)
        self.assertIn("--formats", out)
        code, out, _ = run_script("doctor", home=self.home, out=self.out)
        self.assertEqual(code, 0)
        self.assertIn(f"server {SERVER.url}", out)
        self.assertIn("Default      custom", out)

    def test_file_is_cut_sent_and_stitched(self):
        code, out, err = run_script(self.wav, "--chunk-minutes", "1", "--formats", "md,vtt",
                                    home=self.home, out=self.out)
        self.assertEqual(code, 0, err)
        folder = self.out / "Local files" / "My recording"
        self.assertIn(f"FOLDER:     {folder}", out)
        self.assertIn("ENGINE:     Custom server whisper-1", out)
        self.assertEqual(out.count("ALSO:"), 2)
        self.assertEqual(sorted(c["piece"] for c in SERVER.calls), [0, 1, 2])
        for c in SERVER.calls:
            self.assertEqual((c["path"], c["auth"], c["verbose"], c["ext"]),
                             ("/v1/audio/transcriptions", "Bearer test-key", True, "ogg"))
        starts = [int(h) * 3600 + int(m) * 60 + int(s) for h, m, s in
                  re.findall(r"^(\d\d):(\d\d):(\d\d),\d+ -->", (folder / "subtitles.srt").read_text("utf-8"), re.M)]
        self.assertEqual(len(starts), 3)
        self.assertEqual(starts[0], 1)                      # each piece's times are moved to where the piece began
        self.assertTrue(45 <= starts[1] <= 61 and 90 <= starts[2] <= 121, starts)   # cuts sit in quiet seconds
        text = (folder / "transcript.txt").read_text("utf-8")
        self.assertRegex(text, r"(?s)Piece 0 says hello\..*Piece 1 says hello\..*Piece 2 says hello\.")
        self.assertIn("Language: english", text)
        self.assertTrue((folder / "transcript.md").is_file() and (folder / "subtitles.vtt").is_file())

    def test_rate_limit_stops_and_resumes(self):
        SERVER.fail.add(2)
        code, out, err = run_script(self.wav, "--chunk-minutes", "1", home=self.home, out=self.out)
        self.assertEqual(code, 2, err)
        self.assertIn("STOPPED:", err)
        self.assertIn("2 h 0 min", err)
        self.assertNotIn("FOLDER:", out)
        SERVER.fail.clear()
        SERVER.calls.clear()
        code, out, err = run_script(self.wav, "--chunk-minutes", "1", home=self.home, out=self.out)
        self.assertEqual(code, 0, err)
        self.assertEqual([c["piece"] for c in SERVER.calls], [2], "only the missing piece is sent again")
        self.assertIn("2 already done in an earlier run", err)
        self.assertIn("FOLDER:", out)

    def test_missing_key_is_explained(self):
        code, out, err = run_script(self.wav, "--provider", "openai", home=self.home, out=self.out,
                                    OPENAI_API_KEY="")
        if "rejected the API key" in err or code == 0:
            self.skipTest("this machine has a real OpenAI key")
        self.assertEqual(code, 1)
        self.assertIn("OpenAI needs an API key", err)


class McpServer(unittest.TestCase):
    """Talks to `captionless mcp` the way an app does: one JSON message per line."""

    @classmethod
    def setUpClass(cls):
        cls.home = TMP / "home"
        cls.out = Path(tempfile.mkdtemp(dir=TMP))
        cls.wav = make_wav(TMP / "Voice memo.wav", 30)
        save_video(cls.out)
        env = dict(os.environ, CAPTIONLESS_HOME=str(cls.home), CAPTIONLESS_OUT=str(cls.out),
                   CAPTIONLESS_BASE_URL=SERVER.url, CAPTIONLESS_PROVIDER="custom", PYTHONIOENCODING="utf-8")
        cls.proc = subprocess.Popen([sys.executable, str(SCRIPT), "mcp"], env=env, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        cls.lines = queue.Queue()
        threading.Thread(target=lambda: [cls.lines.put(x) for x in iter(cls.proc.stdout.readline, b"")],
                         daemon=True).start()
        cls.errors = []
        threading.Thread(target=lambda: [cls.errors.append(x) for x in iter(cls.proc.stderr.readline, b"")],
                         daemon=True).start()
        cls.next_id = 0

    @classmethod
    def tearDownClass(cls):
        cls.proc.stdin.close()
        try:
            cls.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        for stream in (cls.proc.stdout, cls.proc.stderr):
            stream.close()

    def send(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            McpServer.next_id += 1
            msg["id"] = McpServer.next_id
        self.proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        self.proc.stdin.flush()
        return msg.get("id")

    def reply(self, mid, timeout=600):
        """The answer to request `mid`; every line the server writes must be one JSON message."""
        while True:
            try:
                raw = self.lines.get(timeout=timeout)
            except queue.Empty:
                self.fail("no answer from the server; its log:\n" + b"".join(self.errors).decode("utf-8", "replace"))
            msg = json.loads(raw.decode("utf-8"))
            self.assertEqual(msg.get("jsonrpc"), "2.0")
            if msg.get("id") == mid:
                return msg

    def call(self, name, **arguments):
        msg = self.reply(self.send("tools/call", {"name": name, "arguments": arguments,
                                                  "_meta": {"progressToken": "t"}}))
        result = msg["result"]
        return result["content"][0]["text"], result["isError"]

    def test_conversation(self):
        hello = self.reply(self.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                    "clientInfo": {"name": "test", "version": "0"}}))["result"]
        self.assertEqual(hello["protocolVersion"], "2025-06-18")
        self.assertEqual(hello["serverInfo"], {"name": "captionless", "version": cl.__version__})
        self.assertIn("tools", hello["capabilities"])
        self.send("notifications/initialized", notify=True)
        self.assertEqual(self.reply(self.send("ping"))["result"], {})
        tools = self.reply(self.send("tools/list"))["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["transcribe", "read_transcript", "search_transcripts",
                                                      "list_transcripts", "save_note"])
        for t in tools:
            self.assertEqual(t["inputSchema"]["type"], "object")
        self.assertEqual(self.reply(self.send("resources/list"))["error"]["code"], -32601)

        text, failed = self.call("list_transcripts")
        self.assertFalse(failed)
        self.assertIn("A talk | Some Channel", text)
        self.assertIn("id: abcdefghijk", text)

        text, failed = self.call("search_transcripts", query="pricing model")
        self.assertIn("&t=50s", text)
        text, failed = self.call("read_transcript", video="abcdefghijk")
        self.assertIn("[00:00] Welcome to the talk.", text)

        text, failed = self.call("save_note", video="abcdefghijk", filename="summary.md", content="# Résumé ملخص\n")
        self.assertFalse(failed, text)
        saved = self.out / "Some Channel" / "A talk [abcdefghijk]" / "summary.md"
        self.assertEqual(saved.read_text(encoding="utf-8"), "# Résumé ملخص\n")
        for bad in ("../escape.md", "transcript.txt", "run.bat", "sub/dir.md"):
            text, failed = self.call("save_note", video="abcdefghijk", filename=bad, content="x")
            self.assertTrue(failed, bad)

        text, failed = self.call("read_transcript", video="nothing like this")
        self.assertTrue(failed)
        text, failed = self.call("no_such_tool")
        self.assertTrue(failed)

        text, failed = self.call("transcribe", source=str(self.wav))
        for _ in range(20):                     # a slow machine may need a second call, as the tool explains
            if not text.startswith("Still working"):
                break
            text, failed = self.call("transcribe", source=str(self.wav))
        self.assertFalse(failed, text)
        self.assertTrue(text.startswith("Transcribed."), text[:200])
        self.assertIn("Piece 0 says hello.", text)
        self.assertTrue((self.out / "Local files" / "Voice memo" / "transcript.txt").is_file())

        text, failed = self.call("transcribe", source=str(TMP / "missing.wav"))
        self.assertTrue(failed)


if __name__ == "__main__":
    unittest.main()
