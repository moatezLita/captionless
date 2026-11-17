#!/usr/bin/env python3
"""captionless: transcribe any online video or media file, even one with no captions.

Usage:
  captionless.py <url-or-file> [...] [options]   transcribe (the default command)
  captionless.py search "words" [options]        search every saved transcript
  captionless.py fix [folder]                    re-apply corrections.txt to saved transcripts
  captionless.py export [folder]                 make Markdown and WebVTT copies of saved transcripts
  captionless.py watch add|list|remove|run       transcribe the new videos of channels and playlists
  captionless.py mcp                             run as an MCP server (Claude Desktop, Cursor, ...)
  captionless.py doctor                          check the setup
  captionless.py update                          upgrade the downloader (fixes most download errors)

Only the Python standard library is needed to start. On the first run the script builds a
private virtual environment in ~/.captionless, installs what it needs there, and re-runs itself.
"""

from __future__ import annotations

import argparse
import base64
import bisect
import hashlib
import html
import importlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from pathlib import Path

__version__ = "0.2.0"

HOME = Path(os.environ.get("CAPTIONLESS_HOME") or (Path.home() / ".captionless"))
VENV = HOME / "venv"
BASE_REQS = ["yt-dlp[default]", "av", "numpy", "requests"]
BASE_MODS = ["yt_dlp", "av", "numpy", "requests"]
LOCAL_REQS = ["faster-whisper"]
LOCAL_MODS = ["faster_whisper"]

SR = 16000  # every provider gets 16 kHz mono speech audio

PROVIDERS = {
    "groq": dict(kind="openai", label="Groq", base="https://api.groq.com/openai/v1", keys=["GROQ_API_KEY"],
                 model="whisper-large-v3", fast="whisper-large-v3-turbo", workers=3),
    "openai": dict(kind="openai", label="OpenAI", base="https://api.openai.com/v1", keys=["OPENAI_API_KEY"],
                   model="whisper-1", fast="whisper-1", workers=3),
    "gemini": dict(kind="gemini", label="Gemini", base="https://generativelanguage.googleapis.com/v1beta",
                   keys=["GEMINI_API_KEY", "GOOGLE_API_KEY"], model="gemini-flash-latest",
                   fast="gemini-flash-lite-latest", workers=2),
    # any server that speaks the OpenAI audio API: set CAPTIONLESS_BASE_URL (+ CAPTIONLESS_API_KEY, CAPTIONLESS_MODEL)
    "custom": dict(kind="openai", label="Custom server", base=None, keys=["CAPTIONLESS_API_KEY"],
                   model="whisper-1", fast="whisper-1", workers=2),
    "local": dict(kind="local", label="Local Whisper", keys=[], model="large-v3-turbo", fast="small"),
}
AUTO_ORDER = ("groq", "openai", "gemini")

NO_KEY_HELP = f"""No speech-to-text API key found. Set the key of whichever service you use:
  GROQ_API_KEY     Groq (has a free tier)            https://console.groq.com/keys
  OPENAI_API_KEY   OpenAI                            https://platform.openai.com/api-keys
  GEMINI_API_KEY   Google Gemini (has a free tier)   https://aistudio.google.com/apikey
For another OpenAI-compatible server, set CAPTIONLESS_BASE_URL (and CAPTIONLESS_API_KEY if it needs one).
Set the variable in your environment, or add it as a line (NAME=your-key) to
{HOME / 'config.env'}
To run offline with no key at all, add: --provider local"""

# Languages Whisper understands; a code outside this list is left for the model to detect.
WHISPER_LANGS = set(
    "en zh de es ru ko fr ja pt tr pl ca nl ar sv it id hi fi vi he uk el ms cs ro da hu ta no th ur hr bg lt la "
    "mi ml cy sk te fa lv bn sr az sl kn et mk br eu is hy ne mn bs kk sq sw gl mr pa si km sn yo so af oc ka be "
    "tg sd gu am yi lo uz fo ht ps tk nn mt sa lb my bo tl mg as tt haw ln ha ba jw su yue".split())
LANG_ALIASES = {"iw": "he", "fil": "tl", "jv": "jw", "in": "id", "nb": "no", "cmn": "zh"}

# Credits Whisper learned from subtitled training videos and "hears" in music or silence.
HALLUCINATIONS = re.compile("|".join([
    r"(ترجمة\s*)?نانسي\s*قنقر",
    r"Subtitles by the Amara\.org community",
    r"Sous-titres réalisés para? la communauté d'Amara\.org",
    r"Sous-titrage (ST'?\s*501|Société Radio-Canada)",
    r"Untertitel der Amara\.org-Community",
    r"Untertitel im Auftrag des ZDF( für funk)?,? ?\d*",
    r"Subtítulos realizados por la comunidad de Amara\.org",
    r"Sottotitoli creati dalla comunità Amara\.org",
    r"Legendas pela comunidade (de )?Amara\.org",
    r"Субтитры сделал DimaTorzok",
    r"Редактор субтитров [А-Я]\.\s?[А-Яа-я]+( Корректор [А-Я]\.\s?[А-Яа-я]+)?",
    r"字幕由\s*Amara\.org\s*社[区群]提供",
]), re.IGNORECASE)

SEPARATOR = "-" * 60


class UserError(Exception):
    """Something the user can fix. Shown as one line, without a traceback."""


class Fatal(UserError):
    """A setup problem that would fail every video, so the whole run stops."""


class QuotaError(Exception):
    """The provider's rate limit will not clear within --max-wait."""


_LOG_SINK = None   # the MCP server points this at the running job so it can report progress


def log(msg=""):
    print(msg, file=sys.stderr, flush=True)
    if _LOG_SINK and str(msg).strip():
        _LOG_SINK(str(msg).strip())


# --------------------------------------------------------------------------- settings

_CONFIG = None


def config():
    """KEY=VALUE lines from ~/.captionless/config.env."""
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = {}
        f = HOME / "config.env"
        if f.is_file():
            for line in f.read_text(encoding="utf-8-sig").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                if k.lower().startswith("export "):
                    k = k[7:].strip()
                _CONFIG[k] = v.strip().strip('"').strip("'")
    return _CONFIG


def setting(name, default=None):
    """Environment variable first, then config.env, then (Windows) a variable saved with `setx`."""
    v = os.environ.get(name) or config().get(name)
    if not v and os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                v = str(winreg.QueryValueEx(k, name)[0])
        except OSError:
            v = None
    return v.strip() if v and v.strip() else default


def provider_key(name):
    for var in PROVIDERS[name]["keys"]:
        v = setting(var)
        if v:
            return v
    return None


def default_out():
    v = setting("CAPTIONLESS_OUT")
    if v:
        return Path(v).expanduser()
    desktop = Path.home() / "Desktop"
    return (desktop if desktop.is_dir() else Path.home()) / "YouTube Transcripts"


def resolve_provider(name, model, fast, translate):
    name = (name or setting("CAPTIONLESS_PROVIDER") or "auto").lower()
    if name == "auto":
        name = next((p for p in AUTO_ORDER if provider_key(p)), None)
        if not name:
            if not setting("CAPTIONLESS_BASE_URL"):
                raise Fatal(NO_KEY_HELP)
            name = "custom"
    if name not in PROVIDERS:
        raise Fatal(f"Unknown provider '{name}'. Choose one of: {', '.join(PROVIDERS)}.")
    p = dict(PROVIDERS[name], name=name)
    if p["kind"] != "local":
        p["key"] = provider_key(name)
        if name == "custom":
            p["base"] = setting("CAPTIONLESS_BASE_URL")
            if not p["base"]:
                raise Fatal("The custom provider needs CAPTIONLESS_BASE_URL, e.g. http://localhost:8000/v1")
        elif not p["key"]:
            raise Fatal(f"{p['label']} needs an API key. Set {p['keys'][0]} in your environment or in "
                        f"{HOME / 'config.env'}.")
    # Groq's turbo model cannot translate, so --fast is ignored for translation there.
    use_fast = fast and not (translate and name == "groq")
    p["model"] = model or setting("CAPTIONLESS_MODEL") or (p["fast"] if use_fast else p["model"])
    return p


# --------------------------------------------------------------------------- first-run setup

def venv_python():
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def in_private_venv():
    try:
        return Path(sys.prefix).resolve() == VENV.resolve()
    except OSError:
        return False


def have(mods):
    return all(importlib.util.find_spec(m) is not None for m in mods)


