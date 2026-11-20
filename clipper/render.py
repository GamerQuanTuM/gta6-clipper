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
FACE_H = 640   # facecam panel height


# ── metadata ────────────────────────────────────────────────────────────────

def game_label(c):
    g = (c.get("game") or "").strip()
    if re.search(r"grand theft auto (vi|6)\b", g, re.I) or g.lower() in ("gta vi", "gta 6"):
        return "GTA 6"
    return g or "this game"


def fallback_meta(c):
    name = c.get("streamer_name") or c["streamer"]
    game = game_label(c)
    tag = re.sub(r"[^a-z0-9]", "", game.lower()) or "gaming"
    tags = [name, c["streamer"], "streamer moments", "gaming", "shorts"]
    if game != "this game":
        tags = [game.lower(), f"{game.lower()} funny moments"] + tags
    if game == "GTA 6":
        tags += ["gta vi", "grand theft auto 6"]
    return {
        "hook": f"{name.upper()} WAS NOT READY FOR THIS",
        "title": f"{name} loses it in {game} \U0001F62D #shorts"[:100],
        "description": f"{name} reacts live in {game}.\n\n#{tag} #shorts #gaming #{c['streamer']}",
        "tags": tags,
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


def build_ass(words, hook, credit, teaser, total, cap_margin, hook_mv=190, top_mv=210, font="Anton"):
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hook,{font},104,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,1,0,0,0,100,100,1,0,1,7,3,8,70,70,{hook_mv},1
Style: Top,{font},64,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,1,0,0,0,100,100,1,0,1,5,2,8,80,80,{top_mv},1
Style: Cap,{font},88,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,1,0,0,0,100,100,1,0,1,6,2,2,70,70,{cap_margin},1
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
    # extra lead-in: the first ~2 s of a downloaded HLS section can come out without audio
    a = max(0.0, c["start"] - 6.0)
    b = c["end"] + 3.0
    out = workdir / "src.%(ext)s"
    run([*ytdlp_base(), "-f", "b[height<=1080][height>=480]/bv*[height<=1080]+ba/b", "-S", "res:1080,fps",
         "--download-sections", f"*{a:.2f}-{b:.2f}", "--force-keyframes-at-cuts",
         "--merge-output-format", "mp4", "-o", str(out), c["vod_url"]], timeout=1200)
    files = [p for p in workdir.glob("src.*") if not p.name.endswith(".part")]
    if not files:
        raise RuntimeError("segment download produced no file")
    return files[0], a


def detect_facecam(src, start, dur):
    """Find a static webcam overlay: a face that stays in the same spot across the clip.
    Returns (cx, cy, face_w) in 1920x1080 coordinates, or None."""
    try:
        import cv2
        import numpy as np
        import subprocess
    except ImportError:
        return None
    dw, dh = 960, 540
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{start:.2f}", "-t", f"{dur:.2f}", "-i", str(src), "-vf",
         f"fps=1/3,scale={dw}:{dh}:force_original_aspect_ratio=decrease,pad={dw}:{dh}:(ow-iw)/2:(oh-ih)/2,format=gray",
         "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True, timeout=300).stdout
    n = len(raw) // (dw * dh)
    if n < 3:
        return None
    casc = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    hits = []
    for i in range(n):
        img = np.frombuffer(raw[i * dw * dh:(i + 1) * dw * dh], dtype=np.uint8).reshape(dh, dw)
        img = cv2.equalizeHist(img)
        for (x, y, w, h) in casc.detectMultiScale(img, 1.05, 3, minSize=(22, 22), maxSize=(240, 240)):
            hits.append((x + w / 2, y + h / 2, float(w), i))
    best = []
    for (cx, cy, w, _) in hits:
        grp = [h for h in hits if abs(h[0] - cx) < 28 and abs(h[1] - cy) < 28 and abs(h[2] - w) < 0.4 * w]
        if len({h[3] for h in grp}) > len({h[3] for h in best}):
            best = grp
    if len({h[3] for h in best}) < max(3, int(0.4 * n)):
        return None
    xs = sorted(h[0] for h in best)
    ys = sorted(h[1] for h in best)
    ws = sorted(h[2] for h in best)
    m = len(best) // 2
    fx, fy, fw = xs[m], ys[m], ws[m]
    # webcam overlay edge: a vertical line that is strong in (almost) every frame
    frames = [np.frombuffer(raw[i * dw * dh:(i + 1) * dw * dh], dtype=np.uint8).reshape(dh, dw).astype(np.float32)
              for i in range(n)]
    r0, r1 = int(max(0, fy - 1.2 * fw)), int(min(dh, fy + 1.2 * fw))
    prof = np.array([np.abs(np.diff(cv2.GaussianBlur(g[r0:r1], (3, 3), 0), axis=1)).mean(axis=0) for g in frames])
    low = np.percentile(prof, 25, axis=0)
    base = float(np.median(low)) + 1e-3

    def edge(lo_x, hi_x, outward_right):
        """Outermost strong persistent vertical edge (inner edges like furniture must not win)."""
        lo_x, hi_x = int(max(1, lo_x)), int(min(dw - 2, hi_x))
        if hi_x - lo_x < 6:
            return None
        seg = low[lo_x:hi_x]
        if seg.max() <= max(6.0, 8 * base):
            return None
        cand = np.where(seg >= max(6.0, 8 * base, 0.35 * seg.max()))[0]
        j = int(cand.max() if outward_right else cand.min())
        return (lo_x + j + 1) * 2
    er = edge(fx + 0.6 * fw, fx + 3.2 * fw, True)
    el = edge(fx - 3.2 * fw, fx - 0.6 * fw, False)
    return fx * 2, fy * 2, fw * 2, er, el


_AI_USED = 0
_ORT = None


def ai_facecam(src, spans, crop, out_path, k, total):
    """Upscale the webcam crop with Real-ESRGAN (ONNX, CPU) into a 1080xFH video.
    spans = [(start, dur), ...] on `src`. Returns True on success; False = use the normal filter."""
    global _ORT
    import subprocess
    import numpy as np
    import cv2
    import onnxruntime as ort
    cw, ch, x0, y0 = crop
    if _ORT is None:
        so = ort.SessionOptions()
        so.intra_op_num_threads = os.cpu_count() or 2
        _ORT = ort.InferenceSession(str(ROOT / "models" / "realesr-general-x4v3.onnx"), so,
                                    providers=["CPUExecutionProvider"])
    dummy = np.zeros((1, 3, ch, cw), np.float32)
    _ORT.run(None, {"x": dummy})
    t0 = time.time()
    for _ in range(2):
        _ORT.run(None, {"x": dummy})
    ft = (time.time() - t0) / 2
    fps = min(int(k.get("facecam_ai_fps", 12)), int(float(k.get("facecam_ai_budget_seconds", 300)) / (ft * total)))
    if fps < 6:
        log(f"  AI facecam would be too slow ({ft:.2f}s/frame), using normal upscale")
        return False
    log(f"  AI facecam: {ft:.2f}s/frame -> {fps} fps (about {int(ft * total * fps)}s)")
    norm = "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2"
    writer = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{FACE_H}", "-r", str(fps),
         "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-crf", "15", "-pix_fmt", "yuv420p", str(out_path)],
        stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    nbytes = cw * ch * 3
    t_start, limit = time.time(), float(k.get("facecam_ai_budget_seconds", 300)) * 1.6
    for (st, du) in spans:
        rd = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-ss", f"{st:.3f}", "-t", f"{du:.3f}", "-i", str(src), "-vf",
             f"fps={fps},{norm},crop={cw}:{ch}:{x0}:{y0}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        while True:
            buf = rd.stdout.read(nbytes)
            while buf and len(buf) < nbytes:
                more = rd.stdout.read(nbytes - len(buf))
                if not more:
                    break
                buf += more
            if len(buf) < nbytes:
                break
            if time.time() - t_start > limit:
                rd.kill(); writer.kill()
                raise RuntimeError("AI facecam over time budget")
            x = np.frombuffer(buf, np.uint8).reshape(ch, cw, 3).astype(np.float32).transpose(2, 0, 1)[None] / 255.0
            y = _ORT.run(None, {"x": x})[0][0].transpose(1, 2, 0)
            y = (np.clip(y, 0, 1) * 255).astype(np.uint8)
            y = cv2.resize(y, (W, FACE_H), interpolation=cv2.INTER_AREA)
            writer.stdin.write(y.tobytes())
        rd.wait()
    writer.stdin.close()
    writer.wait(timeout=120)
    return writer.returncode == 0 and Path(out_path).exists()


