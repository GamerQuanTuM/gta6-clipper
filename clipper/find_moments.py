"""Find the most hype moments in recent GTA VI streams.

For each streamer: locate the newest (or still-live) broadcast, scan the last
N minutes for spikes in chat activity (Twitch) and audio loudness (all
platforms), transcribe the best moments with faster-whisper, and write each
candidate to queue/pending/<id>.json for Claude to write hooks/titles.
"""
import json
import os
import re
import shutil
import signal
import sys
import tempfile
import time
import traceback
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from common import (QUEUE, STATE, campaign_active, load_config, load_json, log,  # noqa: E402
                    run, save_json, ytdlp_base)

HYPE = re.compile(
    r"(kekw|omegalul|lul|lmao+|lol|💀|😂|\?{3,}|\bw+\b|pog|holy|no way|clip (it|that)|"
    r"\bclip\b|wtf|nah+|bro+|insane|crazy|😭|🔥|kappa|icant|aware|monkas)", re.I)


# ── discovery ───────────────────────────────────────────────────────────────

def ytdlp_json(args, timeout=120):
    return json.loads(run([*ytdlp_base(), "-J", *args], timeout=timeout))


def game_match(info, keywords):
    if os.environ.get("ANY_GAME") == "1":  # test mode
        return True
    hay = " ".join([
        info.get("title") or "", info.get("description") or "",
        " ".join(info.get("categories") or []),
        " ".join((c.get("title") or "") for c in (info.get("chapters") or [])),
    ]).lower()
    return any(k in hay for k in keywords)


def recent_broadcasts(platform, user, cfg):
    url = (f"https://www.twitch.tv/{user}/videos?filter=all&sort=time" if platform == "twitch"
           else f"https://kick.com/{user}/videos")
    listing = ytdlp_json(["--flat-playlist", "--playlist-items", "1:3", url])
    max_age = cfg["finder"]["max_vod_age_hours"] * 3600
    for e in listing.get("entries") or []:
        vurl = e.get("url") or e.get("webpage_url")
        if not vurl:
            continue
        if platform == "twitch" and not vurl.startswith("http"):
            vurl = f"https://www.twitch.tv/videos/{str(e.get('id', '')).lstrip('v')}"
        try:
            info = ytdlp_json(["--skip-download", vurl])
        except Exception as ex:
            log(f"  {user}: could not read {vurl}: {str(ex)[-200:]}")
            continue
        dur = info.get("duration") or 0
        ts = info.get("timestamp") or 0
        if not dur or not ts:
            continue
        if time.time() - (ts + dur) > max_age:
            continue  # ended too long ago
        if not game_match(info, cfg["game_keywords"]):
            log(f"  {user}: '{(info.get('title') or '')[:60]}' isn't GTA VI, skipping")
            continue
        yield {
            "platform": platform, "streamer": user,
            "streamer_name": info.get("uploader") or info.get("channel") or user,
            "vod_id": str(info["id"]), "vod_url": vurl,
            "vod_title": info.get("title") or "", "duration": int(dur),
            "channel_url": (f"https://www.twitch.tv/{user}" if platform == "twitch"
                            else f"https://kick.com/{user}"),
        }
        return  # newest matching broadcast is enough


# ── signals ─────────────────────────────────────────────────────────────────

def download_audio(vod_url, start, end, workdir, timeout=300):
    out = workdir / "window.%(ext)s"
    run([*ytdlp_base(), "-f", "ba/worst", "--download-sections", f"*{start}-{end}",
         "-o", str(out), vod_url], timeout=timeout)
    files = [p for p in workdir.glob("window.*") if not p.name.endswith(".part")]
    if not files:
        raise RuntimeError("audio download produced no file")
    return files[0]


def audio_loudness(audio_path):
    import subprocess  # binary mode: raw PCM can't go through text pipes
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(audio_path), "-ac", "1", "-ar", "8000",
                          "-f", "s16le", "-"], capture_output=True, timeout=600).stdout
    x =np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    n = len(x) // 8000
    if n == 0:
        return np.zeros(0)
    x = x[: n * 8000].reshape(n, 8000)
    rms = np.sqrt((x ** 2).mean(axis=1)) + 1.0
    return 20 * np.log10(rms)  # dB per second


class _Timeout(Exception):
    pass


