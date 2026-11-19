# GTA VI Launch-Week Clipper 🎮✂️

A free, open-source pipeline that clips famous streamers' GTA VI moments into
30–60 second vertical Shorts with a hook and captions, every night from 1 AM
to 7 AM IST for a week (19–25 Nov 2026).

## How it works

| When (IST) | Who | What happens |
|---|---|---|
| 1:00, 2:00 … 6:00 | GitHub Actions `1-find-moments` | Scans the last ~70 min of each streamer's GTA VI broadcast (Twitch + Kick), finds chat and audio spikes, transcribes them (faster-whisper), and queues the best 5 |
| 1:35, 2:35 … 6:35 | Claude scheduled task | Reads the transcripts, rejects duds, writes the **hook, title, description, tags**, and scouts for more streamers |
| right after | GitHub Actions `2-render-and-publish` | Cuts each clip, flashes the best moment first (hook), adds big captions and a credit, and exports a 1080×1920 MP4 |
| :55 safety run | GitHub Actions | Renders anything Claude missed, using template text |

## Getting your clips (on your phone)

GitHub app → this repo → **Releases** → `GTA VI clips · <date>`
Each clip has an MP4 plus a ready-to-copy title, description and tags.
Post it from the YouTube app: **+ → Short → upload from device**.

> Auto-uploading via the YouTube API is built in (`publish.youtube_api_upload`),
> but Google locks every upload from an un-audited API project as **private
> permanently**. Keep it off unless your project passes the YouTube API audit.

## Switching games

`config.yaml → game_phases` sets which game is clipped on which dates. GTA VI
runs to 25 Nov, then any game the creators play (empty `keywords` = any game).
To switch to a specific game, edit its `keywords` (e.g. `["minecraft"]`) in the
GitHub app, or just tell Claude in chat: "clip Fortnite from tomorrow".

## Changing things

Edit `config.yaml` in the GitHub app (pencil icon):

- `streamers`: who to watch
- `finder.clips_per_run`: how many clips per hour
- `clip.zoom`: how tightly the gameplay is cropped
- `campaign`: the dates and hours

**Test it now:** Actions → `1-find-moments` → Run workflow → tick *force* → Run.
Then wait about 40 minutes, or run `2-render-and-publish` with *force* too.

## Stack (all free / open source)

yt-dlp · chat-downloader · faster-whisper · ffmpeg/libass · numpy ·
GitHub Actions · Anton font (SIL OFL, in `fonts/`)

## Be a good clipper

- Clip streamers who allow clip channels, and keep the credit link.
- The hook, captions and editing make each clip transformative, but
  Take-Two/Rockstar and the streamer can still claim or strike reuploads.
  If a creator asks you to stop, remove their name from `streamers`.


## Where the clips are (from 20 Nov)

Every clip is its own GitHub Release, which acts as a folder: `NN-streamer.mp4` (1080x1920, 60 fps) and `NN-streamer-post.txt` (title, description, tags), with the same text on the page ready to copy. Release titles start with the date and number, e.g. `20 Nov · #03 · xQc: ...`. Each page links to all clips from that night. Up to 25 clips are published per night.