def ensure_deps(local=False):
    """Make the needed packages importable, building ~/.captionless/venv on the first run."""
    mods = BASE_MODS + (LOCAL_MODS if local else [])
    if have(mods):
        return
    reqs = BASE_REQS + (LOCAL_REQS if local else [])
    manual = "pip install " + " ".join(f'"{r}"' for r in reqs)
    if not in_private_venv() and os.environ.get("CAPTIONLESS_NO_BOOTSTRAP"):
        raise Fatal(f"Python packages are missing. Install them with: {manual}")
    vpy = venv_python()
    if not vpy.exists():
        log(f"First run: setting up captionless in {HOME} (about a minute)...")
        HOME.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.check_call([sys.executable, "-m", "venv", str(VENV)], stdout=sys.stderr)
        except (subprocess.CalledProcessError, OSError):
            raise Fatal("Could not create a Python virtual environment. On Debian/Ubuntu run "
                        f"`sudo apt install python3-venv` and try again, or install the packages yourself: {manual}")
    marker = VENV / "captionless-requirements.txt"
    installed = set(marker.read_text(encoding="utf-8").splitlines()) if marker.exists() else set()
    if in_private_venv() or not set(reqs) <= installed:
        log("Installing: " + ", ".join(reqs))
        r = subprocess.run([str(vpy), "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                            "--upgrade", *reqs], stdout=sys.stderr)
        if r.returncode:
            raise Fatal("Installing the Python packages failed; see the pip messages above.")
        marker.write_text("\n".join(sorted(installed | set(reqs))), encoding="utf-8")
    if in_private_venv():
        importlib.invalidate_caches()
        if not have(mods):
            raise Fatal(f"The packages still cannot be imported. Try deleting {VENV} and running again.")
        return
    env = dict(os.environ, CAPTIONLESS_NO_BOOTSTRAP="1")
    sys.exit(subprocess.call([str(vpy), os.path.abspath(__file__), *sys.argv[1:]], env=env))


# --------------------------------------------------------------------------- small helpers

def fmt_ts(sec, srt=False):
    sec = max(0.0, float(sec))
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    if srt:
        ms = min(999, int(round((sec - int(sec)) * 1000)))
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def human(seconds):
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600} h {seconds % 3600 // 60} min"
    return f"{seconds // 60} min" if seconds >= 60 else f"{seconds} s"


def safe_name(text, limit=80):
    text = re.sub(r"\s*[/\\|]\s*", " - ", text or "")
    text = re.sub(r'[<>:"?*\x00-\x1f]', "", text)
    text = re.sub(r"\s+", " ", text).strip().rstrip(".")
    return text[:limit].rstrip(" -.") or "untitled"


def spoken_language(code):
    """A site's language tag (en-US, iw, zh-Hans) as a Whisper language code, or None."""
    if not code:
        return None
    c = str(code).lower().replace("_", "-").split("-")[0]
    c = LANG_ALIASES.get(c, c)
    return c if c in WHISPER_LANGS else None


def clean_text(text):
    text = (text or "").strip()
    if HALLUCINATIONS.search(text):
        text = re.sub(r"\s{2,}", " ", HALLUCINATIONS.sub("", text)).strip(" .،")
    return text


def looks_unreliable(text):
    """Repeated letters ('و و و و', 'mmmmmm') are how a failed Whisper run usually looks."""
    tokens = text.split()
    if len(tokens) > 50 and sum(len(t) == 1 for t in tokens) / len(tokens) > 0.3:
        return True
    return bool(re.search(r"(\S)\1{15,}", text)) or bool(re.search(r"(\b\S{1,3}\b )\1{12,}", text))


YT_ID = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/|/live/|/embed/)([\w-]{11})(?![\w-])")


def youtube_id(url):
    m = YT_ID.search(url)
    return m.group(1) if m else None


def is_playlist_url(url):
    return bool(re.search(r"[?&]list=", url)) and not youtube_id(url)


def time_link(source, seconds):
    vid = youtube_id(source or "")
    if vid:
        return f"https://www.youtube.com/watch?v={vid}&t={int(seconds)}s"
    return source or ""


# --------------------------------------------------------------------------- library on disk

def scan_done(out_dir):
    """{video id: folder} for every video that already has a transcript."""
    done = {}
    if out_dir.is_dir():
        for channel in out_dir.iterdir():
            if not channel.is_dir():
                continue
            for video in channel.iterdir():
                m = re.search(r"\[([^\[\]]+)\]$", video.name)
                if m and (video / "transcript.txt").is_file():
                    done[m.group(1)] = video
    return done


def load_corrections(*files):
    """Rules from corrections.txt files: one `wrong => right` per line, whole words only."""
    rules = []
    for f in files:
        if not f.is_file():
            continue
        for line in f.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=>" not in line:
                continue
            wrong, right = (x.strip() for x in line.split("=>", 1))
            if wrong:
                rules.append((re.compile(r"(?<!\w)" + re.escape(wrong) + r"(?!\w)"), right))
    return rules


def apply_corrections(text, rules):
    for rx, right in rules:
        text = rx.sub(lambda _m, r=right: r, text)
    return text


def paragraphs(segments, target=45.0, hard_max=90.0):
    paras, cur, start = [], [], None
    for s in segments:
        if start is None:
            start = s["start"]
        cur.append(s["text"])
        span = s["end"] - start
        if (span >= target and s["text"].endswith((".", "?", "!", "…", "。", "؟", "۔", "।"))) or span >= hard_max:
            paras.append((start, " ".join(cur)))
            cur, start = [], None
    if cur:
        paras.append((start, " ".join(cur)))
    return paras


def chapter_blocks(segments, chapters):
    """[(chapter title or None, paragraphs)]: the video's own chapters become sections of the transcript."""
    if not chapters:
        return [(None, paragraphs(segments))]
    starts, groups = [c[0] for c in chapters], {}
    for s in segments:   # a segment belongs to the chapter that is running at its midpoint
        groups.setdefault(bisect.bisect_right(starts, (s["start"] + s["end"]) / 2) - 1, []).append(s)
    return [(chapters[i][1] if i >= 0 else None, paragraphs(groups[i])) for i in sorted(groups)]


EXTRA_FILES = {"vtt": "subtitles.vtt", "md": "transcript.md"}


def parse_formats(value):
    """'md,vtt' -> {'md', 'vtt'}: the optional extra files (transcript.txt and subtitles.srt are always written)."""
    wanted = {x.strip().lower().lstrip(".") for x in re.split(r"[,\s]+", value or "") if x.strip()}
    wanted -= {"txt", "srt"}
    unknown = wanted - set(EXTRA_FILES)
    if unknown:
        raise UserError(f"Unknown format '{sorted(unknown)[0]}'. The extra formats are: {', '.join(EXTRA_FILES)}.")
    return wanted


def write_extras(folder, formats=()):
    """Make the asked-for extra files from the two saved ones, and refresh any that already exist.

    subtitles.vtt is the WebVTT twin of subtitles.srt. transcript.md is transcript.txt as Markdown,
    with chapter headings and (for YouTube) timestamps that open the video at that moment.
    """
    formats = set(formats) | {k for k, name in EXTRA_FILES.items() if (folder / name).is_file()}
    srt, txt, made = folder / "subtitles.srt", folder / "transcript.txt", []
    if "vtt" in formats and srt.is_file():
        lines = [ln.replace(",", ".") if "-->" in ln else ln for ln in srt.read_text(encoding="utf-8").split("\n")]
        (folder / EXTRA_FILES["vtt"]).write_text("WEBVTT\n\n" + "\n".join(lines), encoding="utf-8")
        made.append(folder / EXTRA_FILES["vtt"])
    if "md" in formats and txt.is_file():
        meta = read_header(txt)
        head, sep, body = txt.read_text(encoding="utf-8").partition(SEPARATOR)
        source = meta.get("source", "")
        out = [f"# {meta.get('title') or folder.name}", ""]
        out += [f"- **{k.capitalize()}:** {meta[k]}"
                for k in ("source", "channel", "duration", "language", "engine", "created") if meta.get(k)]
        out.append("")
        for line in (body if sep else head).strip("\n").splitlines():
            m = re.match(r"\[(?:(\d+):)?(\d+):(\d+)\]\s*(.*)", line)
            if m:
                secs = int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
                stamp = f"[{fmt_ts(secs)}]"
                if youtube_id(source):
                    stamp += f"({time_link(source, secs)})"
                line = f"**{stamp}** {m.group(4)}"
            out.append(line)
        (folder / EXTRA_FILES["md"]).write_text("\n".join(out) + "\n", encoding="utf-8")
        made.append(folder / EXTRA_FILES["md"])
    return made


def write_outputs(segments, meta, out_dir, done, timestamps=True):
    """Saves <out>/<channel>/<title> [id]/transcript.txt and subtitles.srt."""
    folder = done.get(meta["id"]) if meta.get("id") else None
    if folder is None:
        channel = safe_name(meta.get("channel") or ("Local files" if meta["kind"] == "file" else "Other"), 60)
        name = safe_name(meta["title"]) + (f" [{meta['id']}]" if meta.get("id") else "")
        folder = out_dir / channel / name
    folder.mkdir(parents=True, exist_ok=True)

    rules = load_corrections(out_dir / "corrections.txt", folder.parent / "corrections.txt")
    if rules:
        segments = [dict(s, text=apply_corrections(s["text"], rules)) for s in segments]

    header = [f"Title:    {meta['title']}", f"Source:   {meta['source']}"]
    if meta.get("channel"):
        header.append(f"Channel:  {meta['channel']}")
    header += [f"Duration: {fmt_ts(meta.get('duration') or 0)}",
               f"Language: {meta.get('language') or 'unknown'}",
               f"Engine:   {meta['engine']}",
               f"Created:  {time.strftime('%Y-%m-%d')}",
               SEPARATOR, ""]
    body = []
    for chapter, paras in chapter_blocks(segments, meta.get("chapters")):
        if chapter:
            body += [f"## {chapter}", ""]
        for start, text in paras:
            body += [f"[{fmt_ts(start)}] {text}" if timestamps else text, ""]
    txt = folder / "transcript.txt"
    txt.write_text("\n".join(header + body), encoding="utf-8")

    srt = []
    for i, s in enumerate(segments, 1):
        srt += [str(i), f"{fmt_ts(s['start'], True)} --> {fmt_ts(max(s['end'], s['start'] + 0.3), True)}",
                s["text"], ""]
    (folder / "subtitles.srt").write_text("\n".join(srt), encoding="utf-8")
    return folder


# --------------------------------------------------------------------------- download

def find_js_runtimes():
    """YouTube downloads need a JavaScript runtime: Deno (preferred) or Node 20+."""
    found = {}
    exe = "deno.exe" if os.name == "nt" else "deno"
    deno = shutil.which("deno")
    if not deno:
        candidates = [Path.home() / ".deno" / "bin" / exe]
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates += Path(local, "Microsoft", "WinGet", "Packages").glob("DenoLand.Deno*/deno.exe")
        deno = next((str(c) for c in candidates if c.is_file()), None)
    if deno:
        found["deno"] = {"path": deno}
    node = shutil.which("node")
    if node:
        found["node"] = {"path": node}
    return found


JS_HINT = ("No JavaScript runtime was found, and YouTube downloads need one. Install Deno, then try again:\n"
           "  Windows: winget install DenoLand.Deno      macOS: brew install deno\n"
           "  Linux:   curl -fsSL https://deno.land/install.sh | sh")


def ydl_options(args, **extra):
    opts = {"quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True,
            "js_runtimes": find_js_runtimes() or {"deno": {}}}
    if getattr(args, "cookies_from_browser", None):
        opts["cookiesfrombrowser"] = (args.cookies_from_browser,)
    opts.update(extra)
    return opts


def download_error(e):
    msg = str(e).strip().splitlines()[0] if str(e).strip() else e.__class__.__name__
    msg = re.sub(r"^ERROR:\s*", "", msg)
    if not find_js_runtimes():
        msg += "\n" + JS_HINT
    elif "Sign in to confirm" in msg:
        msg += "\nTry again with --cookies-from-browser chrome (or edge, firefox, safari)."
    return UserError(msg)


def expand_playlist(url, args, **extra):
    """Every video of a playlist or channel as (title, [items])."""
    import yt_dlp

    first, last = getattr(args, "start", 1), getattr(args, "end", 0)
    try:
        with yt_dlp.YoutubeDL(ydl_options(args, noplaylist=False, extract_flat="in_playlist", **extra)) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise download_error(e)
    if info.get("_type") != "playlist":
        return None, [dict(kind="url", value=url, id=info.get("id"), title=info.get("title"))]
    entries = []
    for e in info.get("entries") or []:
        if not e:
            continue
        if e.get("_type") == "playlist" and e.get("entries"):   # a channel is a list of tabs
            entries += [x for x in e["entries"] if x]
        else:
            entries.append(e)
    items = []
    for n, e in enumerate(entries, 1):
        if n < first or (last and n > last):
            continue
        link = e.get("url") or e.get("webpage_url")
        if not link and e.get("id"):
            link = f"https://www.youtube.com/watch?v={e['id']}"
        if link:
            items.append(dict(kind="url", value=link, id=e.get("id"), title=e.get("title"), index=n))
    return info.get("title") or "playlist", items


# --------------------------------------------------------------------------- existing captions

def parse_json3(raw):
    segs = []
    for ev in json.loads(raw).get("events", []):
        if not ev.get("segs"):
            continue
        text = re.sub(r"\s+", " ", "".join(s.get("utf8", "") for s in ev["segs"])).strip()
        if text:
            start = ev.get("tStartMs", 0) / 1000
            segs.append({"start": start, "end": start + ev.get("dDurationMs", 0) / 1000, "text": text})
    return segs


def parse_vtt(raw):
    def secs(t):
        parts = t.replace(",", ".").split(":")
        return sum(float(p) * 60 ** i for i, p in enumerate(reversed(parts)))

    segs = []
    for block in re.split(r"\r?\n\s*\r?\n", raw):
        lines = [x for x in block.strip().splitlines() if x.strip()]
        idx = next((i for i, x in enumerate(lines) if "-->" in x), None)
        if idx is None:
            continue
        m = re.match(r"\s*([\d:.,]+)\s*-->\s*([\d:.,]+)", lines[idx])
        if not m:
            continue
        text = html.unescape(re.sub(r"<[^>]+>", "", " ".join(lines[idx + 1:])))
        text = re.sub(r"\s+", " ", text).strip()
        if text and not (segs and segs[-1]["text"] == text):
            segs.append({"start": secs(m.group(1)), "end": secs(m.group(2)), "text": text})
    return segs


def fetch_captions(ydl, info, language, mode):
    """Captions that already exist for the spoken language, as (segments, language, label) or None.

    mode 'manual' accepts only captions the uploader provided; 'any' also accepts the site's
    automatic captions, which cost nothing but are usually less accurate than Whisper.
    """
    manual = {k: v for k, v in (info.get("subtitles") or {}).items() if k != "live_chat"}
    auto = info.get("automatic_captions") or {}
    if not language:   # the "-orig" automatic track names the language that is actually spoken
        language = next((spoken_language(k[:-5]) for k in auto if k.endswith("-orig")), None)

    def matching(tracks):
        if language:
            keys = [k for k in tracks if spoken_language(k) == language]
            return sorted(keys, key=lambda k: (not k.endswith("-orig"), len(k)))
        return list(tracks) if len(tracks) == 1 else []

    candidates = [(k, manual[k], "captions provided by the uploader") for k in matching(manual)]
    if mode == "any":
        candidates += [(k, auto[k], "automatic captions (less accurate than AI transcription)")
                       for k in matching(auto)]
    duration = info.get("duration") or 0
    for key, formats, label in candidates:
        for ext, parse in (("json3", parse_json3), ("vtt", parse_vtt)):
            f = next((x for x in formats if x.get("ext") == ext and x.get("url")), None)
            if not f:
                continue
            try:
                segs = parse(ydl.urlopen(f["url"]).read().decode("utf-8", "replace"))
            except Exception:  # noqa: BLE001 - any failure just means "transcribe it ourselves"
                continue
            for a, b in zip(segs, segs[1:]):   # rolling captions overlap; end each one where the next starts
                a["end"] = max(min(a["end"], b["start"]), a["start"])
            covers = not duration or duration < 60 or (segs and segs[-1]["start"] >= duration * 0.6)
            if len(segs) >= 2 and covers:
                return segs, spoken_language(key) or key, label
    return None


# --------------------------------------------------------------------------- audio

def decode_audio(src):
    """Any audio or video file as a 16 kHz mono int16 numpy array (PyAV bundles FFmpeg)."""
    import av
    import numpy as np

    resampler = av.AudioResampler(format="s16", layout="mono", rate=SR)
    parts = []
    with av.open(src) as container:
        if not container.streams.audio:
            raise UserError("This file has no audio track.")
        stream = container.streams.audio[0]
        stream.thread_type = "AUTO"
        try:
            for frame in container.decode(stream):
                for f in resampler.resample(frame):
                    parts.append(f.to_ndarray().reshape(-1))
        except av.error.InvalidDataError:
            log("  warning: part of the audio is corrupt; keeping what decoded cleanly")
        for f in resampler.resample(None):
            parts.append(f.to_ndarray().reshape(-1))
    if not parts:
        raise UserError("Could not decode any audio from this file.")
    return np.concatenate(parts)


def find_cuts(audio, chunk_sec):
    """Sample positions that split the audio near every chunk_sec, each at the quietest nearby moment."""
    import numpy as np

    n, step, frame = len(audio), int(chunk_sec * SR), SR // 10
    search = min(30 * SR, step // 4)
    cuts, pos = [0], 0
    while n - pos > step + 60 * SR:          # never leave a tiny last piece
        target = pos + step
        win = audio[target - search:target].astype(np.float32)
        m = len(win) // frame
        energy = (win[: m * frame].reshape(m, frame) ** 2).mean(axis=1)
        smooth = np.convolve(energy, np.ones(5) / 5, mode="same")
        pos = target - search + int(smooth.argmin()) * frame + frame // 2
        cuts.append(pos)
    cuts.append(n)
    return cuts


def encode_audio(samples, path, codec):
    """Write int16 samples as a small speech file: 'opus' (.ogg, 32 kbps) or 'mp3' (64 kbps)."""
    import av

    fmt, encoder, bitrate = ("ogg", "libopus", 32000) if codec == "opus" else ("mp3", "libmp3lame", 64000)
    with av.open(str(path), "w", format=fmt) as out:
        st = out.add_stream(encoder, rate=SR, layout="mono")
        st.bit_rate = bitrate
        for i in range(0, len(samples), SR):
            fr = av.AudioFrame.from_ndarray(samples[i:i + SR].reshape(1, -1), format="s16", layout="mono")
            fr.sample_rate = SR
            fr.pts = i
            fr.time_base = Fraction(1, SR)
            for pkt in st.encode(fr):
                out.mux(pkt)
        for pkt in st.encode(None):
            out.mux(pkt)


# --------------------------------------------------------------------------- providers (cloud)

def retry_after(resp):
    """Seconds the provider asks us to wait, from the header or the error text."""
    h = resp.headers.get("retry-after")
    if h and re.fullmatch(r"\d+(\.\d+)?", h.strip()):
        return float(h)
    text = resp.text[:2000]
    m = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', text)
    if m:
        return float(m.group(1))
    m = re.search(r"try again in (?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?", text)
    if m and any(m.groups()):
        return int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + float(m.group(3) or 0)
    return None


def post_with_retry(send, label, max_wait, stop):
    """send() -> Response. Retries network errors, 429 and 5xx; raises QuotaError when the wait is too long."""
    import requests

    delay, last = 5, None
    for _ in range(8):
        if stop.is_set():
            raise QuotaError("stopped")
        try:
            r = send()
        except requests.RequestException as e:
            log(f"  network error ({e.__class__.__name__}); retrying in {delay}s")
            time.sleep(delay)
            delay = min(delay * 2, 120)
            continue
        last = r.status_code
        if r.status_code != 429 and r.status_code < 500:
            return r
        wait = retry_after(r)
        if r.status_code == 429 and wait and wait > max_wait:
            raise QuotaError(f"{label} rate limit reached; it clears in about {human(wait)}.")
        wait = min(wait or delay, max_wait) + 1
        log(f"  {label} {'rate limit' if r.status_code == 429 else f'server error {r.status_code}'}; "
            f"waiting {human(wait)} and retrying")
        time.sleep(wait)
        delay = min(delay * 2, 120)
    if last == 429:
        raise QuotaError(f"{label} rate limit reached and it is not clearing.")
    raise UserError(f"{label} kept failing after several retries. Try again later or use another --provider.")


def api_error(resp, label):
    try:
        err = resp.json().get("error")
        detail = err.get("message") if isinstance(err, dict) else str(err)
    except Exception:  # noqa: BLE001
        detail = resp.text[:300]
    if resp.status_code in (401, 403):
        return Fatal(f"{label} rejected the API key ({resp.status_code}): {detail}")
    if resp.status_code == 404:
        return Fatal(f"{label} does not know this model or address (404): {detail}")
    return UserError(f"{label} error {resp.status_code}: {detail}")


def call_openai(path, duration, prov, language, task, prompt, max_wait, stop):
    """Groq, OpenAI and every OpenAI-compatible server: POST /audio/transcriptions."""
    import requests

    url = prov["base"].rstrip("/") + ("/audio/translations" if task == "translate" else "/audio/transcriptions")
    headers = {"Authorization": f"Bearer {prov['key']}"} if prov.get("key") else {}
    fmt = "verbose_json"
    while True:
        data = {"model": prov["model"], "response_format": fmt, "temperature": "0"}
        if language and task == "transcribe":
            data["language"] = language
        if prompt:
            data["prompt"] = prompt

        def send():
            with open(path, "rb") as f:
                return requests.post(url, headers=headers, data=data, timeout=600,
                                     files={"file": (os.path.basename(path), f, "audio/ogg")})

        r = post_with_retry(send, prov["label"], max_wait, stop)
        if r.status_code == 400 and fmt == "verbose_json" and "response_format" in r.text:
            fmt = "json"     # newer models return plain text only, so timestamps fall back to one per piece
            continue
        break
    if r.status_code != 200:
        raise api_error(r, prov["label"])
    j = r.json()
    raw, segs = j.get("segments") or [], []
    for s in raw:
        # Whisper's own silence rule: probably no speech and low confidence means a hallucination
        if s.get("no_speech_prob", 0) > 0.6 and s.get("avg_logprob", 0) < -1.0:
            continue
        if (s.get("text") or "").strip():
            segs.append({"start": float(s["start"]), "end": float(s["end"]), "text": s["text"].strip()})
    if not raw and (j.get("text") or "").strip():
        segs.append({"start": 0.0, "end": duration, "text": j["text"].strip()})
    total = sum(max(s["end"] - s["start"], 0.01) for s in raw)
    garbled = bool(raw) and total > 0 and (
        sum(s.get("avg_logprob", 0) * max(s["end"] - s["start"], 0.01) for s in raw) / total < -0.8
        or sum(s.get("compression_ratio", 1) for s in raw) / len(raw) > 2.4)
    return {"language": j.get("language"), "segments": segs, "garbled": garbled}


GEMINI_SCHEMA = {"type": "ARRAY", "items": {
    "type": "OBJECT", "required": ["start", "text"],
    "properties": {"start": {"type": "STRING"}, "text": {"type": "STRING"}}}}


def call_gemini(path, duration, prov, language, task, prompt, max_wait, stop):
    """Google Gemini: the audio goes inline and the answer comes back as timestamped JSON."""
    import requests

    if task == "translate":
        what = "Transcribe this audio and translate it into English. Write only the English translation."
    else:
        what = ("Transcribe this audio word for word in the language that is spoken. "
                "Do not translate, summarize, or leave anything out.")
    if language:
        what += f" The spoken language code is '{language}'."
    if prompt:
        what += f" Names and terms that may appear: {prompt}."
    what += (" Split the text into segments of one or two sentences. For each segment give `start`, the time "
             "the segment begins measured from the start of this clip in MM:SS format, and `text`. "
             "If nobody speaks, return an empty list.")
    body = {
        "contents": [{"parts": [
            {"text": what},
            {"inlineData": {"mimeType": "audio/mp3", "data": base64.b64encode(Path(path).read_bytes()).decode()}},
        ]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json",
                             "responseSchema": GEMINI_SCHEMA},
    }
    url = f"{prov['base']}/models/{prov['model']}:generateContent"
    r = post_with_retry(lambda: requests.post(url, headers={"x-goog-api-key": prov["key"]}, json=body, timeout=900),
                        prov["label"], max_wait, stop)
    if r.status_code != 200:
        raise api_error(r, prov["label"])
    j = r.json()
    cand = (j.get("candidates") or [{}])[0]
    text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []) if not p.get("thought"))
    reason = (j.get("promptFeedback") or {}).get("blockReason") or cand.get("finishReason")
    if not text.strip():
        raise UserError(f"Gemini returned no text for one piece (reason: {reason or 'unknown'}).")
    try:
        items = json.loads(text)
    except ValueError:
        raise UserError("Gemini's answer was cut off before it finished"
                        f" (reason: {reason}). Try shorter pieces: --chunk-minutes 5")

    def secs(v):
        try:
            parts = [float(x) for x in str(v).strip().split(":")]
        except ValueError:
            return None
        return sum(p * 60 ** i for i, p in enumerate(reversed(parts)))

    segs, last = [], 0.0
    for it in items if isinstance(items, list) else []:
        t, start = (it.get("text") or "").strip(), secs(it.get("start"))
        if not t:
            continue
        last = min(max(start if start is not None else last, last), duration)   # keep times in order
        segs.append({"start": last, "end": duration, "text": t})
    for a, b in zip(segs, segs[1:]):
        a["end"] = max(b["start"], a["start"])
    return {"language": language, "segments": segs, "garbled": False}


def transcribe_cloud(audio, source_key, prov, language, task, prompt, args, tmpdir):
    """Cut the audio into pieces, send them in parallel, and stitch the results together.

    Each finished piece is cached in ~/.captionless/cache, so a run stopped by a rate limit
    continues where it left off instead of starting again.
    """
    duration = len(audio) / SR
    cuts = find_cuts(audio, args.chunk_minutes * 60)
    spans = list(zip(cuts, cuts[1:]))
    key = "|".join(map(str, [source_key, prov["name"], prov["model"], language, task, prompt, len(audio),
                             args.chunk_minutes]))
    cache = HOME / "cache" / hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    cached = sum((cache / f"{i:04d}.json").is_file() for i in range(len(spans)))
    log(f"  Sending {len(spans)} piece(s), {fmt_ts(duration)} of audio, to {prov['label']} [{prov['model']}]"
        + (f" ({cached} already done in an earlier run)" if cached else "") + "...")
    call = call_gemini if prov["kind"] == "gemini" else call_openai
    codec, ext = ("mp3", "mp3") if prov["kind"] == "gemini" else ("opus", "ogg")
    stop, done, lock = threading.Event(), [cached], threading.Lock()

    def work(i):
        f = cache / f"{i:04d}.json"
        if f.is_file():
            return json.loads(f.read_text(encoding="utf-8"))
        if stop.is_set():
            raise QuotaError("stopped")
        a, b = spans[i]
        piece = Path(tmpdir) / f"piece{i:04d}.{ext}"
        encode_audio(audio[a:b], piece, codec)
        try:
            res = call(piece, (b - a) / SR, prov, language, task, prompt, args.max_wait, stop)
            if prompt and res["garbled"]:
                log("  a piece came out garbled with --prompt; redoing it without the prompt")
                res = call(piece, (b - a) / SR, prov, language, task, None, args.max_wait, stop)
        except QuotaError:
            stop.set()
            raise
        finally:
            piece.unlink(missing_ok=True)
        cache.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
        with lock:
            done[0] += 1
            log(f"  piece {done[0]}/{len(spans)} done")
        return res

    with ThreadPoolExecutor(max_workers=prov.get("workers", 2)) as pool:
        futures = [pool.submit(work, i) for i in range(len(spans))]
        errors = [f.exception() for f in futures]
    quota = next((e for e in errors if isinstance(e, QuotaError) and str(e) != "stopped"), None)
    first = quota or next((e for e in errors if e), None)
    if first:
        raise first
    results = [f.result() for f in futures]

    segments, langs = [], []
    for res, (a, _) in zip(results, spans):
        if res.get("language"):
            langs.append(str(res["language"]))
        for s in res["segments"]:
            text = clean_text(s["text"])
            if text:
                segments.append({"start": s["start"] + a / SR, "end": s["end"] + a / SR, "text": text})
    shutil.rmtree(cache, ignore_errors=True)
    detected = max(set(langs), key=langs.count) if langs else None
    return segments, detected, f"{prov['label']} {prov['model']}"


# --------------------------------------------------------------------------- provider (local)

def add_cuda_dlls():
    """Windows: make pip-installed NVIDIA libraries (cuBLAS, cuDNN) loadable."""
    import site

    if os.name != "nt" or os.environ.get("CAPTIONLESS_NO_CUDA_DLLS"):
        return
    for sp in site.getsitepackages() + [site.getusersitepackages()]:
        for d in Path(sp, "nvidia").glob("*/bin"):
            try:
                os.add_dll_directory(str(d))
            except OSError:
                pass
            os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")


def load_local_model(name, device):
    from faster_whisper import WhisperModel

    if device in ("auto", "cuda"):
        try:
            import ctranslate2
            if ctranslate2.get_cuda_device_count() > 0:
                return WhisperModel(name, device="cuda", compute_type="auto"), "GPU"
        except Exception as e:  # noqa: BLE001
            if device == "cuda":
                raise
            log(f"  GPU not usable ({e.__class__.__name__}); using the CPU")
    return WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=os.cpu_count() or 4), "CPU"


