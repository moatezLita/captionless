# captionless

Transcribe any YouTube video, even one with no captions, from inside Claude Code. Paste a link and get the full text with timestamps, subtitles, and a summary, saved in a tidy folder.

Most "YouTube transcript" tools only fetch captions that already exist, so they fail on the many videos that have none. captionless listens to the audio instead, using a speech-to-text service of your choice. With a free Groq key, an hour-long video takes about two minutes and costs nothing.

```
> Transcribe this and give me the key points: https://www.youtube.com/watch?v=...

  Transcribed 58 minutes in 1 min 54 s. Saved to YouTube Transcripts/Channel/Title [id]/
  - [02:10] The speaker opens with ...
  - [14:35] ...
```

It works in any language Whisper understands (about 100), and also on playlists, other video sites, and audio or video files on your computer.

## Install

You need [Claude Code](https://claude.com/claude-code), Python 3.9 or newer, and either [Deno](https://deno.com) or Node.js 20+ (YouTube downloads require a JavaScript runtime).

1. Add the plugin, inside Claude Code:
   ```
   /plugin marketplace add moatezLita/captionless
   /plugin install captionless@captionless
   ```
2. Get a speech-to-text key. [Groq](https://console.groq.com/keys) is free and asks for no card.
3. Save the key where the tool can find it. Either set an environment variable:
   ```
   # macOS / Linux (add to ~/.zshrc or ~/.bashrc to keep it)
   export GROQ_API_KEY="your-key"

   # Windows (then open a new terminal)
   setx GROQ_API_KEY "your-key"
   ```
   or put it in the file `~/.captionless/config.env`:
   ```
   GROQ_API_KEY=your-key
   ```

That's all. The first transcription sets up a private Python environment in `~/.captionless` (about a minute); nothing is installed globally.

## Use

Ask in plain words, or call the skill directly with `/captionless:transcribe <link>`.

| You say | What happens |
|---|---|
| "Transcribe https://youtu.be/..." | Transcript, subtitles and a summary are saved, and you get the short version in chat |
| "Summarize this lecture as study notes: <link>" | Notes written from the transcript, with clickable timestamps |
| "Transcribe this whole playlist: <link>" | Every video is done in turn; ones already finished are skipped |
| "Make French subtitles for <link>" | A translated `subtitles.fr.srt`, ready to upload |
| "Where did she talk about pricing?" | Searches everything you have transcribed and answers with links to the exact moments |
| "It keeps writing 'Jon' instead of 'John'" | The fix is remembered for that channel and applied to old and new transcripts |

To transcribe on a bare link with no other words, add this line to your `CLAUDE.md`:

```
When my message is only a video link, transcribe it with the captionless skill and give me a short summary.
```

## What you get

```
YouTube Transcripts/
  Channel name/
    corrections.txt            optional: your spelling fixes for this channel
    Video title [video-id]/
      transcript.txt           full text in paragraphs, with [mm:ss] timestamps
      subtitles.srt            subtitles, timed to the audio
      summary.md               written by Claude, plus any notes or translations you ask for
```

The folder is created on your Desktop, or in your home folder if you have no Desktop. Set `CAPTIONLESS_OUT` to put it somewhere else.

## Features

- **No captions needed.** The audio is transcribed with Whisper large-v3 or the model you choose.
- **Captions first, when they are good.** If the uploader wrote captions in the spoken language, those are used instantly and no quota is spent. Automatic captions are ignored by default because AI transcription is usually more accurate.
- **Long videos.** The audio is cut at quiet moments into ten-minute pieces, sent in parallel, and stitched back together with correct timestamps.
- **Playlists and channels** with skip-what's-done, so the same command can be repeated until a long series is finished.
- **Resumes after a rate limit.** Finished pieces are kept, so a run that hits a free-tier limit continues later instead of starting over.
- **Search across everything you have transcribed,** tolerant of case, accents and Arabic spelling variants, with links that open the video at the right second.
- **Per-channel corrections** for names and terms that speech recognition keeps getting wrong.
- **English translation** straight from the audio (`--translate`), and subtitles in any other language through Claude.

## Providers

Use whichever service you have a key for. If several keys are set, the first in this table wins; choose another with `--provider`.

| Provider | Setting | Default model | Cost | Status |
|---|---|---|---|---|
| Groq | `GROQ_API_KEY` | `whisper-large-v3` | Free tier, about 8 hours of audio a day at the time of writing | Tested |
| OpenAI | `OPENAI_API_KEY` | `whisper-1` | Paid | Written to the API docs, not yet tested |
| Google Gemini | `GEMINI_API_KEY` | `gemini-flash-latest` | Free tier | Written to the API docs, not yet tested |
| Any OpenAI-compatible server | `CAPTIONLESS_BASE_URL`, plus `CAPTIONLESS_API_KEY` and `CAPTIONLESS_MODEL` if needed | `whisper-1` | Yours | Not yet tested |
| Local Whisper | none: `--provider local` | `large-v3-turbo` | Free and offline | Tested on CPU |

Notes:

- **Local** runs [faster-whisper](https://github.com/SYSTRAN/faster-whisper) on your machine. It is private and unlimited, but slower, and it downloads the model (about 1.6 GB) on first use. An NVIDIA GPU is used when the CUDA libraries are installed; otherwise it falls back to the CPU.
- **Gemini** estimates timestamps itself, so they are approximate.
- **OpenAI's** newer `gpt-4o-transcribe` models return no timestamps, so each ten-minute piece gets a single one. `whisper-1` gives full timestamps.

If you try one of the untested providers, please [open an issue](https://github.com/moatezLita/captionless/issues) with what happened, working or not.

## Use it without Claude Code

The tool is a single Python script with no setup step:

```
git clone https://github.com/moatezLita/captionless
python3 captionless/skills/transcribe/scripts/captionless.py "https://www.youtube.com/watch?v=..."
```

```
captionless.py <links or files> [options]    transcribe
captionless.py search "words"                search saved transcripts
captionless.py fix                           re-apply corrections.txt to saved transcripts
captionless.py doctor                        check keys, JavaScript runtime and folders
captionless.py update                        upgrade the downloader
```

Common options: `--lang es`, `--translate`, `--playlist --from 10 --to 20`, `--limit 5`, `--provider local`, `--model <name>`, `--captions any|never`, `--force`, `-o <folder>`. Run with `--help` for the full list.

To use it as a plain skill in another agent, copy the `skills/transcribe` folder into that agent's skills directory.

## Settings

Set these as environment variables or as lines in `~/.captionless/config.env`.

| Setting | Meaning |
|---|---|
| `GROQ_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY` | Provider keys |
| `CAPTIONLESS_PROVIDER` | Default provider: `groq`, `openai`, `gemini`, `custom` or `local` |
| `CAPTIONLESS_MODEL` | Default model name |
| `CAPTIONLESS_OUT` | Folder for transcripts |
| `CAPTIONLESS_BASE_URL`, `CAPTIONLESS_API_KEY` | An OpenAI-compatible server, for example `http://localhost:8000/v1` |

## Troubleshooting

- **Download errors.** Video sites change often. Run `captionless.py update` (or ask Claude to "update captionless") and try again.
- **"No JavaScript runtime was found."** Install Deno: `winget install DenoLand.Deno`, `brew install deno`, or `curl -fsSL https://deno.land/install.sh | sh`.
- **"Sign in to confirm you're not a bot", or age-restricted videos.** Add `--cookies-from-browser chrome` (or `edge`, `firefox`, `safari`).
- **Rate limit reached.** The message says how long the wait is. Run the same command again later and it continues where it stopped.
- **The text is nonsense or repeated letters.** Run again with `--force`. If you used `--prompt`, remove it: a hint that doesn't fit can derail Whisper.
- **Local mode ignores the GPU on Windows.** Install the CUDA libraries into the tool's environment: `~/.captionless/venv/Scripts/python -m pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12>=9,<10"`.

## Accuracy, privacy and fair use

Speech recognition is very good on clear speech and still makes mistakes, mostly in names, rare words and quotations. Check anything you plan to publish, especially scripture, legal or medical wording, and exact quotes.

With a cloud provider, the audio of each video is sent to that provider under its own terms; some free tiers may use what you send to improve their products. Your keys stay on your machine and are sent only to the provider they belong to. Use `--provider local` to keep everything on your computer.

captionless is meant for personal use: study, notes, accessibility, and working with videos you have the right to use. Downloading may be against a site's terms of service, and transcripts of other people's videos remain their work. Respect creators and copyright.

## Status

Version 0.1, developed and tested on Windows with Groq. It is written to run on macOS and Linux too, but those have not been tested yet. Reports and pull requests are welcome.

## License

[MIT](LICENSE)
