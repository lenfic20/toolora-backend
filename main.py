"""Toolora backend: POST /download {"url": "...", "mode": "video" | "audio"} -> returns the file.
  mode "video" (default): best quality MP4 that fits the size cap.
  mode "audio": the sound only, as an MP3.
  mode "info":  TikTok / Instagram only. Returns {"items": [...]} listing every picture / video of a post, so a photo
                slideshow or mixed carousel can be shown. An empty list means "treat it as a normal single video".
  mode "file":  fetches one picture / video file by its direct CDN link (only well-known media hosts are allowed).

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
  YT_PROXY          (optional) proxy URL used ONLY for YouTube (overrides YTDLP_PROXY for YouTube links), so a paid
                    residential proxy is only used where it is needed
"""
import asyncio, base64, hmac, html, json, os, re, shutil, subprocess, tempfile, time
import urllib.error, urllib.request
from typing import Literal
from collections import defaultdict, deque
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

import yt_dlp
import yt_dlp.cookies
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask

ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
MAX_MB = int(os.getenv("MAX_FILESIZE_MB", "200"))
RATE = int(os.getenv("RATE_PER_MINUTE", "10"))
PROXY_SECRET = os.getenv("PROXY_SECRET", "")
OUT_PROXY = os.getenv("YTDLP_PROXY", "").strip()
YT_PROXY = os.getenv("YT_PROXY", "").strip()
BUDGET = float(os.getenv("MAX_SECONDS", "80"))
slots = asyncio.Semaphore(int(os.getenv("MAX_CONCURRENT", "3")))
file_slots = asyncio.Semaphore(4)  # picture / slide file transfers (light, so they do not wait behind video downloads)

# Only these sites (and their subdomains) are accepted; this also blocks SSRF to internal hosts.
ALLOWED_HOSTS = ("youtube.com", "youtu.be", "tiktok.com", "instagram.com", "instagr.am", "twitter.com", "x.com", "snapchat.com")

app = FastAPI(title="Toolora backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGINS,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],  # the frontend reads the filename from this header
)

hits: dict[str, deque] = defaultdict(deque)
fhits: dict[str, deque] = defaultdict(deque)  # slide info / picture file requests have their own, more generous allowance


def from_proxy(request: Request) -> bool:
    """True when the request carries the shared secret that only the Toolora worker knows."""
    if not PROXY_SECRET:
        return False
    sent = request.headers.get("x-proxy-secret", "")
    return hmac.compare_digest(sent.encode(), PROXY_SECRET.encode())


def check_rate(ip: str, bucket=None, limit: int | None = None) -> None:
    now, q = time.time(), (hits if bucket is None else bucket)[ip]
    limit = RATE if limit is None else limit
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(429, "Too many requests. Try again in a minute.")
    q.append(now)


class Req(BaseModel):
    url: str
    mode: Literal["video", "audio", "info", "file"] = "video"


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
    if is_tiktok(url):  # yt-dlp only knows /video/ links; photo posts are the same post under /photo/
        return re.sub(r"(tiktok\.com/@[^/?#]+)/photo/", r"\1/video/", url, flags=re.I)
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