def transcribe_local(audio, prov, language, task, prompt, args, state):
    """faster-whisper on this machine: private and unlimited, but slower, and it downloads a model once."""
    import numpy as np

    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    ensure_deps(local=True)
    samples = audio.astype(np.float32) / 32768.0
    duration = len(audio) / SR

    def run(model):
        it, info = model.transcribe(samples, language=language, task=task, beam_size=5, initial_prompt=prompt,
                                    vad_filter=True, vad_parameters={"min_silence_duration_ms": 500},
                                    condition_on_previous_text=False)
        segs, mark, t0 = [], 10, time.time()
        for s in it:
            text = clean_text(s.text)
            if text:
                segs.append({"start": s.start, "end": s.end, "text": text})
            pct = min(100, int(s.end / duration * 100)) if duration else 0
            if pct >= mark:
                log(f"  {pct:3d}%  ({fmt_ts(s.end)} of {fmt_ts(duration)}, {human(time.time() - t0)} so far)")
                mark = (pct // 10 + 1) * 10
        return segs, info.language

    if state.get("model") is None:
        add_cuda_dlls()
        log(f"  Loading the local model '{prov['model']}' (the first use downloads it)...")
        state["model"], state["device"] = load_local_model(prov["model"], args.device)
        log(f"  Running on the {state['device']}")
    try:
        segs, lang = run(state["model"])
    except RuntimeError as e:
        if state["device"] != "GPU" or args.device == "cuda":
            raise
        log(f"  The GPU could not be used ({str(e).splitlines()[0]}); switching to the CPU")
        state["model"], state["device"] = load_local_model(prov["model"], "cpu")
        segs, lang = run(state["model"])
    return segs, lang, f"Local Whisper {prov['model']} ({state['device']})"


# --------------------------------------------------------------------------- command: transcribe

def process(item, args, ctx):
    """Transcribe one video or file. Returns the output folder."""
    out_dir, done = ctx["out"], ctx["done"]
    tmpdir = tempfile.mkdtemp(prefix="captionless-")
    try:
        language = args.lang.lower() if args.lang else None
        segments = None
        if item["kind"] == "file":
            p = Path(item["value"]).resolve()
            st = p.stat()
            meta = dict(kind="file", id=None, title=p.stem, source=str(p), channel=None)
            source_key, audio_path = f"file:{p}:{st.st_size}:{int(st.st_mtime)}", str(p)
        else:
            import yt_dlp

            opts = ydl_options(args, format="bestaudio/best", outtmpl=os.path.join(tmpdir, "%(id)s.%(ext)s"))
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(item["value"], download=False)
                    if info.get("_type") == "playlist":
                        raise UserError("This link is a playlist or channel. Add --playlist to transcribe "
                                        "every video in it.")
                    if info.get("is_live"):
                        raise UserError("This is a live stream that is still running; try again once it ends.")
                    meta = dict(kind="url", id=info.get("id"), title=info.get("title") or info.get("id") or "video",
                                source=info.get("webpage_url") or item["value"], duration=info.get("duration"),
                                channel=info.get("channel") or info.get("uploader"))
                    chapters = sorted((float(c.get("start_time") or 0), str(c["title"]).strip())
                                      for c in info.get("chapters") or [] if str(c.get("title") or "").strip())
                    if len(chapters) > 1:
                        meta["chapters"] = chapters
                    if meta["id"] in done and not args.force:
                        return done[meta["id"]], True
                    log(f"  {meta['title']}")
                    language = language or spoken_language(info.get("language"))
                    if args.captions != "never" and not args.translate:
                        found = fetch_captions(ydl, info, language, args.captions)
                        if found:
                            segments, lang, engine = found
                            log(f"  Using the {engine}; no AI transcription needed")
                            meta.update(language=lang, engine=engine[0].upper() + engine[1:])
                    if segments is None:
                        log("  Downloading the audio...")
                        try:
                            got = ydl.process_ie_result(info, download=True)
                        except yt_dlp.utils.DownloadError:
                            raise
                        except Exception:  # noqa: BLE001 - fall back to a plain second extraction
                            got = ydl.extract_info(item["value"], download=True)
                        reqs = got.get("requested_downloads") or []
                        audio_path = reqs[0]["filepath"] if reqs and reqs[0].get("filepath") \
                            else ydl.prepare_filename(got)
            except yt_dlp.utils.DownloadError as e:
                raise download_error(e)
            source_key = f"{info.get('extractor_key')}:{meta['id']}"

        if segments is None:
            prov = ctx.get("provider")
            if prov is None:
                prov = ctx["provider"] = resolve_provider(args.provider, args.model, args.fast, args.translate)
            task = "translate" if args.translate else "transcribe"
            log("  Preparing the audio...")
            audio = decode_audio(audio_path)
            meta.setdefault("duration", len(audio) / SR)
            meta["duration"] = meta["duration"] or len(audio) / SR
            if prov["kind"] == "local":
                segments, detected, engine = transcribe_local(audio, prov, language, task, args.prompt, args,
                                                              ctx["local"])
            else:
                segments, detected, engine = transcribe_cloud(audio, source_key, prov, language, task, args.prompt,
                                                              args, tmpdir)
            meta.update(language="en (translated)" if args.translate else (language or detected), engine=engine)

        folder = write_outputs(segments, meta, out_dir, done, not args.no_timestamps)
        if meta.get("id"):
            done[meta["id"]] = folder
        print(f"FOLDER:     {folder}")
        print(f"TRANSCRIPT: {folder / 'transcript.txt'}")
        print(f"SUBTITLES:  {folder / 'subtitles.srt'}")
        for extra in write_extras(folder, ctx.get("formats") or ()):
            print(f"ALSO:       {extra}")
        print(f"ENGINE:     {meta['engine']}")
        if not segments:
            print("WARNING:    no speech was found in this audio")
        elif looks_unreliable(" ".join(s["text"] for s in segments)):
            print("WARNING:    this transcript looks unreliable (repeated letters); run it again with --force")
        sys.stdout.flush()
        return folder, False
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def transcribe_parser(prog="captionless", description=None, inputs=True):
    """The options shared by every command that transcribes (`watch run` takes them without the links)."""
    ap = argparse.ArgumentParser(
        prog=prog,
        description=description or "Transcribe online videos (no captions needed) or local audio/video files.",
        epilog="Other commands: search, fix, export, watch, mcp, doctor, update. "
               "Run `captionless.py <command> --help`.")
    if inputs:
        ap.add_argument("inputs", nargs="+", help="video links, playlist links, or local media files")
        ap.add_argument("--playlist", action="store_true", help="transcribe every video of the playlist or channel")
        ap.add_argument("--from", dest="start", type=int, default=1, metavar="N", help="first playlist position")
        ap.add_argument("--to", dest="end", type=int, default=0, metavar="N", help="last playlist position")
        ap.add_argument("--force", action="store_true", help="redo videos that already have a transcript")
    else:
        ap.set_defaults(playlist=False, start=1, end=0, force=False)
    ap.add_argument("-l", "--lang", help="spoken language code such as en, es, ar, ja (default: detected)")
    ap.add_argument("-p", "--provider", choices=["auto", *PROVIDERS],
                    help="speech-to-text service (default: the first one with an API key)")
    ap.add_argument("-m", "--model", help="model name, overriding the provider's default")
    ap.add_argument("--fast", action="store_true", help="use the provider's quicker, slightly less accurate model")
    ap.add_argument("--translate", action="store_true", help="write the transcript in English, whatever is spoken")
    ap.add_argument("--captions", choices=["manual", "any", "never"], default="manual",
                    help="reuse existing captions instead of AI: manual = only ones the uploader made (default), "
                         "any = also automatic captions, never = always transcribe")
    ap.add_argument("--limit", type=int, default=0, metavar="N", help="stop after N new transcripts")
    ap.add_argument("--formats", metavar="LIST",
                    help="extra files to write next to transcript.txt and subtitles.srt: md (Markdown with "
                         "clickable timestamps), vtt (WebVTT subtitles), or both as md,vtt")
    ap.add_argument("--prompt", help="names or terms to spell correctly (use sparingly: a bad hint hurts accuracy)")
    ap.add_argument("--chunk-minutes", type=float, default=10, metavar="M", help="length of each piece sent")
    ap.add_argument("--max-wait", type=int, default=900, metavar="SEC",
                    help="longest pause to sit out a rate limit before stopping (default: 900)")
    ap.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto", help="for --provider local")
    ap.add_argument("-o", "--out", help=f"output folder (default: {default_out()})")
    ap.add_argument("--no-timestamps", action="store_true", help="leave [mm:ss] markers out of transcript.txt")
    ap.add_argument("--cookies-from-browser", metavar="BROWSER",
                    help="use this browser's login (chrome, edge, firefox, safari) for restricted videos")
    return ap


def prepare(args):
    """What every transcribing command does first: check the options and the packages. Returns the output folder."""
    if not 1 <= args.chunk_minutes <= 20:
        raise UserError("--chunk-minutes must be between 1 and 20")
    args.formats = parse_formats(args.formats if args.formats is not None else setting("CAPTIONLESS_FORMATS"))
    wants_local = (args.provider or setting("CAPTIONLESS_PROVIDER") or "").lower() == "local"
    ensure_deps(local=wants_local)
    cache_root = HOME / "cache"
    if cache_root.is_dir():   # forget unfinished runs older than a week
        for d in cache_root.iterdir():
            if d.is_dir() and time.time() - d.stat().st_mtime > 7 * 86400:
                shutil.rmtree(d, ignore_errors=True)
    return Path(args.out).expanduser() if args.out else default_out()


def cmd_transcribe(argv):
    args = transcribe_parser().parse_args(argv)
    out_dir = prepare(args)
    items = []
    for inp in args.inputs:
        if os.path.isfile(inp):
            items.append(dict(kind="file", value=inp, id=None, title=Path(inp).name))
        elif args.playlist or is_playlist_url(inp):
            title, found = expand_playlist(inp, args)
            if title:
                log(f"Playlist: {title} ({len(found)} video(s) selected)")
            items += found
        else:
            items.append(dict(kind="url", value=inp, id=youtube_id(inp), title=None))
    return run_items(items, args, out_dir)[0]


def run_items(items, args, out_dir):
    """Transcribe a list of videos and files one after another. Returns (exit code, failed items)."""
    ctx = dict(out=out_dir, done=scan_done(out_dir), provider=None, local={}, formats=args.formats)
    new = skipped = 0
    failures, stopped, limited = [], None, None
    for n, item in enumerate(items, 1):
        if args.limit and new >= args.limit:
            limited = f"Reached --limit {args.limit} with {len(items) - n + 1} video(s) left."
            break
        if item["id"] and item["id"] in ctx["done"] and not args.force:
            skipped += 1
            print(f"SKIPPED (already done): {ctx['done'][item['id']]}")
            continue
        log(f"\n=== [{n}/{len(items)}] {item.get('title') or item['value']}")
        t0 = time.time()
        try:
            folder, was_done = process(item, args, ctx)
        except QuotaError as e:
            stopped = f"{e} Finished pieces are saved."
            break
        except Fatal:
            raise
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa: BLE001 - one bad video must not end a playlist run
            msg = str(e).strip() or e.__class__.__name__
            log("  FAILED: " + msg)
            failures.append((item, msg.splitlines()[0]))
            continue
        if was_done:
            skipped += 1
            print(f"SKIPPED (already done): {folder}")
        else:
            new += 1
            log(f"  Done in {human(time.time() - t0)}")

    sys.stdout.flush()
    if len(items) > 1 or stopped or failures:
        log(f"\n{new} transcribed, {skipped} already done, {len(failures)} failed.")
    for item, msg in failures:
        log(f"  failed: {item.get('title') or item['value']}: {msg}")
    if limited:
        log(f"{limited} Run the same command again to continue; finished videos are skipped.")
    if stopped:
        log(f"STOPPED: {stopped} Run the same command again to continue; finished videos are skipped.")
        return 2, failures
    return (1 if failures else 0), failures


# --------------------------------------------------------------------------- command: search

def normalize(text):
    """Fold case, accents and Arabic spelling variants so a search finds what a person means."""
    text = unicodedata.normalize("NFKD", text).casefold()
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace("ـ", "").replace("ى", "ي").replace("ة", "ه")
    return re.sub(r"\s+", " ", text)


def read_header(txt):
    meta = {}
    with open(txt, encoding="utf-8") as f:
        for line in f:
            if line.startswith("---") or len(meta) > 12:
                break
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip().lower()] = v.strip()
    return meta


