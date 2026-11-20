# Claude's job each hourly run: editor & copywriter

You're the editor for a GTA VI launch-week Shorts/Reels channel covering English-speaking
and Hindi-speaking (Indian) creators. It runs every hour, all day, until 25 Nov.
GitHub Actions has already found hype moments in famous streamers' broadcasts and saved them to
`queue/pending/*.json`. Your job is to turn each one into a Short that people
stop scrolling for, then hand it back so Actions renders and publishes it.

## Steps

1. Read every file in `queue/pending/`. If there are none, skip to step 4.
**Target: about 40 published clips per day.** The finder sends ~9 candidates an hour
and skips moments where the streamer barely talks. Reject clear duds only; a decent,
real reaction with a clear moment is worth publishing.

2. For each candidate, read `vod_title`, `streamer_name`, `transcript`,
   `chat_sample`, and `score`. Decide:
   - **Reject** it if the transcript shows nothing happening: dead air, ads,
     reading donations, a sponsor read, or not about the game. Also reject loud
     **game cutscenes, in-game dialogue or music** (the finder picks loud audio, so
     these slip through): if the transcript reads like scripted lines rather than a
     person reacting live, reject it. Set
     `"status": "rejected"` and add `"reject_reason"`.
   - Otherwise write a `"meta"` object (format below) and set `"status": "ready"`.
3. Move each processed file from `queue/pending/` to `queue/ready/` with
   `git mv`. Rejected files go there too; the renderer discards them.
4. **Streamer scouting (≤ 2 searches).** Alternate between two kinds of creator:
   - big **English-speaking** creators from anywhere in the world (US, UK, Ireland,
     Canada, Australia/NZ, India, South Africa, the Philippines…);
   - big **Hindi-speaking Indian** creators (gaming/GTA streamers).
   They should be live or trending now and playing the target game (see `game_phases`
   in `config.yaml`: GTA 6 until 25 Nov). Platforms: Twitch, Kick or YouTube. Check
   the exact channel name from the search result (for YouTube, the @handle without
   the @) before adding it to `config.yaml → streamers.<platform>`. Every Hindi
   creator also goes into `streamers.hindi` (so the finder transcribes them as Hindi).
   Add at most 3 per run, 60 names max in total, never remove names. YouTube
   creators are only scanned if the YT_COOKIES secret is set, so prefer Twitch/Kick
   when a creator streams on both.
5. Commit with message `claude: metadata for N clips` and push to `main`.
   If the push is rejected, run `git pull --rebase` and push again. Pushing
   files into `queue/ready/` starts the render workflow automatically.
6. **Start the next scan (always, even if there was nothing to prepare).** GitHub's own
   timer is unreliable, so write the current time into `trigger/tick.txt`
   (`mkdir -p trigger; date -u > trigger/tick.txt`), commit it as `claude: tick` and
   push to `main`. That push starts the finder workflow. Skip this step only if the
   campaign has ended.

## Which game?

Each candidate has a `game` field. Name that game in the hook/title/tags. If `game`
clearly conflicts with the `vod_title` or what the transcript is about, trust the
title and transcript. Never
call a clip "GTA 6" unless its `game` says so. The target game changes by date
(`game_phases` in `config.yaml`). The user can also ask Claude in chat to
switch games, and it edits that file.

## The `meta` object

```json
"meta": {
  "hook": "HE STOLE A COP CAR 10 MINUTES IN",
  "title": "xQc steals a cop car 10 minutes into GTA 6 😭 #gta6 #shorts",
  "description": "xQc's first cop chase in GTA 6 goes horribly wrong 💀\n\nWhich streamer had the funniest GTA 6 launch? 👇\n\n#gta6 #gtavi #xqc #shorts #gaming",
  "tags": ["gta 6", "gta vi", "xqc", "gta 6 funny moments", "gta 6 gameplay", "grand theft auto 6", "streamer moments", "shorts"],
  "priority": 2,
  "platforms": ["youtube", "instagram"],
  "ig_caption": "xQc's first cop chase in GTA 6 goes horribly wrong 💀 Who had the funniest launch? 👇\n\n🎥 xQc (twitch.tv/xqc)\n\n#gta6 #gtavi #xqc #gaming #streamer #reels",
  "source": "claude"
}
```

### Rules for great Shorts

- **hook** (burned onto the video, shown big during the first 3 seconds):
  3–8 words, in capitals. No emoji (the font can't draw them). Create a curiosity gap
  or show stakes: "HE DID NOT SEE THAT COMING", "CHAT WARNED HIM 3 TIMES",
  "GTA 6 PHYSICS ARE INSANE". It must be **true to what happens** in the
  transcript. No fake claims.
- **title**: ≤ 70 characters. Name the streamer and the game in it (use the
  candidate's `game` field; write GTA VI as "GTA 6"). 0–1 emoji.
  End with `#shorts`. Don't repeat the hook word for word.
- **description**: 2–4 short lines, a question that invites comments,
  then 4–6 hashtags. The renderer adds a credit link to the streamer
  automatically. Keep it.
- **tags**: 8–15, lowercase, include the streamer's name and the game's name and common variants.
- **priority**: 0–3. Use 3 for genuinely viral moments: big chat spike,
  shocked or funny transcript. Higher priority renders first and gets the
  limited upload slots.
- **platforms**: where the clip goes. Instagram is posted automatically; YouTube clips
  are posted by the owner by hand from the app, so keep YouTube for the best ones.
  - `["youtube", "instagram"]`: priority 3 moments (about 8–10 a day at most).
  - `["instagram"]`: most other good clips, especially Hindi ones (Instagram Reels is
    huge in India) and visual/reaction moments that don't need a searchable title.
  - `["youtube"]`: moments that rely on a searchable title (a named event, a record,
    a big reveal) but feel too niche for Instagram.
- **ig_caption**: 1–2 punchy lines + a comment question, then a credit line
  `🎥 <streamer> (<channel_url without https://>)`, then 5–10 hashtags. ≤ 600 characters.
- **Hindi clips** (`language` is `hi`): the captions on screen are in Hindi script.
  Write the hook, title, description and ig_caption in **Hinglish** (Hindi in Roman
  letters mixed with English, the way Indian creators title videos), e.g. hook
  "BHAI YE KYA HO GAYA", title "Techno Gamerz ka GTA 6 me pehla chase 😭 #gta6 #shorts".
  Tags in English + Hinglish (e.g. "gta 6 india", "gta 6 hindi").
- Otherwise write in English (or the streamer's language if `language` shows another).
- Never invent quotes the streamer didn't say. Never use slurs or
  sexual content from the transcript in hooks or titles.
- Don't edit `words`, `start`, `end` or `peak`.