def is_tiktok(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "tiktok.com" or host.endswith(".tiktok.com")


def is_snapchat(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "snapchat.com" or host.endswith(".snapchat.com")


# Hosts that picture / video files may be fetched from (this also blocks SSRF through the "file" mode).
MEDIA_HOSTS = ("tiktokcdn.com", "tiktokcdn-us.com", "tiktokv.com", "tiktokv.us", "byteoversea.com", "ibytedtos.com",
               "ibyteimg.com", "muscdn.com", "tikwm.com", "cdninstagram.com", "fbcdn.net", "sc-cdn.net", "snapchat.com")


def media_ok(u: str) -> bool:
    try:
        p = urlparse(u)
    except ValueError:
        return False
    host = (p.hostname or "").lower()
    return p.scheme == "https" and any(host == h or host.endswith("." + h) for h in MEDIA_HOSTS)


class _CheckRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not media_ok(newurl):
            raise urllib.error.URLError("redirect to a host that is not allowed")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_media(u: str, path: str) -> str:
    """Download one allowed media URL to `path`; returns the Content-Type. Every redirect target is re-checked."""
    if not media_ok(u):
        raise RuntimeError("that file can't be fetched")
    host = (urlparse(u).hostname or "").lower()
    referer = ("https://www.tiktok.com/" if any(x in host for x in ("tiktok", "byte", "muscdn", "tikwm"))
               else "https://www.snapchat.com/" if ("snap" in host or "sc-cdn" in host) else "https://www.instagram.com/")
    handlers = [_CheckRedirect()]
    if OUT_PROXY:
        handlers.append(urllib.request.ProxyHandler({"http": OUT_PROXY, "https": OUT_PROXY}))
    req = urllib.request.Request(u, headers={"User-Agent": UA, "Referer": referer, "Accept": "*/*"})
    limit = MAX_MB * 1024 * 1024
    got = 0
    with urllib.request.build_opener(*handlers).open(req, timeout=45) as r, open(path, "wb") as fh:
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
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
    return ctype


MEDIA_EXT = {"image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
             "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm"}


def fetch_media_file(u: str, folder: str) -> tuple[str, str]:
    tmp = os.path.join(folder, "media.part")
    ctype = download_media(u, tmp)
    ext = MEDIA_EXT.get(ctype) or os.path.splitext(urlparse(u).path)[1].lower()
    if ext not in MEDIA_EXT.values():
        ext = ".mp4" if ctype.startswith("video/") else ".jpg"
    if not ctype or ctype == "application/octet-stream":
        ctype = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif", ".mov": "video/quicktime", ".webm": "video/webm"}.get(ext, "video/mp4")
    path = os.path.join(folder, "media" + ext)
    os.replace(tmp, path)
    return path, ctype


# ---- photo slides / mixed carousels (TikTok and Instagram) ----
INFO_CACHE: dict[str, tuple[float, list]] = {}


def tiktok_item(text: str):
    """The post's data object from a TikTok page, or None when the page has none (blocked / changed)."""
    m = re.search(r'<script[^>]+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', text, re.S)
    if m:
        try:
            d = json.loads(m.group(1))
            it = ((((d.get("__DEFAULT_SCOPE__") or {}).get("webapp.video-detail") or {}).get("itemInfo") or {}).get("itemStruct"))
            if isinstance(it, dict):
                return it
        except (ValueError, AttributeError):
            pass
    m = re.search(r'<script[^>]+id="SIGI_STATE"[^>]*>(.*?)</script>', text, re.S)
    if m:
        try:
            mod = json.loads(m.group(1)).get("ItemModule") or {}
            for it in mod.values():
                if isinstance(it, dict):
                    return it
        except (ValueError, AttributeError):
            pass
    return None


def tiktok_items_from(item: dict) -> list[dict]:
    imgs = ((item.get("imagePost") or {}).get("images")) or []
    out = []
    for im in imgs:
        urls = [u for u in (((im or {}).get("imageURL") or {}).get("urlList") or []) if isinstance(u, str) and media_ok(u)]
        if urls:
            out.append({"kind": "image", "src": next((u for u in urls if re.search(r"\.jpe?g(\?|$)", u)), urls[0])})
    vid = item.get("video") or {}
    if out and (vid.get("duration") or 0) > 0 and (vid.get("playAddr") or vid.get("downloadAddr")):
        out.append({"kind": "video", "via": "page"})  # the post also carries a real video: fetched with the normal video route
    return out


def tikwm_items(url: str) -> list[dict]:
    """Second opinion used only when TikTok's own page can't be read from this server."""
    req = urllib.request.Request("https://www.tikwm.com/api/?hd=1&url=" + quote(url, safe=""), headers={"User-Agent": UA})
    with open_url(req, 20) as r:
        data = json.loads(r.read(2_000_000).decode("utf-8", "replace"))
    d = data.get("data") if isinstance(data, dict) and data.get("code") == 0 else None
    return [{"kind": "image", "src": u} for u in ((d or {}).get("images") or []) if isinstance(u, str) and media_ok(u)]


def tiktok_items(url: str) -> list[dict]:
    text = page_text(url)
    item = tiktok_item(text) if text else None
    if item is not None:
        return tiktok_items_from(item)
    return tikwm_items(url)


def _cookie_copy() -> str | None:
    if not COOKIES:
        return None
    fd, path = tempfile.mkstemp(prefix="ck_", suffix=".txt")
    with os.fdopen(fd, "w") as fh:
        fh.write(COOKIES)
    try:
        yt_dlp.cookies.YoutubeDLCookieJar(path).load(ignore_discard=True, ignore_expires=True)
        return path
    except Exception:
        os.remove(path)
        return None


def instagram_items(url: str) -> list[dict]:
    items: list[dict] = []
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": False, "socket_timeout": 15,
            "retries": 1, "format": "b/best", "ignoreerrors": True}
    if OUT_PROXY:
        opts["proxy"] = OUT_PROXY
    ck = _cookie_copy()
    if ck:
        opts["cookiefile"] = ck
    try:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            entries = list(info.get("entries") or [info]) if info else []
            for e in entries:
                u = (e or {}).get("url")
                if not u or not media_ok(u):
                    continue
                is_img = (e.get("ext") or "").lower() in ("jpg", "jpeg", "png", "webp") or urlparse(u).path.lower().endswith(IMAGE_EXT)
                items.append({"kind": "image" if is_img else "video", "src": u})
        except Exception as e:
            print(f"[toolora] instagram info (yt-dlp) failed: {ANSI.sub('', str(e))[:300]}", flush=True)
    finally:
        if ck:
            try:
                os.remove(ck)
            except OSError:
                pass
    if not items:  # public embed page: only trusted when it shows pictures only (video posts stay on the normal route)
        m = re.search(r"instagram\.com/(?:[^/?#]+/)?(?:p|reels?|tv)/([A-Za-z0-9_-]+)", url)
        text = page_text(f"https://www.instagram.com/p/{m.group(1)}/embed/captioned/") if m else ""
        if text:
            v, i = ig_media_urls(text)
            if not [x for x in v if cdn_ok(x)]:
                seen = set()
                for x in i:
                    key = urlparse(x).path
                    if cdn_ok(x) and key not in seen:
                        seen.add(key)
                        items.append({"kind": "image", "src": x})
    return items


def info_items(url: str) -> list[dict]:
    key = url.strip()
    hit = INFO_CACHE.get(key)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    url = normalize_url(key)
    try:
        items = instagram_items(url) if is_instagram(url) else tiktok_items(url)
    except Exception as e:
        print(f"[toolora] slides info failed {key[:120]}: {ANSI.sub('', str(e))[:300]}", flush=True)
        items = []
    if items:
        if len(INFO_CACHE) > 200:
            INFO_CACHE.clear()
        INFO_CACHE[key] = (time.time(), items)
    return items


# ---- Snapchat Spotlight (no watermark: the public page serves the original video) ----
def snap_resolve(url: str) -> str:
    """snapchat.com/t/XXXX short links redirect to the real /spotlight/... page."""
    if not urlparse(url).path.startswith("/t/"):
        return url
    try:
        from curl_cffi import requests as cr
        kw = {"proxies": {"http": OUT_PROXY, "https": OUT_PROXY}} if OUT_PROXY else {}
        r = cr.get(url, headers={"Accept-Language": "en-US,en;q=0.9"}, impersonate="chrome", timeout=20, allow_redirects=True, **kw)
        final = str(r.url)
    except Exception:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with open_url(req, 20) as r:
                final = r.geturl()
        except Exception:
            return url
    return final if is_snapchat(final) else url


def snap_video_urls(page: str) -> list[str]:
    t = re.sub(r"\\+u002[Ff]", "/", _unesc(page))  # Next.js pages write "/" as \u002F
    found = re.findall(r'<meta[^>]+property=["\']og:video(?::secure_url|:url)?["\'][^>]+content=["\'](https?://[^"\']+)', t)
    found += re.findall(r'<meta[^>]+content=["\'](https?://[^"\']+)["\'][^>]+property=["\']og:video', t)
    found += re.findall(r'"contentUrl"\s*:\s*"(https?://[^"]+)"', t)
    found += [x for x in re.findall(r'(https?://[A-Za-z0-9.-]*sc-cdn\.net/[^"\'\s<>\\]+)', t) if ".mp4" in urlparse(x).path.lower()]
    return [x for x in _uniq(found) if media_ok(x)]


def snapchat_fallback(url: str, folder: str, mode: str) -> str:
    text = page_text(url)
    if not text:
        raise RuntimeError("Snapchat didn't return the page for this link")
    last = None
    for u in snap_video_urls(text):
        path = os.path.join(folder, "snapchat_spotlight.mp4")
        try:
            ctype = download_media(u, path)
            if ctype.startswith("image/"):
                os.remove(path)
                continue
            return to_mp3(path, folder, "snapchat_spotlight") if mode == "audio" else path
        except Exception as e:
            last = e
    raise last or RuntimeError("couldn't find a video on that Snapchat page. Use a public Spotlight link.")


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
        # Default web API first, then the iOS-app API, then plain "best" as a last format.
        first = {"format": "b[ext=mp4]/bv*+ba/b", "format_sort": SORT} if mode == "video" else formats[0]
        out = [first, {**first, "extractor_args": {"instagram": {"app_id": ["ios"]}}}]
        if mode == "video":
            out.append(formats[-1])
        return out
    if not is_youtube(url):
        if mode == "video":  # TikTok / X / others serve ready-made MP4s: take one, skip the slow audio+video merge
            return [{"format": "b[ext=mp4]/bv*+ba/b", "format_sort": SORT}] + formats[1:]
        return formats

    # YouTube's extractor changes frequently, so try each client once with one format chain (fast failures).
    if mode == "audio":
        chain = [formats[0]]
        tiers = []
    else:
        chain = [{"format": "bv*+ba/b", "format_sort": YT_SORT}]  # best up to 1080p, H.264 + AAC, so it plays everywhere and stays under the size cap
        tiers = [{"format": "bv*[height<=720]+ba/b[height<=720]", "format_sort": YT_SORT, "_lower": True},
                 {"format": "bv*[height<=480]+ba/b[height<=480]", "format_sort": YT_SORT, "_lower": True},
                 {"format": "best", "_lower": True}]
    # Clients that need no JavaScript challenge solving go first: they answer in a second or two even on a small
    # server, while the web clients can take a minute there. yt-dlp's own default mix goes last as the catch-all.
    clients = ["android_vr", "visionos", "tv", "web_safari", "mweb", "web_embedded", "ios", "android", "tv_simply", "web"]
    out = []
    for client in clients:
        out.append({**chain[0], "extractor_args": {"youtube": {"player_client": [client]}}, "remote_components": ["ejs:github"]})
    out.append({**chain[0], "remote_components": ["ejs:github"]})
    out.extend({**t, "remote_components": ["ejs:github"]} for t in tiers)  # smaller versions, used if the big one is over the size cap
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
    if is_snapchat(url):
        url = snap_resolve(url)
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
    if is_youtube(url) and YT_PROXY:
        base["proxy"] = YT_PROXY
    elif OUT_PROXY:
        base["proxy"] = OUT_PROXY
    if is_youtube(url):
        base.update({"retries": 1, "fragment_retries": 2, "socket_timeout": 15})
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
                    ydl.download([url])
            except Exception as e:
                last = e
                if insta or snap:  # Instagram is flaky from servers: always try the next strategy
                    continue
                if not isinstance(e, yt_dlp.utils.DownloadError):
                    raise
                msg = str(e).lower()
                if is_youtube(url):
                    if any(x in msg for x in YT_FINAL):
                        raise  # private / removed / copyright: no other client can help
                    if "sign in to confirm" in msg or "not a bot" in msg:
                        blocked += 1
                        if blocked >= 4:
                            raise  # YouTube is blocking this server's address; more clients won't change that
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
        if snap:
            clear_folder(folder)
            try:
                return snapchat_fallback(url, folder, mode)
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


@app.post("/download")
async def download(req: Req, request: Request):
    if PROXY_SECRET and not from_proxy(request):
        raise HTTPException(403, "Use the Toolora website to download.")
    if from_proxy(request):
        ip = request.headers.get("x-client-ip") or "?"  # the real visitor, as seen by the worker
    else:
        ip = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")
    if req.mode in ("info", "file"):
        check_rate(ip, fhits, RATE * 12)
    else:
        check_rate(ip)
    link = req.url.strip()

    if req.mode == "file":  # one picture / video of a slide post, by its direct media link
        if not media_ok(link):
            raise HTTPException(400, "That file can't be fetched.")
        folder = tempfile.mkdtemp(prefix="dl_")
        cleanup = BackgroundTask(shutil.rmtree, folder, ignore_errors=True)
        try:
            async with file_slots:
                path, ctype = await asyncio.to_thread(fetch_media_file, link, folder)
        except Exception as e:
            shutil.rmtree(folder, ignore_errors=True)
            print(f"[toolora] file fetch failed {link[:120]}: {ANSI.sub('', str(e))[:300]}", flush=True)
            raise HTTPException(422, f"Couldn't fetch that file: {clean_error(e)}")
        return FileResponse(path, media_type=ctype, filename=os.path.basename(path), background=cleanup)

    if not valid_url(link):
        raise HTTPException(400, "Paste a link from YouTube, TikTok, Instagram, X (Twitter) or Snapchat.")

    if req.mode == "info":  # never an error: an empty list simply means "use the normal single-video download"
        items: list = []
        if is_instagram(link) or is_tiktok(link):
            try:
                async with slots:
                    items = await asyncio.wait_for(asyncio.to_thread(info_items, link), 45)
            except Exception as e:
                print(f"[toolora] slides info failed {link[:120]}: {ANSI.sub('', str(e))[:300]}", flush=True)
                items = []
        return JSONResponse({"items": items}, headers={"Cache-Control": "no-store"})

    folder = tempfile.mkdtemp(prefix="dl_")
    cleanup = BackgroundTask(shutil.rmtree, folder, ignore_errors=True)
    try:
        async with slots:
            path = await asyncio.to_thread(fetch, link, folder, req.mode)
    except Exception as e:
        shutil.rmtree(folder, ignore_errors=True)
        print(f"[toolora] download failed ({req.mode}) {link[:120]}: {ANSI.sub('', str(e))[:600]}", flush=True)
        reason = clean_error(e)
        what = "extract the audio from" if req.mode == "audio" else "download"
        raise HTTPException(422, f"Couldn't {what} that video: {reason}")
    ext = os.path.splitext(path)[1].lower()
    media = {".mp3": "audio/mpeg", ".mp4": "video/mp4", ".webm": "video/webm", ".mkv": "video/x-matroska", ".mov": "video/quicktime", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}.get(ext, "application/octet-stream")
    return FileResponse(path, media_type=media, filename=os.path.basename(path), background=cleanup)


# Optional: if a "static" folder sits next to this file, serve it as the website (handy for local testing).
# In production the site is served by the Cloudflare worker, so the backend runs without this folder.
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(STATIC_DIR):
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="site")
