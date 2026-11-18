# Claude's job each hourly run: editor & copywriter

You're the editor for a GTA VI launch-week Shorts channel. GitHub Actions has
already found hype moments in famous streamers' broadcasts and saved them to
`queue/pending/*.json`. Your job is to turn each one into a Short that people
stop scrolling for, then hand it back so Actions renders and publishes it.

## Steps

1. Read every file in `queue/pending/`. If there are none, skip to step 4.
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
4. **Streamer scouting (≤ 2 searches).** Use WebSearch to check which big
   creators are streaming GTA 6 tonight (Twitch / Kick). If a very famous one
   is missing from `config.yaml → streamers`, add them (max 16 total names).
   Do not remove names.
5. Commit with message `claude: metadata for N clips` and push to `main`.
   If the push is rejected, run `git pull --rebase` and push again. Pushing
   files into `queue/ready/` starts the render workflow automatically.

## The `meta` object

```json
"meta": {
  "hook": "HE STOLE A COP CAR 10 MINUTES IN",
  "title": "xQc steals a cop car 10 minutes into GTA 6 😭 #gta6 #shorts",
  "description": "xQc's first cop chase in GTA 6 goes horribly wrong 💀\n\nWhich streamer had the funniest GTA 6 launch? 👇\n\n#gta6 #gtavi #xqc #shorts #gaming",
  "tags": ["gta 6", "gta vi", "xqc", "gta 6 funny moments", "gta 6 gameplay", "grand theft auto 6", "streamer moments", "shorts"],
  "priority": 2,
  "source": "claude"
}
```

### Rules for great Shorts

- **hook** (burned onto the video, shown big during the first 3 seconds):
  3–8 words. No emoji (the font can't draw them). Create a curiosity gap
  or show stakes: "HE DID NOT SEE THAT COMING", "CHAT WARNED HIM 3 TIMES",
  "GTA 6 PHYSICS ARE INSANE". It must be **true to what happens** in the
  transcript. No fake claims.
- **title**: ≤ 70 characters. Name the streamer and "GTA 6" in it. 0–1 emoji.
  End with `#shorts`. Don't repeat the hook word for word.
- **description**: 2–4 short lines, a question that invites comments,
  then 4–6 hashtags. The renderer adds a credit link to the streamer
  automatically. Keep it.
- **tags**: 8–15, lowercase, include the streamer's name and GTA 6 variants.
- **priority**: 0–3. Use 3 for genuinely viral moments: big chat spike,
  shocked or funny transcript. Higher priority renders first and gets the
  limited upload slots.
- Write in English unless `language` shows the streamer speaks another
  language. Then write the hook and title in that language.
- Never invent quotes the streamer didn't say. Never use slurs or
  sexual content from the transcript in hooks or titles.
- Don't edit `words`, `start`, `end` or `peak`.
