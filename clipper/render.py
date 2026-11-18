"""Render queued moments into vertical 30-60s Shorts with a hook + captions,
then publish them (GitHub Release for phone posting, optional YouTube API).

A clip is rendered when:
  * Claude has written its metadata (queue/ready/*.json), or
  * it has waited in queue/pending longer than publish.fallback_after_minutes
    (then a template hook/title is used so nothing is lost).
"""
import json
import os
import re
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import (QUEUE, ROOT, STATE, campaign_active, load_config, load_json, log,  # noqa: E402
                    night_key, run, save_json, ytdlp_base)

W, H = 1080, 1920


# ── metadata ────────────────────────────────────────────────────────────────

def fallback_meta(c):
    name = c.get("streamer_name") or c["streamer"]
    return {
        "hook": f"{name.upper()} WAS NOT READY FOR THIS",
        "title": f"{name} loses it in GTA 6 😭 #gta6 #shorts",
        "description": f"{name} reacts live in GTA 6 on launch week.\n\n#gta6 #gtavi #shorts #{c['streamer']}",
        "tags": ["gta 6", "gta vi", "gta 6 funny moments", "grand theft auto 6", c["streamer"], name, "shorts"],
        "source": "template",
    }


def finalize_meta(c, meta):
    name = c.get("streamer_name") or c["streamer"]
    m = dict(meta)
    m["hook"] = re.sub(r"[^\w\s'!?.,:%&-]", "", m.get("hook") or "").strip()[:70] or fallback_meta(c)["hook"]
    m["title"] = (m.get("title") or fallback_meta(c)["title"]).strip()[:100]
    credit = f"🎥 Clip from {name}'s live stream: {c['channel_url']}"
    desc = (m.get("description") or "").strip()
    if c["channel_url"] not in desc:
        desc = f"{credit}\n\n{desc}"
    m["description"] = desc[:4900]
    tags, total = [], 0
    for t in m.get("tags") or []:
        t = str(t).strip().replace("<", "").replace(">", "")
        if t and total + len(t) + 3 < 480:
            tags.append(t)
            total += len(t) + 3
    m["tags"] = tags
    return m


# ── subtitles (ASS) ─────────────────────────────────────────────────────────

def ass_time(t):
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def esc(text):
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def build_ass(words, hook, credit, teaser, total, cap_margin):
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hook,Anton,104,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,1,0,0,0,100,100,1,0,1,7,3,8,70,70,190,1
Style: Top,Anton,64,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,1,0,0,0,100,100,1,0,1,5,2,8,80,80,210,1
Style: Cap,Anton,88,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,1,0,0,0,100,100,1,0,1,6,2,2,70,70,{cap_margin},1
Style: Credit,Anton,40,&H00E6E6E6,&H00FFFFFF,&H00000000,&H96000000,1,0,0,0,100,100,1,0,1,3,1,2,40,40,95,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = []
    hook_t = esc(hook.upper())
    big_end = teaser + 1.6
    # Hook: big, yellow, pops in during the teaser
    ev.append(f"Dialogue: 2,{ass_time(0)},{ass_time(big_end)},Hook,,0,0,0,,"
              f"{{\\c&H00FFFF&\\fscx70\\fscy70\\t(0,180,\\fscx100\\fscy100)}}{hook_t}")
    # Then stays small at the top for the rest of the video
    ev.append(f"Dialogue: 1,{ass_time(big_end)},{ass_time(total)},Top,,0,0,0,,{{\\fad(150,0)}}{hook_t}")
    ev.append(f"Dialogue: 1,{ass_time(0)},{ass_time(total)},Credit,,0,0,0,,{esc(credit)}")

    # Captions: chunks of up to 3 words, active word highlighted yellow
    chunks, cur = [], []
    for w in words:
        if cur and (len(cur) >= 3 or w[0] - cur[-1][1] > 0.5 or re.search(r"[.!?]$", cur[-1][2])):
            chunks.append(cur)
            cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    for ci, ch in enumerate(chunks):
        nxt = chunks[ci + 1][0][0] if ci + 1 < len(chunks) else ch[-1][1] + 0.4
        ch_end = min(nxt, ch[-1][1] + 0.4)
        clean = [re.sub(r"[^\w'?!%$-]", "", w[2]).upper() for w in ch]
        for k, w in enumerate(ch):
            s = ch[0][0] if k == 0 else w[0]
            e = ch[k + 1][0] if k + 1 < len(ch) else ch_end
            if e <= s:
                continue
            parts = [(f"{{\\c&H00FFFF&}}{t}{{\\c&HFFFFFF&}}" if j == k else t) for j, t in enumerate(clean) if t]
            pop = "{\\fscx112\\fscy112\\t(0,90,\\fscx100\\fscy100)}" if k == 0 else ""
            ev.append(f"Dialogue: 0,{ass_time(s + teaser)},{ass_time(e + teaser)},Cap,,0,0,0,,{pop}{' '.join(parts)}")
    return head + "\n".join(ev) + "\n"