def chat_activity(vod_url, start, end, seconds):
    """Per-second hype-weighted chat counts (Twitch only, best effort)."""
    from chat_downloader import ChatDownloader

    counts = np.zeros(seconds, dtype=np.float32)
    samples = {}

    def _alarm(*_):
        raise _Timeout()

    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(240)
    got = 0
    try:
        chat = ChatDownloader().get_chat(vod_url, start_time=start, end_time=end,
                                         message_types=["text_message"])
        for m in chat:
            t = m.get("time_in_seconds")
            if t is None:
                continue
            i = int(t - start)
            if 0 <= i < seconds:
                text = m.get("message") or ""
                counts[i] += 1.0 + (1.5 if HYPE.search(text) else 0.0)
                samples.setdefault(i, []).append(text[:80])
                got += 1
    except _Timeout:
        log("    chat download timed out, using what we got")
    except Exception as ex:
        log(f"    chat unavailable ({type(ex).__name__}); using audio only")
    finally:
        signal.alarm(0)
    return (counts if got > 50 else None), samples


def robust_z(x, smooth=5):
    if x is None or len(x) == 0:
        return None
    k = np.ones(smooth) / smooth
    s = np.convolve(x, k, mode="same")
    med = np.median(s)
    mad = np.median(np.abs(s - med)) * 1.4826 + 1e-6
    return (s - med) / mad


def pick_peaks(score, n, min_gap, min_score, margin_lo, margin_hi):
    order = np.argsort(score)[::-1]
    picked = []
    for i in order:
        if score[i] < min_score or len(picked) >= n:
            break
        if i < margin_lo or i > len(score) - 1 - margin_hi:
            continue
        if all(abs(i - j) >= min_gap for j in picked):
            picked.append(int(i))
    return picked


# ── transcription ───────────────────────────────────────────────────────────

_MODEL = None


def transcribe(audio_path, ss, to, model_name):
    """Return list of [start, end, word] relative to ss."""
    global _MODEL
    from faster_whisper import WhisperModel
    if _MODEL is None:
        _MODEL = WhisperModel(model_name, device="cpu", compute_type="int8")
    wav = Path(audio_path).with_name(f"seg_{int(ss)}.wav")
    run(["ffmpeg", "-v", "error", "-y", "-ss", f"{ss:.2f}", "-to", f"{to:.2f}", "-i", str(audio_path),
         "-ac", "1", "-ar", "16000", str(wav)], timeout=120)
    segs, info = _MODEL.transcribe(str(wav), word_timestamps=True, vad_filter=True, beam_size=1)
    words = []
    for s in segs:
        for w in s.words or []:
            words.append([round(w.start, 2), round(w.end, 2), w.word.strip()])
    return words, info.language


def snap_bounds(words, lo, hi, cfg):
    """Nudge start/end so we don't cut mid-sentence. Times relative to window."""
    c = cfg["clip"]
    # start: first word that begins after a pause >= 0.4s near lo
    best_lo = lo
    for i, w in enumerate(words):
        if lo - 4 <= w[0] <= lo + 3:
            prev_end = words[i - 1][1] if i else -9
            if w[0] - prev_end >= 0.4:
                best_lo = w[0] - 0.15
                break
    best_hi = hi
    for i, w in enumerate(words):
        if hi - 3 <= w[1] <= hi + 4:
            nxt = words[i + 1][0] if i + 1 < len(words) else 9e9
            if nxt - w[1] >= 0.4:
                best_hi = w[1] + 0.3
                break
    length = best_hi - best_lo
    if length < c["min_seconds"]:
        best_lo = best_hi - c["min_seconds"]
    if length > c["max_seconds"]:
        best_hi = best_lo + c["max_seconds"]
    return best_lo, best_hi


# ── main ────────────────────────────────────────────────────────────────────

