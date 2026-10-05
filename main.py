"""Toolora backend: POST /download {"url": "...", "mode": "video" | "audio"} -> returns the file.
  mode "video" (default): best quality MP4 that fits the size cap.
  mode "audio": the sound only, as an MP3.

Run locally:  uvicorn main:app --reload
Env vars (all optional):
  ALLOWED_ORIGINS   comma-separated site origins, e.g. https://toolora.com,https://www.toolora.com  (default: *)
  MAX_FILESIZE_MB   per-download cap (default 200)
  MAX_CONCURRENT    simultaneous downloads (default 3)
  RATE_PER_MINUTE   requests per IP per minute (default 10)
  PROXY_SECRET      shared secret sent by the Toolora worker; when set, /download only accepts requests that carry it
  COOKIES_TXT       (optional) browser cookies in Netscape cookies.txt format. Lets Instagram/YouTube serve the server
                    like a logged-in browser, which fixes "login required" / "sign in to confirm" blocks.
                    Alternatives: COOKIES_B64 (the same file, base64-encoded) or a Render Secret File named cookies.txt
  MAX_SECONDS       give up on a link after this many seconds of trying different methods (default 80)
  YTDLP_PROXY       (optional) proxy URL such as http://user:pass@host:port, used for every outgoing request
"""
import asyncio, base64, hmac, html, os, re, shutil, subprocess, tempfile, time
import urllib.error, urllib.request
from pathlib import Path
from typing import Literal
from collections import defaultdict, deque
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import yt_dlp
import yt_dlp.cookies
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask

ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
MAX_MB = int(os.getenv("MAX_FILESIZE_MB", "200"))
RATE = int(os.getenv("RATE_PER_MINUTE", "10"))
PROXY_SECRET = os.getenv("PROXY_SECRET", "")
OUT_PROXY = os.getenv("YTDLP_PROXY", "").strip()
BUDGET = float(os.getenv("MAX_SECONDS", "100"))
slots = asyncio.Semaphore(int(os.getenv("MAX_CONCURRENT", "3")))

# Only these sites (and their subdomains) are accepted; this also blocks SSRF to internal hosts.
ALLOWED_HOSTS = ("youtube.com", "youtu.be", "tiktok.com", "instagram.com", "instagr.am", "twitter.com", "x.com", "snapchat.com")

app = FastAPI(title="Toolora backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGINS,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", "X-Toolora-Caption"],  # frontend reads the filename and post caption
)

hits: dict[str, deque] = defaultdict(deque)


def from_proxy(request: Request) -> bool:
    """True when the request carries the shared secret that only the Toolora worker knows."""
    if not PROXY_SECRET:
        return False
    sent = request.headers.get("x-proxy-secret", "")
    return hmac.compare_digest(sent.encode(), PROXY_SECRET.encode())


def check_rate(ip: str) -> None:
    now, q = time.time(), hits[ip]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= RATE:
        raise HTTPException(429, "Too many requests. Try again in a minute.")
    q.append(now)


class Req(BaseModel):
    url: str
    mode: Literal["video", "audio"] = "video"


def valid_url(u: str) -> bool:
    try:
        p = urlparse(u.strip())
    except ValueError:
        return False
    host = (p.hostname or "").lower()
    return p.scheme in ("http", "https") and any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)


SORT = ["res", "ext:mp4:m4a"]  # best resolution, preferring MP4/M4A so the result plays on phones
YT_SORT = ["res:1080", "vcodec:h264", "acodec:aac", "ext:mp4:m4a"]  # YouTube: up to 1080p, phone-friendly codecs
FRAGMENT = re.compile(r"\.f[\w-]+\.[A-Za-z0-9]+$")  # intermediate streams such as "name.f137.mp4"
YT_FINAL = ("private video", "has been removed", "been terminated", "copyright", "members-only", "members only", "join this channel", "live event will begin", "premieres in", "unsupported url", "is not a valid url", "video has been deleted")
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def clear_folder(folder: str) -> None:
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                os.remove(path)
            except OSError:
                pass