def read_cues(folder):
    """(start seconds, text) pairs from subtitles.srt, or from transcript.txt paragraphs."""
    cues = []
    srt = folder / "subtitles.srt"
    if srt.is_file():
        for block in re.split(r"\n\s*\n", srt.read_text(encoding="utf-8")):
            m = re.search(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->", block)
            if m:
                text = " ".join(block[m.end():].splitlines()[1:]).strip()
                h, mi, s, ms = (int(x) for x in m.groups())
                cues.append((h * 3600 + mi * 60 + s + ms / 1000, text))
        if cues:
            return cues
    for line in (folder / "transcript.txt").read_text(encoding="utf-8").splitlines():
        m = re.match(r"\[(?:(\d+):)?(\d+):(\d+)\]\s*(.*)", line)
        if m:
            cues.append((int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3)), m.group(4)))
    return cues


def cmd_search(argv):
    ap = argparse.ArgumentParser(prog="captionless search",
                                 description="Find where something was said, across every saved transcript.")
    ap.add_argument("query", nargs="+", help="words that must all appear close together")
    ap.add_argument("-n", "--limit", type=int, default=20, help="most results to show (default: 20)")
    ap.add_argument("--channel", help="only search channels whose folder name contains this text")
    ap.add_argument("-o", "--out", help="transcripts folder")
    args = ap.parse_args(argv)
    out_dir = Path(args.out).expanduser() if args.out else default_out()
    print(search_library(out_dir, " ".join(args.query), args.channel, args.limit))
    return 0


