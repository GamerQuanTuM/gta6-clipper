"""One-off: swap a published clip's video for the 30 fps original (converted to 60 fps).
Used when Twitch muted a stream after the original render, so a re-render came out silent.
Usage: python tools/replace_from_old.py OLD_TAG OLD_ASSET NEW_TAG NEW_NAME [...more groups of 4]"""
import subprocess
import sys
import tempfile
from pathlib import Path

args = sys.argv[1:]
for i in range(0, len(args), 4):
    old_tag, old_asset, new_tag, new_name = args[i:i + 4]
    d = Path(tempfile.mkdtemp())
    subprocess.run(["gh", "release", "download", old_tag, "-p", old_asset, "-D", str(d)], check=True)
    out = d / new_name
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(d / old_asset), "-vf", "fps=60", "-c:v", "libx264",
                    "-preset", "veryfast", "-crf", "18", "-profile:v", "high", "-level", "4.2", "-g", "120",
                    "-c:a", "copy", "-movflags", "+faststart", str(out)], check=True)
    subprocess.run(["gh", "release", "upload", new_tag, str(out), "--clobber"], check=True)
    print(f"replaced {new_tag}/{new_name} with {old_tag}/{old_asset} at 60 fps")
