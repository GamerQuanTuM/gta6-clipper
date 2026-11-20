"""Post a finished clip as an Instagram Reel (Instagram API with Instagram Login).

Needs one GitHub secret: IG_ACCESS_TOKEN (a long-lived Instagram user token with the
instagram_business_basic + instagram_business_content_publish permissions, for a
Professional (Creator or Business) account). Tokens last 60 days; refresh_token()
extends one when it is more than a day old.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://graph.instagram.com/v25.0"
RUPLOAD = "https://rupload.facebook.com/ig-api-upload/v25.0"


def _call(method, url, params=None, data=None, headers=None, timeout=120):
    tok = os.environ["IG_ACCESS_TOKEN"]
    params = dict(params or {})
    if data is None and headers is None:
        params["access_token"] = tok
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Instagram {method} {url.split('?')[0]} -> {e.code}: {e.read()[:400]!r}") from None


def enabled():
    return bool(os.environ.get("IG_ACCESS_TOKEN"))


def account():
    return _call("GET", f"{API}/me", {"fields": "user_id,username,account_type"})


def posts_left_today():
    """How many more posts the API allows in the current 24 h window (None if unknown)."""
    try:
        d = _call("GET", f"{API}/me/content_publishing_limit", {"fields": "quota_usage,config"})["data"][0]
        return d["config"]["quota_total"] - d["quota_usage"]
    except Exception:
        return None


def _wait_ready(cid, max_wait=600):
    t0 = time.time()
    while time.time() - t0 < max_wait:
        st = _call("GET", f"{API}/{cid}", {"fields": "status_code,status"})
        code = st.get("status_code")
        if code == "FINISHED":
            return
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram could not process the video: {st.get('status')}")
        time.sleep(10)
    raise RuntimeError("Instagram took too long to process the video")


def post_reel(mp4_path, caption, public_url=None):
    """Upload and publish; returns (media_id, permalink)."""
    caption = caption[:2150]
    base = {"media_type": "REELS", "caption": caption, "share_to_feed": "true"}
    cid = None
    try:   # 1) send the file itself (no public URL needed)
        cid = _call("POST", f"{API}/me/media", {**base, "upload_type": "resumable"})["id"]
        size = os.path.getsize(mp4_path)
        with open(mp4_path, "rb") as fh:
            _call("POST", f"{RUPLOAD}/{cid}", data=fh.read(), timeout=600, headers={
                "Authorization": f"OAuth {os.environ['IG_ACCESS_TOKEN']}",
                "offset": "0", "file_size": str(size), "Content-Type": "application/octet-stream"})
    except Exception as e:
        if not public_url:
            raise
        print(f"  Instagram file upload failed ({str(e)[:160]}); letting Instagram fetch the public link instead")
        cid = _call("POST", f"{API}/me/media", {**base, "video_url": public_url})["id"]
    _wait_ready(cid)
    media_id = _call("POST", f"{API}/me/media_publish", {"creation_id": cid})["id"]
    link = _call("GET", f"{API}/{media_id}", {"fields": "permalink"}).get("permalink", "")
    return media_id, link


def refresh_token():
    """Extend a long-lived token by 60 days. Returns the new token (or None)."""
    try:
        return _call("GET", "https://graph.instagram.com/refresh_access_token",
                     {"grant_type": "ig_refresh_token"}).get("access_token")
    except Exception:
        return None


def caption_for(meta):
    if meta.get("ig_caption"):
        return meta["ig_caption"]
    tags = " ".join("#" + "".join(ch for ch in t.title() if ch.isalnum()) for t in (meta.get("tags") or [])[:12])
    return f"{meta['title']}\n\n{meta['description']}\n\n{tags}".strip()