# ── video ───────────────────────────────────────────────────────────────────

def download_segment(c, workdir):
    a = max(0.0, c["start"] - 1.0)
    b = c["end"] + 1.0
    out = workdir / "src.%(ext)s"
    run([*ytdlp_base(), "-f", "b[height<=1080][height>=480]/bv*[height<=1080]+ba/b",
         "--download-sections", f"*{a:.2f}-{b:.2f}", "--force-keyframes-at-cuts",
         "--merge-output-format", "mp4", "-o", str(out), c["vod_url"]], timeout=1200)
    files = [p for p in workdir.glob("src.*") if not p.name.endswith(".part")]
    if not files:
        raise RuntimeError("segment download produced no file")
    return files[0], a


def render(c, meta, cfg, src, src_offset, out_path, workdir):
    k = cfg["clip"]
    teaser = float(k["teaser_seconds"])
    main_start = c["start"] - src_offset
    main_dur = c["end"] - c["start"]
    tz = max(0.0, c["peak"] - src_offset - 0.8)  # flash the moment itself first
    if tz + teaser > main_start + main_dur:
        tz = max(0.0, main_start + main_dur - teaser)
    total = teaser + main_dur

    fg_h = int(round(W * 9 / 16 * float(k["zoom"]) / 2) * 2)
    fg_top = (H - fg_h) // 2
    cap_margin = max(150, H - (fg_top + fg_h) - 230)
    credit = c["channel_url"].replace("https://", "").replace("www.", "")
    ass = workdir / "subs.ass"
    ass.write_text(build_ass(c["words"], meta["hook"], credit, teaser, total, cap_margin), encoding="utf-8")

    fonts = ROOT / "fonts"
    norm = "fps=30,scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1"
    fc = (
        f"[0:v]{norm},setpts=PTS-STARTPTS[tv];[0:a]asetpts=PTS-STARTPTS[ta];"
        f"[1:v]{norm},setpts=PTS-STARTPTS[mv];[1:a]asetpts=PTS-STARTPTS[ma];"
        f"[tv][ta][mv][ma]concat=n=2:v=1:a=1[cv][ca];"
        f"[cv]split[a][b];"
        f"[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,gblur=sigma=8,"
        f"scale={W}:{H},eq=brightness=-0.12[bg];"
        f"[b]scale=-2:{fg_h},crop=min(iw\\,{W}):{fg_h}[fg];"
        f"[bg][fg]overlay=(W-w)/2:{fg_top},ass={ass}:fontsdir={fonts},format=yuv420p[v];"
        f"[ca]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[aout]"
    )
    # two separate inputs (teaser + main) so ffmpeg never buffers minutes of raw frames
    run(["ffmpeg", "-v", "error", "-y",
         "-ss", f"{tz:.3f}", "-t", f"{teaser:.3f}", "-i", str(src),
         "-ss", f"{main_start:.3f}", "-t", f"{main_dur:.3f}", "-i", str(src),
         "-filter_complex", fc,
         "-map", "[v]", "-map", "[aout]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
         "-t", f"{total:.3f}", str(out_path)], timeout=1800)
    return total


# ── publishing ──────────────────────────────────────────────────────────────

def gh(*args, check=True):
    return run(["gh", *args], timeout=600, check=check)


def publish_release(night, items, files):
    tag = f"night-{night}"
    notes = [f"# GTA VI clips: night of {night}\n",
             "Download an MP4, then post it from the YouTube app (**Create → Short → upload**). "
             "Copy the title/description below. Long-press a block to copy it.\n"]
    for i, it in enumerate(items, 1):
        m = it["meta"]
        notes += [
            f"\n---\n### {i}. {m['title']}",
            f"**File:** `{it['id']}.mp4` · {it.get('duration_s', '?')}s · from [{it['streamer_name']}]({it['channel_url']})"
            + (f" · YouTube: https://youtu.be/{it['youtube_id']}" if it.get("youtube_id") else ""),
            "\n**Title**\n```\n" + m["title"] + "\n```",
            "**Description**\n```\n" + m["description"] + "\n```",
            "**Tags**\n```\n" + ", ".join(m["tags"]) + "\n```",
        ]
    nf = Path(tempfile.mkstemp(suffix=".md")[1])
    nf.write_text("\n".join(notes), encoding="utf-8")
    exists = subprocess_ok(["gh", "release", "view", tag])
    if not exists:
        gh("release", "create", tag, "--title", f"GTA VI clips · {night}", "--notes-file", str(nf))
    else:
        gh("release", "edit", tag, "--notes-file", str(nf))
    if files:
        gh("release", "upload", tag, *map(str, files), "--clobber")
    return tag


