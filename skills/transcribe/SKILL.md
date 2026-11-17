---
name: transcribe
description: Transcribe a YouTube video (or any online video, playlist, or local audio/video file) to text, even when it has no captions, then summarize it, answer questions about it, translate it, or make subtitles. Also searches the user's saved transcripts ("where did they talk about X?"). Use when the user shares a video link and wants its transcript, a summary, notes, quotes, or subtitles, or asks about videos they transcribed before.
argument-hint: <video link, playlist link, or file> [what you want: summary, notes, translate to French, ...]
---

# captionless

You can't listen to audio, so this skill gives you a tool that turns a video into text you can read. It downloads the audio, sends it to a speech-to-text service (Groq, OpenAI, Gemini, a custom server, or a local Whisper model), and saves the result in the user's transcripts folder. Everything after that — summaries, answers, translations — is your work, done from the saved text.

The tool is one script. Run it with `python3` (on Windows, `python`):

```
python3 "${CLAUDE_SKILL_DIR}/scripts/captionless.py" <command or links> [options]
```

Always put links in double quotes: they contain `&`, which shells treat specially. The first run sets itself up (about a minute) and needs no action from you.

Request: `$ARGUMENTS`

## Transcribe a video

1. Run the script with the link or file path. Give it a 10-minute timeout; an hour of audio usually takes 1–3 minutes with a cloud provider. Several links can go in one run.
   - The spoken language is detected. Add `--lang <code>` (en, es, ar, ja, ...) only if the user names the language or the result came out in the wrong one.
   - For an English transcript of a video in another language, add `--translate`.
   - Don't add `--prompt` unless the user asks for it. A hint that doesn't fit can make Whisper produce garbage.
   - A video that already has a transcript is skipped. Add `--force` to redo it.
2. The script prints these lines when a video is done:
   ```
   FOLDER:     <transcripts folder>/<Channel>/<Title> [id]
   TRANSCRIPT: .../transcript.txt     full text with [mm:ss] timestamps
   SUBTITLES:  .../subtitles.srt
   ENGINE:     what produced the text
   ```
   An `ALSO:` line names each extra file that was written (see "Other formats"). A `WARNING:` line means the result is suspect. Read the transcript before trusting it, and if it is mostly repeated letters or syllables, run again with `--force` rather than summarizing nonsense.
3. Read `transcript.txt` (in parts, for long ones), then do what the user asked.

When the video has chapters, they appear in `transcript.txt` as `## Chapter title` lines above the paragraphs they cover. Use them as the outline of your summary.

If the video has captions written by its uploader, the script uses those and skips the AI step; `ENGINE` says so. `--captions any` also accepts automatic captions (instant, but less accurate), and `--captions never` always transcribes.

## What to deliver

Save what you write in the video's folder, next to the transcript, so the user's library stays in one place. Write in the language the user is writing to you in, unless they ask for another.

- **No specific request:** write `summary.md` — a two- or three-sentence overview, a table of topics with timestamps, and the key takeaways — then give a short version in your reply along with the folder path.
- **A specific request** (notes, quotes, an article, action items, a translation): produce that from the transcript, save it as a fitting file such as `notes.md`, and show it in your reply.
- **A clean reading version** ("make it readable", "add punctuation", "fix the paragraphs"): write `reading.md` in the video's folder. Keep the speaker's words and their order exactly; add only punctuation, paragraph breaks, and the chapter headings, and keep one `[mm:ss]` timestamp at the start of each paragraph. Don't summarize, translate, or drop repetitions. Where a word is clearly a recognition error, correct it and list those corrections at the end of the file so the user can check them. Work through a long transcript in parts so nothing is skipped. This matters most for languages that Whisper writes without punctuation, such as Arabic.
- **The transcript itself:** paste it if they ask for the text. Otherwise point to the file; an hour of speech is too long to read in a chat.

Make timestamps clickable when the source is YouTube: `[12:35](https://www.youtube.com/watch?v=VIDEO_ID&t=755s)`, with the time in seconds.

Speech recognition makes mistakes, mostly in names, rare words, and quotations. Fix obvious slips when you quote, and tell the user when something matters and you can't be sure of it. This applies most to anything they might publish: scripture, legal or medical wording, and exact quotes should be checked against a reliable source.

## Playlists and channels

Add `--playlist` to transcribe every video behind a playlist or channel link. Without it, a link that contains both a video and a playlist transcribes only that video.