def process_broadcast(b, cfg, progress, workdir):
    f, c = cfg["finder"], cfg["clip"]
    key = f"{b['platform']}:{b['vod_id']}"
    end = max(0, b["duration"] - 20)
    start = max(progress.get(key, 0), end - f["lookback_minutes"] * 60, 0)
    if end - start < 150:
        log(f"  {b['streamer']}: only {end - start}s new since last run, skipping")
        return [], key, None
    log(f"  {b['streamer']}: scanning {start // 60}-{end // 60} min of '{b['vod_title'][:50]}'")
    audio = download_audio(b["vod_url"], start, end, workdir, f.get("download_timeout", 300))
    loud = audio_loudness(audio)
    secs = len(loud)
    if secs < 120:
        return [], key, end
    chat, samples = (chat_activity(b["vod_url"], start, start + secs, secs)
                     if b["platform"] == "twitch" and f.get("use_chat", False) else (None, {}))
    az = robust_z(loud)
    cz = robust_z(chat)
    if cz is not None:
        d = f["chat_delay_seconds"]
        cz = np.concatenate([cz[d:], np.zeros(d)])  # shift chat back to the moment
        score = 0.65 * cz + 0.35 * az
    else:
        score = az
    thr = f["min_score"] if cz is not None else f.get("min_score_audio_only", 1.8)
    if os.environ.get("TEST_STREAMERS"):
        thr = min(thr, 1.0)  # test mode: always produce something
    top = np.sort(score)[::-1][:3]
    log(f"    loudest scores: {[round(float(v), 1) for v in top]} (need {thr})")
    peaks = pick_peaks(score, f["clips_per_streamer"], f["min_gap_seconds"], thr,
                       margin_lo=c["before_moment"] + 5, margin_hi=c["after_moment"] + 5)
    cands = []
    for p in peaks:
        lo, hi = p - c["before_moment"], p + c["after_moment"]
        words, lang = transcribe(audio, max(0, lo - 5), min(secs, hi + 5), c["whisper_model"])
        base = max(0, lo - 5)
        rel = [[w[0] + base, w[1] + base, w[2]] for w in words]  # window-relative
        lo2, hi2 = snap_bounds(rel, lo, hi, cfg)
        clip_words = [[round(w[0] - lo2, 2), round(w[1] - lo2, 2), w[2]]
                      for w in rel if w[0] >= lo2 and w[1] <= hi2]
        chat_near = []
        for s in range(p - 2, p + f["chat_delay_seconds"] + 8):
            chat_near += samples.get(s, [])
        cid = f"{b['platform']}-{b['streamer']}-{b['vod_id']}-{int(start + p)}"
        cands.append({
            "id": re.sub(r"[^A-Za-z0-9_-]", "", cid),
            "status": "pending",
            **{k: b[k] for k in ("platform", "streamer", "streamer_name", "channel_url",
                                 "vod_url", "vod_id", "vod_title")},
            "start": round(start + lo2, 2),
            "end": round(start + hi2, 2),
            "peak": round(start + p, 2),
            "score": round(float(score[p]), 2),
            "language": lang,
            "transcript": " ".join(w[2] for w in clip_words),
            "words": clip_words,
            "chat_sample": chat_near[:25],
            "created_unix": int(time.time()),
            "meta": None,
        })
        log(f"    moment @ {int(start + p) // 60}:{int(start + p) % 60:02d} score {score[p]:.1f}")
    return cands, key, end


def main():
    cfg = load_config()
    if not campaign_active(cfg):
        return
    progress = load_json(STATE / "progress.json", {})
    known = {p.stem for d in ("pending", "ready", "done", "failed") for p in (QUEUE / d).glob("*.json")}
    test = os.environ.get("TEST_STREAMERS", "").strip()
    if test:
        cfg["streamers"] = {"twitch": [], "kick": []}
        for item in test.split(","):
            plat, _, name = item.strip().partition(":")
            if plat in cfg["streamers"] and name:
                cfg["streamers"][plat].append(name)
        log(f"TEST MODE: {cfg['streamers']}")
    all_cands = []
    deadline = time.time() + cfg["finder"].get("run_budget_minutes", 32) * 60
    for platform in ("twitch", "kick"):
        for user in cfg["streamers"].get(platform) or []:
            if time.time() > deadline:
                log(f"Time budget used up, skipping {platform}/{user} (will be first next run)")
                continue
            log(f"{platform}/{user}")
            workdir = Path(tempfile.mkdtemp(prefix=f"{user}_"))
            try:
                for b in recent_broadcasts(platform, user, cfg):
                    cands, key, new_end = process_broadcast(b, cfg, progress, workdir)
                    if new_end:
                        progress[key] = new_end
                    all_cands += [x for x in cands if x["id"] not in known]
            except Exception:
                log(f"  {user}: failed: {traceback.format_exc()[-300:]}")
            finally:
                shutil.rmtree(workdir, ignore_errors=True)
    all_cands.sort(key=lambda x: x["score"], reverse=True)
    keep = all_cands[: cfg["finder"]["clips_per_run"]]
    for x in keep:
        save_json(QUEUE / "pending" / f"{x['id']}.json", x)
    if not test:
        save_json(STATE / "progress.json", progress)
    log(f"Queued {len(keep)} new moments (found {len(all_cands)}).")


if __name__ == "__main__":
    main()