def is_youtube(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "youtube.com" or host.endswith(".youtube.com") or host == "youtu.be" or host.endswith(".youtu.be")


IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
IG_CDN = ("cdninstagram.com", "fbcdn.net")  # the only hosts the fallback downloader will pull media from


def is_instagram(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in ("instagram.com", "instagr.am") or host.endswith(".instagram.com") or host.endswith(".instagr.am")


def is_tiktok(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "tiktok.com" or host.endswith(".tiktok.com")


def is_snapchat(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "snapchat.com" or host.endswith(".snapchat.com")


def load_cookies() -> str:
    """Optional browser cookies (Netscape format) from env vars or a secret file. Empty string when not configured."""
    raw = os.getenv("COOKIES_TXT", "")
    if not raw.strip() and os.getenv("COOKIES_B64", "").strip():
        try:
            raw = base64.b64decode(os.environ["COOKIES_B64"]).decode("utf-8", "replace")
        except Exception:
            raw = ""
    if not raw.strip():
        here = os.path.dirname(os.path.abspath(__file__))
        for path in (os.getenv("COOKIES_FILE", ""), "/etc/secrets/cookies.txt", os.path.join(here, "cookies.txt")):
            if path and os.path.isfile(path):
                try:
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        raw = fh.read()
                    break
                except OSError:
                    pass
    raw = raw.strip()
    if not raw:
        return ""
    if "\n" not in raw and "\\n" in raw:  # some dashboards store a multi-line value with literal \n
        raw = raw.replace("\\n", "\n")
    # Keep only well-formed cookie lines; one malformed line would otherwise make every download fail.
    lines = ["# Netscape HTTP Cookie File"]
    for line in raw.splitlines():
        line = line.strip()
        if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        parts = line.split("\t") if "\t" in line else line.split(None, 6)
        if len(parts) != 7:
            continue
        domain = parts[0].replace("#HttpOnly_", "")
        parts[1] = "TRUE" if domain.startswith(".") else "FALSE"  # the "include subdomains" flag must match the leading dot
        parts[3] = "TRUE" if parts[3].strip().upper() == "TRUE" else "FALSE"
        if not parts[4].strip().lstrip("-").isdigit():
            parts[4] = "0"
        lines.append("\t".join(parts))
    return "\n".join(lines) + "\n" if len(lines) > 1 else ""


COOKIES = load_cookies()


def open_url(req, timeout=25):
    handlers = [urllib.request.ProxyHandler({"http": OUT_PROXY, "https": OUT_PROXY})] if OUT_PROXY else []
    return urllib.request.build_opener(*handlers).open(req, timeout=timeout)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def normalize_url(url: str) -> str:
    """Make links friendlier for the extractors: instagr.am -> instagram.com, resolve /share/ links, drop tracking params."""
    url = url.strip()
    if not is_instagram(url):
        return url
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if host == "instagr.am" or host.endswith(".instagr.am"):
        host = "www.instagram.com"
    path = p.path or "/"
    cur = f"https://{host}{path}"
    for _ in range(4):  # share links such as /share/reel/XXXX redirect to the real /reel/<code>/ page
        if not urlparse(cur).path.startswith("/share/"):
            break
        try:
            handlers = [_NoRedirect]
            if OUT_PROXY:
                handlers.append(urllib.request.ProxyHandler({"http": OUT_PROXY, "https": OUT_PROXY}))
            urllib.request.build_opener(*handlers).open(
                urllib.request.Request(cur, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}), timeout=15).close()
            break  # no redirect: nothing more to resolve
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location") if e.headers else None
            if e.code not in (301, 302, 303, 307, 308) or not loc:
                break
            nxt = urljoin(cur, loc)
            q = parse_qs(urlparse(nxt).query)
            if "/accounts/login" in urlparse(nxt).path and q.get("next"):  # login wall that remembers the target
                nxt = urljoin(cur, unquote(q["next"][0]))
            if not is_instagram(nxt):
                break
            np = urlparse(nxt)
            cur = f"https://{(np.hostname or host).lower()}{np.path or '/'}"
        except Exception:
            break
    return cur


def clean_error(e: BaseException) -> str:
    """A readable one-line reason; never empty."""
    msg = ANSI.sub("", str(e)).strip()
    msg = re.sub(r"^(ERROR:\s*)+", "", msg)
    msg = re.sub(r"\s+", " ", msg).strip()
    if not msg:
        cause = getattr(e, "exc_info", None)
        cause = cause[1] if cause else e.__cause__
        msg = re.sub(r"\s+", " ", str(cause or "")).strip()
    if not msg:
        msg = ("the site didn't return a playable file. The post may be private, deleted or restricted, "
               "or the site is temporarily blocking the server. Please try again in a moment.")
    low = msg.lower()
    if "sign in to confirm" in low and "bot" in low:
        msg = ("YouTube is refusing this server's connection right now (it asked to confirm the visitor is not a bot). "
               "Please try again in a few minutes.")
    return msg[:360]


def _unesc(s: str) -> str:
    s = html.unescape(s)
    s = re.sub(r"\\+/", "/", s)
    s = re.sub(r"\\+u0026", "&", s)
    s = re.sub(r"\\+u0025", "%", s)
    return s


def _uniq(xs):
    seen, out = set(), []
    for x in xs:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def clean_caption(value) -> str:
    if not isinstance(value, str):
        return ""
    value = html.unescape(value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:4000]


def caption_from_info(info: dict, fallback: str = "") -> str:
    if isinstance(info, dict):
        for key in ("description", "caption", "title"):
            text = clean_caption(info.get(key))
            if text:
                return text
    return clean_caption(fallback)


def write_caption(folder: str, caption: str) -> None:
    try:
        Path(os.path.join(folder, ".caption.txt")).write_text(clean_caption(caption), encoding="utf-8")
    except Exception:
        pass


def read_caption(folder: str) -> str:
    try:
        return clean_caption(Path(os.path.join(folder, ".caption.txt")).read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return ""


def ig_media_urls(page: str):
    """Pull direct video / picture URLs out of an Instagram embed or post page."""
    t = _unesc(page)
    vids = re.findall(r'video_url\\*"\s*:\s*\\*"(https?://[^"\\\s]+)', t)
    vids += re.findall(r'<meta[^>]+property=["\']og:video(?::secure_url|:url)?["\'][^>]+content=["\'](https?://[^"\']+)', t)
    vids += re.findall(r'<video[^>]+src=["\'](https?://[^"\']+)', t)
    imgs = re.findall(r'display_url\\*"\s*:\s*\\*"(https?://[^"\\\s]+)', t)
    imgs += re.findall(r'class=["\'][^"\']*EmbeddedMediaImage[^"\']*["\'][^>]+src=["\'](https?://[^"\']+)', t)
    imgs += re.findall(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\'](https?://[^"\']+)', t)
    return _uniq(vids), _uniq(imgs)


def cdn_ok(u: str) -> bool:
    p = urlparse(u)
    host = (p.hostname or "").lower()
    return p.scheme == "https" and any(host == h or host.endswith("." + h) for h in IG_CDN)


def page_text(url: str) -> str:
    """Fetch a page like a browser. Uses curl_cffi (Chrome impersonation) when present, plain urllib otherwise."""
    try:
        from curl_cffi import requests as cr
        kw = {"proxies": {"http": OUT_PROXY, "https": OUT_PROXY}} if OUT_PROXY else {}
        r = cr.get(url, headers={"Accept-Language": "en-US,en;q=0.9"}, impersonate="chrome", timeout=25, allow_redirects=True, **kw)
        if r.status_code == 200 and r.text:
            return r.text
    except Exception:
        pass
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
                                                   "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
        with open_url(req) as r:
            return r.read(6_000_000).decode("utf-8", "replace")
    except Exception:
        return ""


def download_to(u: str, path: str) -> None:
    limit = MAX_MB * 1024 * 1024
    got = 0
    req = urllib.request.Request(u, headers={"User-Agent": UA, "Referer": "https://www.instagram.com/"})
    with open_url(req, 45) as r, open(path, "wb") as fh:
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            got += len(chunk)
            if got > limit:
                raise RuntimeError(f"the file is larger than the {MAX_MB} MB limit")
            fh.write(chunk)
    if got == 0:
        raise RuntimeError("the download was empty")


def to_mp3(src: str, folder: str, stem: str) -> str:
    out = os.path.join(folder, stem + ".mp3")
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-vn", "-codec:a", "libmp3lame", "-b:a", "192k", out],
                       capture_output=True, timeout=180)
    if r.returncode != 0 or not os.path.isfile(out) or os.path.getsize(out) == 0:
        raise RuntimeError("this post has no audio track")
    os.remove(src)
    return out


def instagram_fallback(url: str, folder: str, mode: str) -> str:
    """Last resort when yt-dlp can't read an Instagram post: read the public embed page and download the media directly."""
    m = re.search(r"instagram\.com/(?:[^/?#]+/)?(?:p|reels?|tv)/([A-Za-z0-9_-]+)", url)
    if not m:
        raise RuntimeError("not a post, reel or video link")
    code = m.group(1)
    vids, imgs = [], []
    for page in (f"https://www.instagram.com/p/{code}/embed/captioned/",
                 f"https://www.instagram.com/reel/{code}/embed/captioned/",
                 f"https://www.instagram.com/p/{code}/"):
        text = page_text(page)
        if not text:
            continue
        v, i = ig_media_urls(text)
        vids += [x for x in v if cdn_ok(x)]
        imgs += [x for x in i if cdn_ok(x)]
        if vids:
            break
    stem = f"instagram_{code}"
    last = None
    for u in _uniq(vids):
        path = os.path.join(folder, stem + ".mp4")
        try:
            download_to(u, path)
            return to_mp3(path, folder, stem) if mode == "audio" else path
        except Exception as e:
            last = e
    if mode == "audio":
        raise last or RuntimeError("this post has no audio track")
    for u in _uniq(imgs):
        ext = os.path.splitext(urlparse(u).path)[1].lower()
        path = os.path.join(folder, stem + (ext if ext in IMAGE_EXT else ".jpg"))
        try:
            download_to(u, path)
            return path
        except Exception as e:
            last = e
    raise last or RuntimeError("Instagram did not return any media for this link")



def snapchat_media_url(url: str) -> str:
    """Extract Snapchat's direct Spotlight mediaUrl/contentUrl from the page JSON.
    Prefer Snapchat's direct story media URL over the generic HTML5 fallback because the
    generic share player can expose a watermarked rendition.
    """
    text = page_text(url)
    if not text:
        raise RuntimeError("Snapchat did not return the Spotlight page")
    candidates = []
    # The public Spotlight page contains a JSON script with pageProps.spotlightFeed.
    # Pull likely mediaUrl/contentUrl values without depending on one exact React build.
    for key in ("mediaUrl", "contentUrl", "media_url", "content_url"):
        candidates += re.findall(r'"' + re.escape(key) + r'"\s*:\s*"(https?://[^"\\]+)"', text)
        candidates += re.findall(r'"' + re.escape(key) + r'"\s*:\s*"(https?:\\/\\/[^"\\]+)"', text)
    for raw in candidates:
        u = _unesc(raw).replace('\\/', '/')
        host = (urlparse(u).hostname or '').lower()
        if u.startswith('https://') and host and (host.endswith('snap.com') or host.endswith('sc-cdn.net') or 'snap' in host):
            return u
    # Last chance: let yt-dlp's generic extractor expose the same direct URL, but never
    # select a format merely because it is the only one; this path is only used if the
    # page JSON has changed.
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True, "remote_components": ["ejs:github"]}) as ydl:
            info = ydl.extract_info(url, download=False)
        u = info.get("url") if isinstance(info, dict) else None
        if u:
            return u
        for f in (info.get("formats") or []) if isinstance(info, dict) else []:
            u = f.get("url")
            if u:
                return u
    except Exception:
        pass
    raise RuntimeError("Snapchat did not expose a direct Spotlight video")


def snapchat_fallback(url: str, folder: str, mode: str) -> str:
    if mode == "audio":
        raise RuntimeError("Snapchat audio is not available separately")
    u = snapchat_media_url(url)
    path = os.path.join(folder, "snapchat_spotlight.mp4")
    download_to(u, path)
    return path


def _image_url_from_info(info: dict) -> str | None:
    if not isinstance(info, dict):
        return None
    for key in ("url", "display_url"):
        u = info.get(key)
        if isinstance(u, str) and re.search(r'\.(?:jpe?g|png|webp|gif)(?:[?#]|$)', u, re.I):
            return u
    for f in info.get("formats") or []:
        u = f.get("url")
        if isinstance(u, str) and (str(f.get("ext") or "").lower() in {"jpg", "jpeg", "png", "webp", "gif"} or re.search(r'\.(?:jpe?g|png|webp|gif)(?:[?#]|$)', u, re.I)):
            return u
    return None


def _video_url_from_info(info: dict) -> str | None:
    if not isinstance(info, dict):
        return None
    u = info.get("url")
    if isinstance(u, str) and u.startswith(("http://", "https://")) and not re.search(r'\.(?:jpe?g|png|webp|gif)(?:[?#]|$)', u, re.I):
        if info.get("vcodec") not in (None, "none") or str(info.get("ext") or "").lower() in {"mp4", "webm", "mov", "mkv"}:
            return u
    choices=[]
    for f in info.get("formats") or []:
        fu=f.get("url")
        if not isinstance(fu,str) or not fu.startswith(("http://","https://")):
            continue
        if str(f.get("vcodec") or "none").lower()=="none":
            continue
        if str(f.get("ext") or "").lower() in {"mp4","webm","mov","mkv"} or "video" in str(f.get("format_note") or "").lower():
            choices.append(f)
    if choices:
        choices.sort(key=lambda f: (int(f.get("height") or 0), int(f.get("width") or 0)), reverse=True)
        return choices[0].get("url")
    return None


def gallery_media(url: str, folder: str) -> tuple[list[dict], str]:
    """Return every actual media item in a TikTok/Instagram post, preserving order.
    Unlike the old photo-only gallery, this includes videos in mixed Instagram carousels.
    """
    try:
        with yt_dlp.YoutubeDL({
            "quiet": True, "no_warnings": True, "noplaylist": False,
            "extract_flat": False, "remote_components": ["ejs:github"],
            "socket_timeout": 25, "retries": 4,
        }) as ydl:
            info = ydl.extract_info(normalize_url(url), download=False)
    except Exception:
        info = None
    if not isinstance(info, dict):
        return [], ""
    parent_caption = caption_from_info(info)
    entries = [e for e in (info.get("entries") or []) if isinstance(e, dict)]
    if not entries:
        entries = [info]
    items=[]
    seen=set()
    for idx, entry in enumerate(entries, 1):
        iu=_image_url_from_info(entry)
        kind="image"
        u=iu
        if not u:
            u=_video_url_from_info(entry)
            kind="video"
        if not u:
            continue
        if u in seen:
            continue
        seen.add(u)
        ext=os.path.splitext(urlparse(u).path)[1].lower()
        if kind=="image":
            ext=ext if ext in IMAGE_EXT else ".jpg"
            mime={".jpg":"image/jpeg",".jpeg":"image/jpeg",".png":"image/png",".webp":"image/webp"}.get(ext,"image/jpeg")
        else:
            ext=ext if ext in {".mp4",".webm",".mov",".mkv"} else ".mp4"
            mime={".mp4":"video/mp4",".webm":"video/webm",".mov":"video/quicktime",".mkv":"video/x-matroska"}.get(ext,"video/mp4")
        path=os.path.join(folder, f"media_{idx}{ext}")
        try:
            download_to(u,path)
            if os.path.getsize(path)<=0:
                continue
        except Exception:
            try: os.remove(path)
            except OSError: pass
            continue
        items.append({"path":path,"name":os.path.basename(path),"type":mime,"kind":kind,
                      "caption":caption_from_info(entry,parent_caption)})
    return items, parent_caption


# Backwards-compatible helper used by /download for single-media fallbacks.
def gallery_images(url: str, folder: str) -> list[str]:
    return [x["path"] for x in gallery_media(url, folder)[0] if x["kind"]=="image"]


def attempts_for(mode: str, url: str) -> list[dict]:
    if mode == "audio":
        formats = [{
            "format": "bestaudio/best",
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
        }]
    else:
        formats = [{"format": "bv*+ba/b", "format_sort": SORT},
                   {"format": "bv*[height<=1080]+ba/b[height<=1080]", "format_sort": SORT},
                   {"format": "bv*[height<=720]+ba/b[height<=720]", "format_sort": SORT},
                   {"format": "bv*[height<=480]+ba/b[height<=480]", "format_sort": SORT},
                   {"format": "best"}]
    if is_instagram(url):
        first = {"format": "b[ext=mp4]/bv*+ba/b", "format_sort": SORT} if mode == "video" else formats[0]
        out = [first, {**first, "extractor_args": {"instagram": {"app_id": ["ios"]}}}]
        if mode == "video":
            out.append(formats[-1])
        return out
    if is_snapchat(url):
        return [{"format": "best"}]
    if not is_youtube(url):
        if mode == "video":
            return [{"format": "b[ext=mp4]/bv*+ba/b", "format_sort": SORT}] + formats[1:]
        return formats

    # YouTube changes its player/challenge requirements frequently. Start with yt-dlp's
    # current default extractor, then try known lightweight clients, then smaller fallbacks.
    # Deno + yt-dlp-ejs are installed in the Render image and remote EJS updates are enabled.
    if mode == "audio":
        chain = [formats[0]]
        tiers = []
    else:
        chain = [{"format": "bv*+ba/b", "format_sort": YT_SORT}]
        tiers = [{"format": "bv*[height<=720]+ba/b[height<=720]", "format_sort": YT_SORT, "_lower": True},
                 {"format": "bv*[height<=480]+ba/b[height<=480]", "format_sort": YT_SORT, "_lower": True},
                 {"format": "best", "_lower": True}]
    out = [{**chain[0], "remote_components": ["ejs:github"]}]
    for client in ["android_vr", "tv", "web_safari", "mweb", "web_embedded", "ios", "android", "tv_simply", "web"]:
        out.append({**chain[0], "extractor_args": {"youtube": {"player_client": [client]}}, "remote_components": ["ejs:github"]})
    out.extend({**t, "remote_components": ["ejs:github"]} for t in tiers)
    return out


def pick_file(folder: str, mode: str) -> str | None:
    want = (".mp3",) if mode == "audio" else (".mp4", ".mkv", ".webm", ".mov")
    files = [f for f in os.listdir(folder) if f.lower().endswith(want) and not FRAGMENT.search(f)]
    if not files and mode == "video":  # photo posts: hand back the picture
        files = [f for f in os.listdir(folder) if f.lower().endswith(IMAGE_EXT) and not FRAGMENT.search(f)]
    if not files:
        return None
    return os.path.join(folder, max(files, key=lambda f: os.path.getsize(os.path.join(folder, f))))


def fetch(url: str, folder: str, mode: str = "video") -> str:
    url = normalize_url(url)
    base = {
        "outtmpl": os.path.join(folder, "%(title).80B [%(id)s].%(ext)s"),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "restrictfilenames": True,
        "max_filesize": MAX_MB * 1024 * 1024,
        "socket_timeout": 20,
        "quiet": True,
        "no_warnings": False,
        "retries": 3,
        "fragment_retries": 3,
        "remote_components": ["ejs:github"],
    }
    if OUT_PROXY:
        base["proxy"] = OUT_PROXY
    if is_youtube(url):
        base.update({"retries": 4, "fragment_retries": 5, "socket_timeout": 20, "concurrent_fragment_downloads": 2})
    cookie_path = None
    if COOKIES:
        fd, cookie_path = tempfile.mkstemp(prefix="ck_", suffix=".txt")  # yt-dlp rewrites its cookie file, so give it a private copy
        with os.fdopen(fd, "w") as fh:
            fh.write(COOKIES)
        try:
            yt_dlp.cookies.YoutubeDLCookieJar(cookie_path).load(ignore_discard=True, ignore_expires=True)
            base["cookiefile"] = cookie_path
        except Exception:  # unusable cookies must never block downloads
            pass
    insta = is_instagram(url)
    snap = is_snapchat(url)
    last = None
    try:
        if mode == "video" and (is_instagram(url) or is_tiktok(url)):
            gallery_items, gallery_caption = gallery_media(url, folder)
            if gallery_items:
                write_caption(folder, gallery_items[0].get("caption") or gallery_caption)
                # Keep /download backward compatible: return the first actual media item.
                return gallery_items[0]["path"]
        if snap and mode == "video":
            try:
                return snapchat_fallback(url, folder, mode)
            except Exception as e:
                last = e
        capped = False  # a download that finished without a file means the size cap was hit
        started, blocked = time.monotonic(), 0
        for extra in attempts_for(mode, url):
            if last is not None and time.monotonic() - started > BUDGET:
                break  # stop trying new methods; the visitor gets the last reason instead of an endless spinner
            lower = bool(extra.get("_lower"))
            extra = {k: v for k, v in extra.items() if k != "_lower"}
            if capped and not lower and is_youtube(url):
                continue  # too big: jump straight to the smaller versions
            if lower and not capped and "requested format" not in str(last or "").lower():
                continue  # smaller versions only help with size or format problems, not with blocks
            clear_folder(folder)
            try:
                with yt_dlp.YoutubeDL({**base, **extra}) as ydl:
                    info = ydl.extract_info(url, download=True)
                    write_caption(folder, caption_from_info(info))
            except Exception as e:
                last = e
                if insta:  # Instagram is flaky from servers: always try the next strategy
                    continue
                if not isinstance(e, yt_dlp.utils.DownloadError):
                    raise
                msg = str(e).lower()
                if is_youtube(url):
                    if any(x in msg for x in YT_FINAL):
                        raise  # private / removed / copyright: no other client can help
                    if "sign in to confirm" in msg or "not a bot" in msg:
                        blocked += 1
                    continue
                if mode == "audio" or "requested format" not in msg:
                    raise
                continue
            path = pick_file(folder, mode)
            if path:
                return path
            capped = True
        if insta:
            clear_folder(folder)
            try:
                return instagram_fallback(url, folder, mode)
            except Exception as e:
                if last is None:
                    last = e
        if last:
            raise last
        raise RuntimeError("no file produced (it may exceed the size limit)")
    finally:
        if cookie_path:
            try:
                os.remove(cookie_path)
            except OSError:
                pass


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/gallery")
async def gallery(req: Req, request: Request):
    if PROXY_SECRET and not from_proxy(request):
        raise HTTPException(403, "Use the Toolora website to download.")
    ip = (request.headers.get("x-client-ip") or "?") if from_proxy(request) else (request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?"))
    check_rate(ip)
    if not valid_url(req.url) or not (is_tiktok(req.url) or is_instagram(req.url)):
        raise HTTPException(400, "Paste a TikTok or Instagram link.")
    folder = tempfile.mkdtemp(prefix="gallery_")
    try:
        async with slots:
            media, post_caption = await asyncio.to_thread(gallery_media, req.url.strip(), folder)
        if not media:
            raise HTTPException(422, "No photos or videos were found in that post.")
        items=[]
        for item in media:
            path=item["path"]
            with open(path,"rb") as fh:
                data=base64.b64encode(fh.read()).decode("ascii")
            items.append({"name":item["name"],"type":item["type"],"kind":item["kind"],
                          "caption":item.get("caption") or post_caption,"data":data})
        return {"items":items,"caption":post_caption}
    except HTTPException:
        raise
    except Exception as e:
        print(f"[toolora] gallery failed {req.url.strip()[:120]}: {ANSI.sub('', str(e))[:600]}", flush=True)
        raise HTTPException(422, f"Couldn't fetch the media: {clean_error(e)}")
    finally:
        shutil.rmtree(folder, ignore_errors=True)


@app.post("/download")
async def download(req: Req, request: Request):
    if PROXY_SECRET and not from_proxy(request):
        raise HTTPException(403, "Use the Toolora website to download.")
    if from_proxy(request):
        ip = request.headers.get("x-client-ip") or "?"  # the real visitor, as seen by the worker
    else:
        ip = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")
    check_rate(ip)
    if not valid_url(req.url):
        raise HTTPException(400, "Paste a link from YouTube, TikTok, Instagram, Snapchat or X (Twitter).")
    folder = tempfile.mkdtemp(prefix="dl_")
    cleanup = BackgroundTask(shutil.rmtree, folder, ignore_errors=True)
    try:
        async with slots:
            path = await asyncio.to_thread(fetch, req.url.strip(), folder, req.mode)
    except Exception as e:
        shutil.rmtree(folder, ignore_errors=True)
        print(f"[toolora] download failed ({req.mode}) {req.url.strip()[:120]}: {ANSI.sub('', str(e))[:600]}", flush=True)
        reason = clean_error(e)
        what = "extract the audio from" if req.mode == "audio" else "download"
        raise HTTPException(422, f"Couldn't {what} that video: {reason}")
    ext = os.path.splitext(path)[1].lower()
    media = {".mp3": "audio/mpeg", ".mp4": "video/mp4", ".webm": "video/webm", ".mkv": "video/x-matroska", ".mov": "video/quicktime", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}.get(ext, "application/octet-stream")
    caption = read_caption(folder)
    headers = {"X-Toolora-Caption": caption} if caption else {}
    return FileResponse(path, media_type=media, filename=os.path.basename(path), headers=headers, background=cleanup)


# Optional: if a "static" folder sits next to this file, serve it as the website (handy for local testing).
# In production the site is served by the Cloudflare worker, so the backend runs without this folder.
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(STATIC_DIR):
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="site")