def search_library(out_dir, query, channel=None, limit=20):
    """The search results as text: each one names the video, the moment, a link, and the passage."""
    phrase = normalize(query)
    terms = phrase.split()
    hits = []
    for txt in sorted(out_dir.glob("*/*/transcript.txt")):
        folder = txt.parent
        if channel and normalize(channel) not in normalize(folder.parent.name):
            continue
        cues = read_cues(folder)
        norm = [normalize(t) for _, t in cues]
        meta, i = None, 0
        while i < len(cues):
            j = i + 1   # a window of up to 4 cues, or about a minute
            while j < len(cues) and j - i < 4 and cues[j][0] - cues[i][0] < 60:
                j += 1
            window = " ".join(norm[i:j])
            if all(t in window for t in terms) and any(t in norm[i] for t in terms):
                meta = meta or read_header(txt)
                snippet = " ".join(t for _, t in cues[i:j])
                hits.append((phrase not in window, meta, cues[i][0], snippet, txt))
                i = j
            else:
                i += 1
    hits.sort(key=lambda h: h[0])   # exact phrase first, otherwise library order
    if not hits or not terms:
        return f'No matches for "{query}" in {out_dir}'
    out = [f'{len(hits)} match(es) for "{query}"' + (f", showing the first {limit}" if len(hits) > limit else ""),
           ""]
    for n, (_, meta, start, snippet, txt) in enumerate(hits[:limit], 1):
        title = meta.get("title", txt.parent.name)
        out += [f"[{n}] {title}" + (f"  ({meta['channel']})" if meta.get("channel") else ""),
                f"    {fmt_ts(start)}  {time_link(meta.get('source'), start)}",
                f"    {snippet[:400]}{'...' if len(snippet) > 400 else ''}",
                f"    {txt}", ""]
    return "\n".join(out)