def subprocess_ok(cmd):
    import subprocess
    return subprocess.run(cmd, capture_output=True).returncode == 0


# ── main ────────────────────────────────────────────────────────────────────

def due_items(cfg):
    items = []
    for p in sorted((QUEUE / "ready").glob("*.json")):
        items.append((p, json.loads(p.read_text())))
    wait = 0 if os.environ.get("RENDER_NOW") == "1" else cfg["publish"]["fallback_after_minutes"] * 60
    for p in sorted((QUEUE / "pending").glob("*.json")):
        c = json.loads(p.read_text())
        if time.time() - c.get("created_unix", 0) >= wait:
            c["meta"] = fallback_meta(c)
            items.append((p, c))
    items.sort(key=lambda x: (x[1].get("meta") or {}).get("priority", 0) * 100 + x[1].get("score", 0),
               reverse=True)
    return items


def main():
    cfg = load_config()
    if not campaign_active(cfg, grace_hours=1) and os.environ.get("GITHUB_EVENT_NAME") != "push":
        # pushes from Claude always get rendered, even slightly after 7 AM
        return
    items = due_items(cfg)
    if not items:
        log("Nothing to render.")
        return
    night = night_key()
    night_file = STATE / "nights" / f"{night}.json"
    night_items = load_json(night_file, [])
    uploads = sum(1 for x in night_items if x.get("youtube_id"))
    new_files = []
    pub = cfg["publish"]
    for path, c in items:
        if c.get("status") == "rejected":
            shutil.move(str(path), QUEUE / "failed" / path.name)
            continue
        workdir = Path(tempfile.mkdtemp(prefix="r_"))
        try:
            meta = finalize_meta(c, c.get("meta") or fallback_meta(c))
            log(f"Rendering {c['id']}  ->  {meta['title']}")
            src, off = download_segment(c, workdir)
            out_dir = ROOT / "out"
            out_dir.mkdir(exist_ok=True)
            mp4 = out_dir / f"{c['id']}.mp4"
            dur = render(c, meta, cfg, src, off, mp4, workdir)
            txt = out_dir / f"{c['id']}.txt"
            txt.write_text(f"TITLE:\n{meta['title']}\n\nDESCRIPTION:\n{meta['description']}\n\nTAGS:\n"
                           f"{', '.join(meta['tags'])}\n", encoding="utf-8")
            c.update(meta=meta, status="done", duration_s=round(dur, 1), rendered_unix=int(time.time()))
            if pub.get("youtube_api_upload") and uploads < pub["max_youtube_uploads_per_night"]:
                try:
                    from youtube_upload import upload
                    c["youtube_id"] = upload(mp4, meta, pub["youtube_privacy"])
                    uploads += 1
                    log(f"  uploaded: https://youtu.be/{c['youtube_id']}")
                except Exception as ex:
                    log(f"  YouTube upload failed: {str(ex)[-300:]}")
            new_files += [mp4, txt]
            night_items.append({k: c[k] for k in ("id", "streamer", "streamer_name", "channel_url",
                                                  "meta", "duration_s") if k in c}
                               | ({"youtube_id": c["youtube_id"]} if c.get("youtube_id") else {}))
            save_json(QUEUE / "done" / path.name, {k: v for k, v in c.items() if k != "words"})
            path.unlink()
        except Exception:
            log(f"  failed: {traceback.format_exc()[-1200:]}")
            c["attempts"] = c.get("attempts", 0) + 1
            if c["attempts"] >= 2:
                save_json(QUEUE / "failed" / path.name, c)
                path.unlink()
            else:
                save_json(path, c)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
    save_json(night_file, night_items)
    if pub.get("github_release") and new_files:
        tag = publish_release(night, night_items, new_files)
        log(f"Published {len(new_files) // 2} clips to release {tag}")


if __name__ == "__main__":
    main()