def render(c, meta, cfg, src, src_offset, out_path, workdir):
    k = cfg["clip"]
    teaser = float(k["teaser_seconds"])
    main_start = c["start"] - src_offset
    main_dur = c["end"] - c["start"]
    tz = max(0.0, c["peak"] - src_offset - 0.8)  # flash the moment itself first
    if tz + teaser > main_start + main_dur:
        tz = max(0.0, main_start + main_dur - teaser)
    total = teaser + main_dur

    face = detect_facecam(src, main_start, main_dur) if k.get("facecam_split", True) else None
    try:
        log("  source video:", run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                   "stream=width,height,r_frame_rate", "-of", "csv=p=0", str(src)]).strip())
    except Exception:
        pass
    FT, FH = 170, FACE_H                   # facecam panel: top y, height (1080 wide)
    game = None
    if face:
        cx, cy, fw, er, el = face
        ow = min(900.0, max(fw * 5.0, 380.0))        # estimated webcam overlay size in the source
        oh = ow * 0.9
        pad = 48
        hw = max(120.0, min(ow / 2, cx, 1920 - cx))   # webcam sits against the screen edge: it ends about where the face mirrors
        fx0, fx1 = cx - hw, cx + hw                   # webcam x-range: detected edge if found, else symmetric guess
        if er and cx < 960 and er - cx < 6 * fw:
            fx0, fx1 = 0.0, float(er)
        elif el and cx >= 960 and cx - el < 6 * fw:
            fx0, fx1 = float(el), 1920.0
        opts = []                                     # (area removed, crop x, y, w, h): drop the strip holding the webcam
        if fx1 < 0.5 * 1920:
            xk = min(0.45 * 1920, fx1 + pad)
            opts.append((xk * 1080, xk, 0, 1920 - xk, 1080))
        if fx0 > 0.5 * 1920:
            xk = max(0.55 * 1920, fx0 - pad)
            opts.append(((1920 - xk) * 1080, 0, 0, xk, 1080))
        if cy + oh / 2 < 0.5 * 1080:
            yk = cy + oh / 2 + pad
            opts.append((yk * 1920, 0, yk, 1920, 1080 - yk))
        if cy - oh / 2 > 0.5 * 1080:
            yk = cy - oh / 2 - pad
            opts.append(((1080 - yk) * 1920, 0, 0, 1920, yk))
        if opts:
            _, gx, gy, gw, gh = min(opts)
            gx, gy, gw, gh = (int(v) // 2 * 2 for v in (gx, gy, gw, gh))
            GH = min(900, int(round(W * gh / gw / 2) * 2))
            game = (gx, gy, gw, gh, GH)
            # facecam crop: the whole webcam overlay, centred on the face
            cw = max(240.0, fx1 - fx0)
            ch = cw * FH / W
            x0 = min(max(fx0, 0), 1920 - cw)
            y0 = min(max(cy - ch * 0.5, 0), 1080 - ch)
            cw, ch, x0, y0 = (int(v) // 2 * 2 for v in (cw, ch, x0, y0))
            log(f"  facecam at ({int(cx)},{int(cy)}); gameplay crop {gw}x{gh} (webcam removed)")
        else:
            face = None                                # webcam in the middle: can't separate it, keep whole frame
    mode = k.get("facecam_mode", "off")      # "off": webcam removed, gameplay big & centred; "split": webcam panel on top
    mode = "off" if mode in (False, None, "off") else str(mode)   # YAML turns a bare `off` into False
    solo = bool(game) and mode == "off"
    if solo:
        gx, gy, gw, gh, _ = game
        nw = min(gw, int(gh * W / 1000) // 2 * 2)        # aim for a ~1080x1000 gameplay picture
        gx += (gw - nw) // 2 // 2 * 2
        gw = nw
        GH = int(round(W * gh / gw / 2) * 2)
        game = (gx, gy, gw, gh, GH)
        GT = 330
        face = None
        cap_margin, hook_mv, top_mv = max(170, H - (GT + GH + 170)), 70, 90
    elif game:
        end_y = FT + FH + game[4]
        cap_margin, hook_mv, top_mv = max(170, H - (end_y + 130)), 60, 80
    else:
        face = None
        zoom = float(k.get("zoom", 1.0))
        fg_h = int(round(W * 9 / 16 * zoom / 2) * 2)
        fg_top = (H - fg_h) // 2
        cap_margin = max(150, H - (fg_top + fg_h) - 230)
        hook_mv, top_mv = 190, 210
    face_vid = None
    global _AI_USED
    if face and game and k.get("facecam_ai", True) and _AI_USED < int(k.get("facecam_ai_max_per_run", 2)):
        try:
            fv = workdir / "face_ai.mp4"
            if ai_facecam(src, [(tz, teaser), (main_start, main_dur)], (cw, ch, x0, y0), fv, k, total):
                face_vid = fv
                _AI_USED += 1
        except Exception as ex:
            log(f"  AI facecam failed, using normal upscale: {str(ex)[-200:]}")
    credit = c["channel_url"].replace("https://", "").replace("www.", "")
    ass = workdir / "subs.ass"
    ass.write_text(build_ass(c["words"], meta["hook"], credit, teaser, total, cap_margin, hook_mv, top_mv,
                              font="Mukta ExtraBold" if c.get("language") == "hi" else "Anton"),
                   encoding="utf-8")

    fonts = ROOT / "fonts"
    FPS = int(k.get("fps", 60))
    norm = f"fps={FPS},scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1"
    bgchain = (f"scale=270:480:force_original_aspect_ratio=increase,crop=270:480,gblur=sigma=8,"
               f"scale={W}:{H},eq=brightness=-0.12")
    if face:
        gx, gy, gw, gh, GH = game
        if face_vid:
            fcpart = f"[2:v]tpad=stop_mode=clone:stop_duration=2,framerate=fps={FPS},setpts=PTS-STARTPTS[fc];"
            head = f"[cv]split=2[a][b];"
        else:
            fcpart = (f"[c]crop={cw}:{ch}:{x0}:{y0},hqdn3d=3:2.5:4:4,scale={W}:{FH}:flags=lanczos,"
                      f"unsharp=5:5:0.6[fc];")
            head = f"[cv]split=3[a][b][c];"
        layout = (
            f"{head}[a]{bgchain}[bg];"
            f"[b]crop={gw}:{gh}:{gx}:{gy},scale={W}:{GH}:flags=lanczos[gp];"
            f"{fcpart}"
            f"[bg][fc]overlay=0:{FT}[t1];[t1][gp]overlay=0:{FT + FH}"
        )
    elif solo:
        gx, gy, gw, gh, GH = game
        layout = (
            f"[cv]split=2[a][b];[a]{bgchain}[bg];"
            f"[b]crop={gw}:{gh}:{gx}:{gy},scale={W}:{GH}:flags=lanczos[gp];"
            f"[bg][gp]overlay=0:{GT}"
        )
    else:
        layout = (
            f"[cv]split[a][b];[a]{bgchain}[bg];"
            f"[b]scale=-2:{fg_h},crop=min(iw\\,{W}):{fg_h}[fg];"
            f"[bg][fg]overlay=(W-w)/2:{fg_top}"
        )
    fc = (
        f"[0:v]{norm},setpts=PTS-STARTPTS[tv];[0:a]asetpts=PTS-STARTPTS[ta];"
        f"[1:v]{norm},setpts=PTS-STARTPTS[mv];[1:a]asetpts=PTS-STARTPTS[ma];"
        f"[tv][ta][mv][ma]concat=n=2:v=1:a=1[cv][ca];"
        f"{layout},ass={ass}:fontsdir={fonts},format=yuv420p[v];"
        f"[ca]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[aout]"
    )
    # two separate inputs (teaser + main) so ffmpeg never buffers minutes of raw frames
    run(["ffmpeg", "-v", "error", "-y",
         "-ss", f"{tz:.3f}", "-t", f"{teaser:.3f}", "-i", str(src),
         "-ss", f"{main_start:.3f}", "-t", f"{main_dur:.3f}", "-i", str(src),
         *(["-i", str(face_vid)] if face_vid else []),
         "-filter_complex", fc,
         "-map", "[v]", "-map", "[aout]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-profile:v", "high", "-level", "4.2", "-r", str(FPS), "-g", str(FPS * 2),
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-ar", "48000",
         "-movflags", "+faststart",
         "-t", f"{total:.3f}", str(out_path)], timeout=1800)
    return total


# ── publishing ──────────────────────────────────────────────────────────────

def gh(*args, check=True):
    return run(["gh", *args], timeout=600, check=check)


def publish_clip(night, n, c, mp4, txt):
    """One GitHub Release per clip = one 'folder' holding the video and its post text."""
    import datetime as _dt
    m = c["meta"]
    repo = os.environ.get("GITHUB_REPOSITORY", "GamerQuanTuM/gta6-clipper")
    day = _dt.date.fromisoformat(night).strftime("%d %b")
    name = f"{n:02d}-{c['streamer']}"
    tag = f"{night}-{name}"
    if subprocess_ok(["gh", "release", "view", tag]):
        tag += f"-{c['id'][-5:]}"
    folder = Path(tempfile.mkdtemp(prefix="pub_"))
    v, t = folder / f"{name}.mp4", folder / f"{name}-post.txt"
    shutil.copy(mp4, v)
    shutil.copy(txt, t)
    notes = "\n".join([
        f"**Hook:** {m['hook']}  ",
        f"**From:** [{c['streamer_name']}]({c['channel_url']}) · {c.get('duration_s', '?')}s · 1080x1920 60fps",
        "",
        "Download the MP4 below, then post it from the YouTube app (**Create → Short → upload**). "
        "Long-press a block to copy it.",
        "",
        "**Title**\n```\n" + m["title"] + "\n```",
        "**Description**\n```\n" + m["description"] + "\n```",
        "**Tags**\n```\n" + ", ".join(m["tags"]) + "\n```",
        "",
        f"[All clips from {day}](https://github.com/{repo}/releases?q={night}&expanded=true)",
    ])
    nf = folder / "notes.md"
    nf.write_text(notes, encoding="utf-8")
    title = f"{day} · #{n:02d} · {c['streamer_name']}: {m['title']}"[:120]
    gh("release", "create", tag, str(v), str(t), "--title", title, "--notes-file", str(nf))
    shutil.rmtree(folder, ignore_errors=True)
    return tag


def silent_spots(mp4, seconds=8, db=-55):
    """Quarter-second windows in the first few seconds that are silent (below db)."""
    import subprocess
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(mp4), "-t", str(seconds), "-vn", "-ac", "1",
                          "-ar", "16000", "-f", "f32le", "-"], capture_output=True).stdout
    a = np.frombuffer(raw, np.float32)
    out = []
    for i in range(0, len(a) - 4000, 4000):
        if 20 * np.log10(np.sqrt(np.mean(a[i:i + 4000] ** 2)) + 1e-9) < db:
            out.append(round(i / 16000, 2))
    return out


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
    published = 0
    pub = cfg["publish"]
    cap = cfg["finder"].get("max_clips_per_night") or 999
    min_words = cfg["finder"].get("min_words", 0)
    for path, c in items:
        if c.get("night", night) != night:      # re-render of an earlier night's clip
            save_json(night_file, night_items)
            night = c["night"]
            night_file = STATE / "nights" / f"{night}.json"
            night_items = load_json(night_file, [])
        if c.get("status") == "rejected":
            shutil.move(str(path), QUEUE / "failed" / path.name)
            continue
        if len(night_items) >= cap:
            log(f"Night cap of {cap} clips reached; leaving {path.name} unrendered")
            c.update(status="rejected", reject_reason="night cap reached")
            save_json(QUEUE / "failed" / path.name, c)
            path.unlink()
            continue
        if (c.get("meta") or {}).get("source") != "claude" and len(c.get("words") or []) < min_words:
            log(f"Skipping {c['id']}: not reviewed by Claude and the streamer barely speaks")
            c.update(status="rejected", reject_reason="no speech (fallback)")
            save_json(QUEUE / "failed" / path.name, c)
            path.unlink()
            continue
        workdir = Path(tempfile.mkdtemp(prefix="r_"))
        try:
            meta = finalize_meta(c, c.get("meta") or fallback_meta(c))
            log(f"Rendering {c['id']}  ->  {meta['title']}")
            src, off = download_segment(c, workdir)
            if not c.get("words"):          # re-render: captions weren't kept, transcribe again
                from find_moments import transcribe
                ms = c["start"] - off
                c["words"], _, _ = transcribe(src, ms, ms + c["end"] - c["start"], cfg["clip"]["whisper_model"])
            out_dir = ROOT / "out"
            out_dir.mkdir(exist_ok=True)
            mp4 = out_dir / f"{c['id']}.mp4"
            dur = render(c, meta, cfg, src, off, mp4, workdir)
            muted = silent_spots(mp4, seconds=120, db=-90)   # pure digital silence = Twitch muted it
            if len(muted) >= 8:
                log(f"  {c['id']}: {len(muted) / 4:.1f}s of muted audio (Twitch copyright mute?), not publishing")
                c.update(status="rejected", reject_reason="audio muted in the source stream")
                save_json(QUEUE / "failed" / path.name, {k: v for k, v in c.items() if k != "words"})
                path.unlink()
                continue
            quiet = silent_spots(mp4)
            if quiet:
                log(f"  note: quiet audio at {quiet} s")
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
            entry = ({k: c[k] for k in ("id", "streamer", "streamer_name", "channel_url",
                                        "meta", "duration_s") if k in c}
                     | ({"youtube_id": c["youtube_id"]} if c.get("youtube_id") else {}))
            if pub.get("github_release"):
                entry["release"] = publish_clip(night, len(night_items) + 1, c, mp4, txt)
                log(f"  published folder {entry['release']}")
                published += 1
            night_items.append(entry)
            save_json(night_file, night_items)
            mp4.unlink(missing_ok=True)
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
    log(f"Published {published} clips tonight's total: {len(night_items)}")


if __name__ == "__main__":
    main()