# --------------------------------------------------------------------------- commands: fix, doctor, update

def cmd_fix(argv):
    ap = argparse.ArgumentParser(
        prog="captionless fix",
        description="Re-apply corrections.txt (lines of `wrong => right`) to transcripts that are already saved. "
                    "One corrections.txt in the transcripts folder applies everywhere; one inside a channel "
                    "folder applies to that channel.")
    ap.add_argument("folder", nargs="?", help="a channel or video folder (default: the whole library)")
    ap.add_argument("-o", "--out", help="transcripts folder")
    args = ap.parse_args(argv)
    out_dir = Path(args.out).expanduser() if args.out else default_out()
    target = Path(args.folder).expanduser() if args.folder else out_dir
    changed = 0
    for txt in sorted(target.rglob("transcript.txt")):
        folder = txt.parent
        rules = load_corrections(out_dir / "corrections.txt", folder.parent / "corrections.txt")
        if not rules:
            continue
        touched = False
        text = txt.read_text(encoding="utf-8")
        head, sep, body = text.partition(SEPARATOR)
        fixed = head + sep + apply_corrections(body, rules) if sep else apply_corrections(text, rules)
        if fixed != text:
            txt.write_text(fixed, encoding="utf-8")
            touched = True
        srt = folder / "subtitles.srt"
        if srt.is_file():
            lines = srt.read_text(encoding="utf-8").split("\n")
            new = [ln if ("-->" in ln or ln.strip().isdigit()) else apply_corrections(ln, rules) for ln in lines]
            if new != lines:
                srt.write_text("\n".join(new), encoding="utf-8")
                touched = True
        if touched:
            changed += 1
            write_extras(folder)   # keep transcript.md and subtitles.vtt, where they exist, in step
            print(f"fixed: {folder}")
    print(f"{changed} transcript(s) updated.")
    return 0


def cmd_export(argv):
    ap = argparse.ArgumentParser(
        prog="captionless export",
        description="Make extra copies of transcripts that are already saved, without transcribing again: "
                    "transcript.md (Markdown with clickable timestamps, for Obsidian, Notion and the like) "
                    "and subtitles.vtt (WebVTT, for web players).")
    ap.add_argument("folder", nargs="?", help="a channel or video folder (default: the whole library)")
    ap.add_argument("--formats", default="md,vtt", metavar="LIST", help="md, vtt, or md,vtt (default: both)")
    ap.add_argument("-o", "--out", help="transcripts folder")
    args = ap.parse_args(argv)
    formats = parse_formats(args.formats)
    out_dir = Path(args.out).expanduser() if args.out else default_out()
    target = Path(args.folder).expanduser() if args.folder else out_dir
    count = 0
    for txt in sorted(target.rglob("transcript.txt")):
        if write_extras(txt.parent, formats):
            count += 1
            print(f"exported: {txt.parent}")
    print(f"{count} transcript(s) exported as {', '.join(sorted(formats)) or 'nothing'}.")
    return 0


# --------------------------------------------------------------------------- command: watch

WATCH_DEPTH = 50   # how many of a channel's newest videos are looked at on each check
YT_CHANNEL = re.compile(r"(https?://(?:www\.|m\.)?youtube\.com/(?:@[^/?#]+|(?:channel|c|user)/[^/?#]+))/?(?:[?#].*)?$")


def watch_file():
    return HOME / "watch.json"


def load_watch():
    f = watch_file()
    if not f.is_file():
        return []
    try:
        return json.loads(f.read_text(encoding="utf-8")).get("sources", [])
    except (ValueError, AttributeError):
        raise UserError(f"{f} is damaged. Fix it or delete it, then add your channels again.")


