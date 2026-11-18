"""Shared helpers for the GTA VI clipper pipeline."""
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
QUEUE = ROOT / "queue"
STATE = ROOT / "state"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def log(*args):
    print(f"[{dt.datetime.now(IST):%H:%M:%S IST}]", *args, flush=True)


def load_config():
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def now_ist():
    return dt.datetime.now(IST)


def night_key(t=None):
    """The IST date of the night a run belongs to (1-7 AM IST -> that date)."""
    t = t or now_ist()
    return t.date().isoformat()


def campaign_active(cfg, grace_hours=0):
    """True when we are inside the configured dates and hours (IST).
    Set FORCE_RUN=1 to bypass (manual testing)."""
    if os.environ.get("FORCE_RUN") == "1":
        return True
    c = cfg["campaign"]
    t = now_ist()
    start = dt.date.fromisoformat(str(c["start_date_ist"]))
    end = dt.date.fromisoformat(str(c["end_date_ist"]))
    if not (start <= t.date() <= end):
        log(f"Outside campaign dates ({start}..{end}); nothing to do.")
        return False
    if not (c["start_hour_ist"] <= t.hour < c["end_hour_ist"] + grace_hours):
        log(f"Outside campaign hours ({c['start_hour_ist']}-{c['end_hour_ist']} IST); nothing to do.")
        return False
    return True


def run(cmd, timeout=None, check=True):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        tail = (p.stderr or p.stdout or "")[-1500:]
        raise RuntimeError(f"Command failed ({p.returncode}): {' '.join(map(str, cmd[:6]))} ...\n{tail}")
    return p.stdout


def load_json(path, default):
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def ytdlp_base():
    """Common yt-dlp flags. Impersonation helps with Kick's Cloudflare."""
    return [sys.executable, "-m", "yt_dlp", "--no-warnings", "--no-progress",
            "--socket-timeout", "30", "--retries", "5"]