- `--from N --to M` selects positions in the playlist, and `--limit N` stops after N new transcripts.
- Finished videos are skipped, so the same command can be run again and again until the list is done.
- Exit code 2 with a `STOPPED:` line means the provider's rate limit was reached. Nothing is lost: tell the user how long the wait is (the message says) and that running the same command later continues where it stopped. `--max-wait <seconds>` makes the script sit out longer pauses on its own.
- Long playlists take a while. Run them in the background and tell the user what you started, rather than writing a summary for every video unless they ask for that.

## Follow a channel or playlist

When the user wants new videos transcribed as they come out ("watch this channel", "keep up with this playlist"):

```
captionless.py watch add "<channel or playlist link>"     start watching; videos already there are left alone
captionless.py watch run                                  transcribe what appeared since the last check
captionless.py watch list
captionless.py watch remove <number or link>
```

- `watch add` transcribes nothing. To also get older videos, use `--playlist` with `--limit` or `--from/--to` as above.
- `watch run` prints one `NEW: <count> in <name>` line per source, then the usual lines for each video it transcribes. It accepts the transcribe options (`--lang`, `--limit`, `--formats`, ...). Run it in the background when several videos are new, and tell the user what came in.
- A channel link looks at the channel's newest 50 uploads on each check; a playlist link looks at the whole list.
- It runs only when someone runs it. If the user wants it to happen by itself, offer to schedule `watch run` (cron on macOS and Linux, Task Scheduler on Windows); the README has the commands.

## Other formats

`transcript.txt` and `subtitles.srt` are always written. Two more are optional:

- `transcript.md`: Markdown with chapter headings and timestamps that link to the moment in the video, for note apps such as Obsidian and Notion.
- `subtitles.vtt`: WebVTT, the subtitle format web players use.

Add `--formats md`, `--formats vtt` or `--formats md,vtt` when transcribing. For videos that are already saved, `captionless.py export [folder] --formats md,vtt` makes the files without transcribing again. If the user always wants them, suggest the line `CAPTIONLESS_FORMATS=md,vtt` in `~/.captionless/config.env`.

## Translated subtitles

`--translate` gives English text and English `subtitles.srt` straight from the audio. For any other language, translate the subtitles yourself:

1. Read `subtitles.srt`.
2. Write `subtitles.<lang>.srt` (for example `subtitles.fr.srt`) in the same folder, with the same cue numbers and the same time lines, changing only the text. Work through long files in parts of about 100 cues so none are skipped.
3. Check that both files have the same number of cues before telling the user it is ready.

## Search the saved transcripts

When the user asks where something was said, or asks a question their saved videos could answer:

```
python3 "${CLAUDE_SKILL_DIR}/scripts/captionless.py" search "words to find" [--channel "name"] [-n 20]
```

All the words must appear close together. The search ignores case, accents, and Arabic spelling variants, but it only matches words, not meaning. So for a question such as "where did he talk about patience?", run a few searches with different likely wordings, in the language of the videos. Each result gives the video, a timestamp link, and the passage. Open `transcript.txt` around a result when you need more context, then answer with the links so the user can jump to the moment.

## Corrections for recurring mistakes

When the same name or term keeps coming out wrong for a channel, or the user corrects a spelling, record it in `corrections.txt` inside that channel's folder, one rule per line:

```
# wrong => right   (whole words, exact match)
Jon Doe => John Doe
```

A `corrections.txt` in the top transcripts folder applies to every channel. New transcripts pick the rules up automatically. Run `captionless.py fix` to apply them to transcripts that are already saved; it also updates `transcript.md` and `subtitles.vtt` where those exist.

## Setup and problems

- **No API key yet:** the script prints the choices and where to get each key. Pass that on to the user and let them choose; if they ask for a free option, Groq and Gemini have free tiers and `--provider local` needs no key. Ask them to set the variable themselves, in their environment or in `~/.captionless/config.env`; a key pasted into the chat ends up in the conversation history, so don't ask for it there.
- **Check the setup:** `captionless.py doctor` shows which providers have keys, whether a JavaScript runtime was found (YouTube downloads need Deno or Node 20+), and where transcripts are saved.
- **"captionless needs Python 3.11 or newer":** the user has to install a newer Python; the script cannot work around it.
- **Download errors that mention the site, formats, or signatures:** run `captionless.py update`, then try again. Video sites change often and the downloader has to keep up.
- **"Sign in to confirm you're not a bot", or age-restricted videos:** add `--cookies-from-browser chrome` (or `edge`, `firefox`, `safari`).
- **Another provider or model:** `--provider groq|openai|gemini|custom|local` and `--model <name>`. `--provider local` runs offline with no key; it is slower and downloads a model the first time.
- **Another output folder:** `-o <folder>`, or `CAPTIONLESS_OUT` in the environment or config file.