def save_watch(sources):
    HOME.mkdir(parents=True, exist_ok=True)
    tmp = watch_file().with_suffix(".tmp")
    tmp.write_text(json.dumps({"sources": sources}, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(watch_file())


def watch_listing(url, args):
    """What is behind a watched link right now: a whole playlist, or a channel's newest videos (newest first)."""
    channel = YT_CHANNEL.match(url)
    if channel:
        url = channel.group(1) + "/videos"
    is_list = bool(re.search(r"[?&]list=", url))
    title, items = expand_playlist(url, args, **({} if is_list else {"playlistend": WATCH_DEPTH}))
    if title is None:
        raise UserError("This link is a single video. Watching works on a channel or a playlist link.")
    return re.sub(r" - Videos$", "", title), [i for i in items if i.get("id")], not is_list


def cmd_watch(argv):
    usage = ("captionless watch add <channel or playlist link> [...]   start watching; videos already there are "
             "left alone\n"
             "       captionless watch list                                    show what is watched\n"
             "       captionless watch remove <number or link>                 stop watching\n"
             "       captionless watch run [transcribe options]                transcribe what is new since the "
             "last check")
    sub, rest = (argv[0] if argv else ""), argv[1:]
    if sub == "run":
        ap = transcribe_parser("captionless watch run", inputs=False,
                               description="Transcribe every video that appeared on the watched channels and "
                                           "playlists since the last check. Schedule this command (cron, Task "
                                           "Scheduler) to keep a library up to date on its own.")
        return watch_run(ap.parse_args(rest))
    ap = argparse.ArgumentParser(prog="captionless watch", usage=usage,
                                 description="Follow channels and playlists, and transcribe their new videos.")
    ap.add_argument("action", choices=["add", "list", "remove"])
    ap.add_argument("targets", nargs="*", help="links to add, or the number or link to remove")
    ap.add_argument("--cookies-from-browser", metavar="BROWSER", help="use this browser's login for the check")
    args = ap.parse_args(argv)
    sources = load_watch()
    if args.action == "list":
        if not sources:
            print("Nothing is being watched. Add a channel or playlist: captionless.py watch add <link>")
        for n, s in enumerate(sources, 1):
            print(f"[{n}] {s['title']}\n    {s['url']}\n    added {s.get('added', '?')}, "
                  f"last checked {s.get('checked') or 'never'}")
        return 0
    if not args.targets:
        ap.error(f"`watch {args.action}` needs a link" + (" or a number" if args.action == "remove" else ""))
    if args.action == "remove":
        for t in args.targets:
            hit = next((s for n, s in enumerate(sources, 1) if t == str(n) or t == s["url"]), None)
            if not hit:
                raise UserError(f"'{t}' is not in the watch list. See it with: captionless.py watch list")
            sources = [s for s in sources if s is not hit]
            print(f"REMOVED: {hit['title']}")
        save_watch(sources)
        return 0
    ensure_deps()
    for url in args.targets:
        if any(s["url"] == url for s in sources):
            print(f"ALREADY WATCHED: {url}")
            continue
        title, items, _ = watch_listing(url, args)
        sources.append(dict(url=url, title=title, added=time.strftime("%Y-%m-%d"), checked=None,
                            seen=[i["id"] for i in items], failed={}))
        save_watch(sources)
        print(f"WATCHING: {title}  ({len(items)} video(s) already there are left alone; "
              "`watch run` transcribes what appears from now on)")
    return 0


def watch_run(args):
    sources = load_watch()
    if not sources:
        print("Nothing is being watched. Add a channel or playlist: captionless.py watch add <link>")
        return 0
    out_dir = prepare(args)
    done, items, fresh_by_source, unreachable = scan_done(out_dir), [], [], 0
    for s in sources:
        try:
            title, listed, newest_first = watch_listing(s["url"], args)
        except UserError as e:
            log(f"Could not check {s['title']}: {str(e).splitlines()[0]}")
            fresh_by_source.append([])
            unreachable += 1
            continue
        seen = set(s["seen"])
        fresh = [i for i in listed if i["id"] not in seen]
        todo = [i for i in fresh if i["id"] not in done]
        if newest_first:
            todo.reverse()   # oldest of the new ones first, the order they came out in
        s["checked"] = time.strftime("%Y-%m-%d %H:%M")
        fresh_by_source.append(fresh)
        items += [i for i in todo if i["id"] not in {x["id"] for x in items}]
        print(f"NEW: {len(todo)} in {title}")
    code, failures = run_items(items, args, out_dir) if items else (0, [])
    not_yet = re.compile(r"Premieres? in|will begin in|live (stream|event)", re.IGNORECASE)   # these come later
    done, failed_ids = scan_done(out_dir), {item["id"] for item, msg in failures if not not_yet.search(msg)}
    for s, fresh in zip(sources, fresh_by_source):
        fails = s.setdefault("failed", {})
        for i in fresh:
            if i["id"] in failed_ids:
                fails[i["id"]] = fails.get(i["id"], 0) + 1
            if i["id"] in done or fails.get(i["id"], 0) >= 3:   # done, or failing every time: stop trying
                if i["id"] not in done:
                    log(f"Giving up on {i.get('title') or i['id']} after 3 failed attempts.")
                s["seen"].append(i["id"])
                fails.pop(i["id"], None)
    save_watch(sources)
    if not items:
        print("No new videos.")
    return code or (1 if unreachable else 0)


def cmd_doctor(argv):
    argparse.ArgumentParser(prog="captionless doctor", description="Check the setup.").parse_args(argv)
    out_dir = default_out()
    print(f"captionless {__version__}")
    print(f"Python       {sys.version.split()[0]}  ({sys.executable})")
    ready = have(BASE_MODS) or (VENV / "captionless-requirements.txt").exists()
    print(f"Packages     {'ready' if ready else 'not installed yet (the first transcription installs them)'}")
    js = find_js_runtimes()
    print("JS runtime   " + (", ".join(f"{k} ({v['path']})" for k, v in js.items()) if js else "none found"))
    if not js:
        print("             " + JS_HINT.replace("\n", "\n             "))
    for name, p in PROVIDERS.items():
        if name == "local":
            state = "available (downloads a model on first use)"
        elif name == "custom":
            state = f"server {setting('CAPTIONLESS_BASE_URL')}" if setting("CAPTIONLESS_BASE_URL") \
                else "not configured (CAPTIONLESS_BASE_URL)"
        else:
            state = "key found" if provider_key(name) else f"no key ({' or '.join(p['keys'])})"
        print(f"{'Providers' if name == 'groq' else '':<12} {name:<7} {state}")
    try:
        prov = resolve_provider(None, None, False, False)
        print(f"Default      {prov['name']} [{prov['model']}]")
    except UserError:
        print("Default      none: add an API key or use --provider local")
    count = len(list(out_dir.glob("*/*/transcript.txt"))) if out_dir.is_dir() else 0
    print(f"Output       {out_dir}  ({count} transcript(s))")
    cfg = HOME / "config.env"
    print(f"Config file  {cfg}  ({'found' if cfg.is_file() else 'not created'})")
    try:
        print(f"Watching     {len(load_watch())} channel(s) or playlist(s)")
    except UserError as e:
        print(f"Watching     {e}")
    return 0


def cmd_update(argv):
    argparse.ArgumentParser(prog="captionless update",
                            description="Upgrade the video downloader (yt-dlp).").parse_args(argv)
    py = venv_python() if venv_python().exists() else Path(sys.executable)
    r = subprocess.run([str(py), "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--upgrade",
                        "yt-dlp[default]"], stdout=sys.stderr)
    if r.returncode:
        raise UserError("The upgrade failed; see the pip messages above. If you installed captionless with pipx "
                        "or uv, upgrade it there instead: `pipx upgrade captionless` or `uv tool upgrade captionless`.")
    v = subprocess.run([str(py), "-c", "import yt_dlp.version as v; print(v.__version__)"],
                       capture_output=True, text=True).stdout.strip()
    print(f"yt-dlp is up to date ({v}).")
    return 0


# --------------------------------------------------------------------------- command: mcp
#
# A small Model Context Protocol server over stdin/stdout, so that apps without a terminal
# (Claude Desktop, Cursor, ...) can use the same library. It speaks plain JSON-RPC and needs no extra package.

MCP_VERSIONS = ("2025-06-18", "2025-11-25", "2025-03-26", "2024-11-05")
MCP_CHUNK = 30000   # characters of transcript returned per call; the rest is read with read_transcript
MCP_GUIDE = (
    "captionless turns a video into text, even when the video has no captions, and keeps every transcript in a "
    "folder on the user's computer. After `transcribe`, read the text and do what the user asked (by default: a "
    "short overview, the main topics with their timestamps, and the key takeaways), then save what you wrote "
    "with `save_note` so it sits next to the transcript. Speech recognition makes mistakes, mostly in names, "
    "rare words and quotations: say so when it matters, and ask the user to check scripture, legal or medical "
    "wording and exact quotes against a reliable source. Never ask the user to type an API key into the chat; "
    "keys belong in the environment or in the config file that the error message names.")
MCP_TOOLS = [
    {"name": "transcribe",
     "description": "Transcribe one online video (YouTube and most other sites) or a media file on this computer, "
                    "even when it has no captions. Saves the transcript and subtitles in the user's library and "
                    "returns the text with [mm:ss] timestamps. A video that is already in the library is returned "
                    "at once. Long videos take a few minutes: if the answer says the work is still running, wait "
                    "about 30 seconds and call this tool again with the same arguments to collect the result.",
     "inputSchema": {"type": "object", "required": ["source"], "properties": {
         "source": {"type": "string", "description": "The video link, or the full path of an audio/video file."},
         "language": {"type": "string", "description": "Spoken language code such as en, es, ar, ja. Leave it "
                                                       "out to detect the language; set it only if the user "
                                                       "names the language or the result came out wrong."},
         "translate": {"type": "boolean", "description": "Write the transcript in English, whatever is spoken."},
         "force": {"type": "boolean", "description": "Transcribe again even if the video is already saved."}}}},
    {"name": "read_transcript",
     "description": "Read a saved transcript, or the next part of a long one.",
     "inputSchema": {"type": "object", "required": ["video"], "properties": {
         "video": {"type": "string", "description": "The video id, part of its title, or its folder path."},
         "offset": {"type": "integer", "description": "Character position to start from (default 0). The "
                                                      "previous part tells you the next offset."}}}},
    {"name": "search_transcripts",
     "description": "Find where something was said, across every saved transcript. All the words must appear "
                    "close together. It ignores case, accents and Arabic spelling variants but matches words, not "
                    "meaning, so try a few likely wordings in the language of the videos. Each result has a "
                    "timestamp and a link to that moment.",
     "inputSchema": {"type": "object", "required": ["query"], "properties": {
         "query": {"type": "string", "description": "Words to find."},
         "channel": {"type": "string", "description": "Only search channels whose name contains this text."},
         "limit": {"type": "integer", "description": "Most results to return (default 10)."}}}},
    {"name": "list_transcripts",
     "description": "List the videos in the user's transcript library, newest first.",
     "inputSchema": {"type": "object", "properties": {
         "channel": {"type": "string", "description": "Only channels whose name contains this text."},
         "limit": {"type": "integer", "description": "Most videos to list (default 30)."}}}},
    {"name": "save_note",
     "description": "Save something you wrote about a video (a summary, notes, a translation, translated "
                    "subtitles) as a file in that video's folder, next to its transcript.",
     "inputSchema": {"type": "object", "required": ["video", "filename", "content"], "properties": {
         "video": {"type": "string", "description": "The video id, part of its title, or its folder path."},
         "filename": {"type": "string", "description": "A plain file name ending in .md, .txt, .srt or .vtt, for "
                                                       "example summary.md or subtitles.fr.srt."},
         "content": {"type": "string", "description": "The text of the file."}}}},
]


def find_video(out_dir, ref):
    """The folder of a saved video, named by its id, its folder path, or part of its title."""
    ref = str(ref or "").strip()
    if not ref:
        raise UserError("Say which video: its id, part of its title, or its folder path.")
    if (Path(ref) / "transcript.txt").is_file():
        return Path(ref)
    done = scan_done(out_dir)
    if ref in done:
        return done[ref]
    if youtube_id(ref) in done:
        return done[youtube_id(ref)]
    want = normalize(ref)
    hits = [t.parent for t in out_dir.glob("*/*/transcript.txt") if want in normalize(t.parent.name)]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise UserError(f"No saved transcript matches '{ref}'. Use list_transcripts to see what is saved.")
    raise UserError(f"'{ref}' matches {len(hits)} videos; use the id in square brackets:\n"
                    + "\n".join(f"- {h.name}" for h in hits[:15]))


def transcript_part(folder, offset=0):
    """The header and one part of a transcript, with a pointer to the next part when there is one."""
    txt = folder / "transcript.txt"
    head, sep, body = txt.read_text(encoding="utf-8").partition(SEPARATOR)
    if not sep:
        head, body = "", head
    body = body.strip("\n")
    offset = max(0, min(int(offset or 0), len(body)))
    end = min(len(body), offset + MCP_CHUNK)
    if end < len(body):   # stop at the end of a paragraph
        cut = body.rfind("\n\n", offset, end)
        end = cut if cut > offset else end
    lines = [head.strip(), f"Folder:   {folder}"]
    m = re.search(r"\[([^\[\]]+)\]$", folder.name)
    if m:
        lines.append(f"Video id: {m.group(1)}")
    others = sorted(p.name for p in folder.iterdir() if p.is_file() and p.name != "transcript.txt")
    if others:
        lines.append(f"Files:    transcript.txt, {', '.join(others)}")
    if offset or end < len(body):
        lines.append(f"Showing characters {offset}-{end} of {len(body)}.")
    lines += [SEPARATOR, "", body[offset:end].strip("\n")]
    if end < len(body):
        ref = m.group(1) if m else str(folder)
        lines += ["", f'[There is more. Call read_transcript with video="{ref}" and offset={end}.]']
    return "\n".join(x for x in lines if x is not None)


_MCP_JOBS, _MCP_LOCK, _MCP_ONE_AT_A_TIME = {}, threading.Lock(), threading.Lock()


def mcp_transcribe(a, progress):
    """Start (or rejoin) a transcription and wait a while for it; long ones are collected with a second call."""
    source = str(a.get("source") or "").strip()
    if not source:
        raise UserError("`source` is needed: a video link or a file path.")
    key = (source, a.get("language") or "", bool(a.get("translate")))
    with _MCP_LOCK:
        job = _MCP_JOBS.get(key)
        if job is None:
            job = _MCP_JOBS[key] = dict(done=threading.Event(), last="waiting to start", text=None, error=None)

            def work():
                global _LOG_SINK
                with _MCP_ONE_AT_A_TIME:   # one video at a time keeps within the providers' rate limits
                    _LOG_SINK = lambda msg: job.update(last=msg)   # noqa: E731
                    try:
                        argv = [source] + (["--lang", str(a["language"])] if a.get("language") else []) \
                            + (["--translate"] if a.get("translate") else []) + (["--force"] if a.get("force") else [])
                        args = transcribe_parser().parse_args(argv)
                        out_dir = prepare(args)
                        if os.path.isfile(source):
                            item = dict(kind="file", value=source, id=None, title=Path(source).name)
                        else:
                            item = dict(kind="url", value=source, id=youtube_id(source), title=None)
                        ctx = dict(out=out_dir, done=scan_done(out_dir), provider=None, local={}, formats=args.formats)
                        folder, was_done = process(item, args, ctx)
                        text = transcript_part(folder)
                        if looks_unreliable(text.partition(SEPARATOR)[2]):
                            text = ("WARNING: this transcript looks unreliable (repeated letters). Call transcribe "
                                    "again with force=true before using it.\n\n") + text
                        job["text"] = ("Already in the library.\n\n" if was_done else "Transcribed.\n\n") + text
                    except QuotaError as e:
                        job["error"] = (f"{e} The finished pieces are saved, so calling transcribe again later "
                                        "continues where this stopped.")
                    except BaseException as e:  # noqa: BLE001 - the job must always report back
                        msg = str(e).strip() or e.__class__.__name__
                        if "playlist or channel" in msg:
                            msg = "This link is a playlist or channel. Give one video link at a time."
                        job["error"] = "Setup is incomplete; run `captionless doctor` in a terminal." \
                            if isinstance(e, SystemExit) else msg
                    finally:
                        _LOG_SINK = None
                        job["done"].set()

            threading.Thread(target=work, daemon=True).start()
    deadline, n = time.time() + float(setting("CAPTIONLESS_MCP_WAIT") or 50), 0
    while not job["done"].wait(5) and time.time() < deadline:
        n += 1
        progress(n, job["last"])
    if not job["done"].is_set():
        return (f"Still working ({job['last']}). The transcription continues in the background. Wait about 30 "
                "seconds, then call transcribe again with the same arguments to collect the result.")
    with _MCP_LOCK:
        _MCP_JOBS.pop(key, None)
    if job["error"]:
        raise UserError(job["error"])
    return job["text"]


def mcp_call(name, a, progress):
    out_dir = default_out()
    if name == "transcribe":
        return mcp_transcribe(a, progress)
    if name == "read_transcript":
        return transcript_part(find_video(out_dir, a.get("video")), a.get("offset") or 0)
    if name == "search_transcripts":
        if not str(a.get("query") or "").strip():
            raise UserError("`query` is needed: the words to find.")
        return search_library(out_dir, str(a["query"]), a.get("channel"), int(a.get("limit") or 10))
    if name == "list_transcripts":
        found = [t for t in out_dir.glob("*/*/transcript.txt")
                 if not a.get("channel") or normalize(str(a["channel"])) in normalize(t.parent.parent.name)]
        found.sort(key=lambda t: t.stat().st_mtime, reverse=True)
        limit = int(a.get("limit") or 30)
        lines = [f"{len(found)} transcript(s) in {out_dir}" + (f", newest {limit} shown" if len(found) > limit else "")]
        for t in found[:limit]:
            meta = read_header(t)
            m = re.search(r"\[([^\[\]]+)\]$", t.parent.name)
            lines.append(f"- {meta.get('title') or t.parent.name} | {meta.get('channel') or t.parent.parent.name} | "
                         f"{meta.get('duration', '?')} | saved {meta.get('created', '?')}"
                         + (f" | id: {m.group(1)}" if m else f" | folder: {t.parent}"))
        return "\n".join(lines)
    if name == "save_note":
        folder, fname = find_video(out_dir, a.get("video")), str(a.get("filename") or "").strip()
        if not re.fullmatch(r"\w[\w .\-]{0,80}\.(md|txt|srt|vtt)", fname) or \
                fname.lower() in ("transcript.txt", "subtitles.srt"):
            raise UserError("`filename` must be a plain name ending in .md, .txt, .srt or .vtt (for example "
                            "summary.md), and may not replace transcript.txt or subtitles.srt.")
        (folder / fname).write_text(str(a.get("content") or ""), encoding="utf-8")
        return f"Saved {folder / fname}"
    raise UserError(f"Unknown tool: {name}")


def cmd_mcp(argv):
    argparse.ArgumentParser(
        prog="captionless mcp",
        description="Run as an MCP server on stdin/stdout. An app such as Claude Desktop starts this command "
                    "itself; see the README for the few lines to add to its settings.").parse_args(argv)
    ensure_deps()
    # The protocol owns the two pipes this program was started with, through private copies of them.
    # Everything else that prints (this script, the downloader, child programs) is moved to stderr, where
    # the app keeps it as a log, and everything else that looks at stdin finds an empty one. That matters
    # on Windows: while this program waits for the next message, anything else touching the same pipe freezes.
    sys.stdout.flush()
    wire, inbox = os.fdopen(os.dup(1), "wb"), os.fdopen(os.dup(0), "rb")
    os.dup2(2, 1)
    nul = os.open(os.devnull, os.O_RDONLY)
    os.dup2(nul, 0)
    os.close(nul)
    sys.stdout = sys.stderr
    lock = threading.Lock()

    def send(obj):
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8", "replace")
        with lock:
            wire.write(data)
            wire.flush()

    def call_tool(mid, params):
        token = (params.get("_meta") or {}).get("progressToken")

        def progress(n, message):
            if token is not None:
                send({"jsonrpc": "2.0", "method": "notifications/progress",
                      "params": {"progressToken": token, "progress": n, "message": message}})

        try:
            text, failed = mcp_call(params.get("name"), params.get("arguments") or {}, progress), False
        except UserError as e:
            text, failed = str(e), True
        except Exception as e:  # noqa: BLE001 - a tool error is an answer, not a crash
            text, failed = f"{e.__class__.__name__}: {e}", True
        send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}],
                                                      "isError": failed}})

    def handle(msg):
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if method is None or mid is None:
            return   # replies and notifications need no answer
        if method == "initialize":
            asked = params.get("protocolVersion")
            result = {"protocolVersion": asked if asked in MCP_VERSIONS else MCP_VERSIONS[0],
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "captionless", "version": __version__},
                      "instructions": MCP_GUIDE}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": MCP_TOOLS}
        elif method == "tools/call":
            threading.Thread(target=call_tool, args=(mid, params), daemon=True).start()
            return
        else:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}})
            return
        send({"jsonrpc": "2.0", "id": mid, "result": result})

    log(f"captionless {__version__} MCP server ready; transcripts in {default_out()}")
    for raw in iter(inbox.readline, b""):
        if not raw.strip():
            continue
        try:
            msg = json.loads(raw.decode("utf-8"))
        except ValueError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            continue
        for m in msg if isinstance(msg, list) else [msg]:
            if isinstance(m, dict):
                handle(m)
    return 0


# --------------------------------------------------------------------------- entry point

COMMANDS = {"search": cmd_search, "fix": cmd_fix, "export": cmd_export, "watch": cmd_watch, "mcp": cmd_mcp,
            "doctor": cmd_doctor, "update": cmd_update}


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    argv = sys.argv[1:]
    if sys.version_info < (3, 11):   # the video downloader no longer supports older versions
        log(f"ERROR: captionless needs Python 3.11 or newer; this is Python {sys.version.split()[0]}.")
        return 1
    if argv and argv[0] in ("-V", "--version"):
        print(f"captionless {__version__}")
        return 0
    try:
        if argv and argv[0] in COMMANDS:
            return COMMANDS[argv[0]](argv[1:])
        return cmd_transcribe(argv)
    except UserError as e:
        log(f"ERROR: {e}")
        return 1
    except KeyboardInterrupt:
        log("\nInterrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
