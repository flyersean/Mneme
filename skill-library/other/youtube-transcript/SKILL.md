---
name: youtube-transcript
description: Extract the transcript/captions/spoken text of a YouTube video or Short — "get the transcript of this video", "summarize this YouTube video", or any task gated on what was said in a YouTube URL. Try the captions API first; handle cloud-IP bot blocking.
license: MIT — adapted from https://github.com/tejask0/agent-skills (youtube-transcript), MIT.
---

# YouTube Transcript

Extracts a plain-text transcript from any YouTube video or Short. Prefer existing
captions (fast); fall back to audio + local transcription only when no captions exist.

## CRITICAL reality on cloud hosts (probe before promising)

YouTube bot-blocks requests from cloud-provider IPs. On the Mneme host (AWS) ALL
of the following fail with "Sign in to confirm you're not a bot" / RequestBlocked,
for every player_client variant tried (tv, android, ios, web_embedded) and for
the youtube-transcript-api package. So from this host, extraction only works with:

1. A cookies file — `--cookies cookies.txt` exported from a logged-in browser on
   another machine. youtube-transcript-api warns YouTube eventually PERMANENTLY
   BANS accounts used for this — use a throwaway account.
2. A proxy — `--proxy <url>` routed through a residential / non-cloud IP.
3. Delegation to a proxy/agent running on a non-cloud host.

Probe first (Step 0 below) with ONE call; if blocked, do NOT retry with other
clients — tell the user honestly which escape hatch is needed.

## Requirements

- `python3 -m yt_dlp` — pip installs into the instance tools/pylibs; the `yt-dlp`
  binary is NOT on PATH. Invoke as a python module, or add
  instances/<port>/tools/pylibs/bin to PATH.
- `ffmpeg` — only for the whisper fallback.
- `faster-whisper` — only if Step 3 runs (not installed on the host by default).

## Workflow

### Step 0 — Probe for bot blocking
    python3 -m yt_dlp --no-warnings --skip-download --print "%(title)s" "URL"

If it errors with "Sign in to confirm you're not a bot", jump to the cloud-host
reality section above — every client variant is also blocked.

### Step 1 — Resolve id and title
    python3 -m yt_dlp --no-warnings --print "%(id)s" --print "%(title)s" --skip-download "URL"

### Step 2 — Captions (manual, then auto)
Work in a scratch dir:
    python3 -m yt_dlp --no-warnings --skip-download --write-subs --write-auto-subs \
      --sub-langs "en.*,en" --sub-format vtt --convert-subs vtt -o "DIR/sub.%(ext)s" "URL"

If a `.vtt` appears, clean it to plain text: strip WEBVTT headers, timestamp cue
lines, bare cue numbers, inline <...> tags; de-duplicate consecutive identical
lines (auto-captions repeat lines across cues). The cleaned output is the transcript — stop here.

### Step 3 — Fallback: audio + local whisper
Only if no `.vtt` was produced:
    python3 -m yt_dlp -x --audio-format mp3 --audio-quality 0 -o "DIR/audio.%(ext)s" "URL"

Then faster-whisper (base model, device auto, int8, beam_size=5, vad_filter=True),
writing segments' text joined to the output .txt. Long videos take minutes on
CPU — run in the background and poll.

## Common Mistakes

- Assuming captions always exist — many videos (non-English, niche, creator-disabled) have none.
- Skipping `--sub-langs` — may grab non-English subs by default.
- Not de-duplicating caption lines — the transcript reads with a stutter.
- Forgetting ffmpeg before Step 3.
- Retrying bot-block errors with different player clients instead of getting
  cookies or a proxy — on cloud IPs every client is blocked, one probe suffices.
- Long whisper runs blocking the session — background them.
