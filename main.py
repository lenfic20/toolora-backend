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
  RAPIDAPI_KEY      RapidAPI key. YouTube downloads (MP3 and video) use RapidAPI only; yt-dlp serves the other platforms.
                    Also: RAPIDAPI_HOST, RAPIDAPI_MP3_QUALITY (default low), RAPIDAPI_DAILY_LIMIT (default 200),
                    RAPIDAPI_VIDEO_HOST, RAPIDAPI_VIDEO_DAILY_LIMIT (default 100), RAPIDAPI_HIDE_ERRORS=1 (hide technical reasons),
                    RAPIDAPI_TEST_TOKEN (turns on the owner-only /rapid-test check).
  COOKIES_TXT       (optional) browser cookies in Netscape cookies.txt format. Lets Instagram serve the server
                    like a logged-in browser, which fixes "login required" blocks.
                    Alternatives: COOKIES_B64 (the same file, base64-encoded) or a Render Secret File named cookies.txt
  MAX_SECONDS       give up on a link after this many seconds of trying different methods (default 80)
  YTDLP_PROXY       (optional) proxy URL such as http://user:pass@host:port, used for every outgoing request
"""
import asyncio, base64, hmac, html, json, os, re, shutil, subprocess, tempfile, time
import ipaddress, socket, urllib.error, urllib.request
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
BUDGET = float(os.getenv("MAX_SECONDS", "80"))
slots = asyncio.Semaphore(int(os.getenv("MAX_CONCURRENT", "3")))
file_slots = asyncio.Semaphore(4)  # picture / slide file transfers (light, so they do not wait behind video downloads)

# Only these sites (and their subdomains) are accepted; this also blocks SSRF to internal hosts.
ALLOWED_HOSTS = ("youtube.com", "youtu.be", "tiktok.com", "instagram.com", "instagr.am", "twitter.com", "x.com", "snapchat.com", "pinterest.com", "pin.it")

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
    quality: str | None = None


def valid_url(u: str) -> bool:
    try:
        p = urlparse(u.strip())
    except ValueError:
        return False
    host = (p.hostname or "").lower()
    pinterest_host = bool(re.fullmatch(r"(?:[a-z0-9-]+\.)*pinterest\.(?:com|[a-z]{2,3}|co\.[a-z]{2}|com\.[a-z]{2})", host))
    return p.scheme in ("http", "https") and (pinterest_host or any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS))


SORT = ["res", "ext:mp4:m4a"]  # best resolution, preferring MP4/M4A so the result plays on phones
FRAGMENT = re.compile(r"\.f[\w-]+\.[A-Za-z0-9]+$")  # intermediate streams such as "name.f137.mp4"
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


def is_pinterest(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return bool(re.fullmatch(r"(?:[a-z0-9-]+\.)*pinterest\.(?:com|[a-z]{2,3}|co\.[a-z]{2}|com\.[a-z]{2})", host) or host == "pin.it" or host.endswith(".pin.it"))


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
        return re.sub(r"(tiktok\.com/@[^/?#]+)/photo/", r"\1/video/", tiktok_resolve(url), flags=re.I)
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
    if "this content isn't available to everyone" in low or "this content is not available to everyone" in low:
        msg = ("Instagram restricts this post to certain audiences. If the post is visible in your own Instagram account, "
               "configure a valid COOKIES_TXT value on the backend using cookies from an account authorized to view it, "
               "then redeploy. If that account cannot view the post, Toolora cannot download it.")
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
MEDIA_HOSTS = ("pinimg.com", "tiktokcdn.com", "tiktokcdn-us.com", "tiktokcdn-eu.com", "tiktokv.com", "tiktokv.us", "byteoversea.com", "ibytedtos.com",
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
    referer = ("https://www.pinterest.com/" if "pinimg.com" in host
               else "https://www.tiktok.com/" if any(x in host for x in ("tiktok", "byte", "muscdn", "tikwm"))
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
        if urls:  # prefer JPEG, then anything that is not HEIC (browsers can't show HEIC)
            best = (next((u for u in urls if re.search(r"\.jpe?g(\?|$)", urlparse(u).path + "?")), None)
                    or next((u for u in urls if "heic" not in u.lower()), None) or urls[0])
            out.append({"kind": "image", "src": best})
    # TikTok photo posts never contain a separate video (the "video" field only holds the soundtrack),
    # so no extra video slide is added: it could only fail.
    return out


def tikwm_items(url: str) -> list[dict]:
    """Second opinion used only when TikTok's own page can't be read from this server."""
    req = urllib.request.Request("https://www.tikwm.com/api/?hd=1&url=" + quote(url, safe=""), headers={"User-Agent": UA})
    with open_url(req, 20) as r:
        data = json.loads(r.read(2_000_000).decode("utf-8", "replace"))
    d = data.get("data") if isinstance(data, dict) and data.get("code") == 0 else None
    return [{"kind": "image", "src": u} for u in ((d or {}).get("images") or []) if isinstance(u, str) and media_ok(u)]


TT_RESOLVED: dict[str, str] = {}


def tiktok_resolve(url: str) -> str:
    """Short share links (tiktok.com/t/..., vm./vt.tiktok.com) -> the real https://www.tiktok.com/@user/(video|photo)/<id> link."""
    url = url.strip()
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if re.search(r"/@[^/?#]+/(?:video|photo)/\d+", p.path or ""):
        return url
    if not (host.startswith(("vm.", "vt.")) or (p.path or "").startswith("/t/")):
        return url
    if url in TT_RESOLVED:
        return TT_RESOLVED[url]
    final = url
    for _ in range(5):  # follow the redirects ourselves so the Location header is always seen
        try:
            handlers = [_NoRedirect]
            if OUT_PROXY:
                handlers.append(urllib.request.ProxyHandler({"http": OUT_PROXY, "https": OUT_PROXY}))
            urllib.request.build_opener(*handlers).open(
                urllib.request.Request(final, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}), timeout=15).close()
            break
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location") if e.headers else None
            if e.code not in (301, 302, 303, 307, 308) or not loc:
                break
            final = urljoin(final, loc)
            if re.search(r"/@[^/?#]+/(?:video|photo)/\d+", urlparse(final).path or ""):
                break
        except Exception:
            break
    m = re.search(r"/(@[^/?#]+)/(video|photo)/(\d+)", urlparse(final).path or "")
    if not m:
        return url
    out = f"https://www.tiktok.com/{m.group(1)}/{m.group(2)}/{m.group(3)}"
    if len(TT_RESOLVED) > 500:
        TT_RESOLVED.clear()
    TT_RESOLVED[url] = out
    return out


def tiktok_ytdlp_item(url: str):
    """The post's data object read through yt-dlp's TikTok extractor (works when the plain page is blocked)."""
    m = re.search(r"/@[^/?#]+/(?:video|photo)/(\d+)", url)
    if not m:
        return None
    opts = {"quiet": True, "no_warnings": True, "socket_timeout": 15}
    if OUT_PROXY:
        opts["proxy"] = OUT_PROXY
    with yt_dlp.YoutubeDL(opts) as ydl:
        ie = ydl.get_info_extractor("TikTok")
        data, _ = ie._extract_web_data_and_status(re.sub(r"/photo/", "/video/", url), m.group(1))
    return data if isinstance(data, dict) else None


def tiktok_items(url: str) -> list[dict]:
    url = tiktok_resolve(url)
    is_photo = "/photo/" in url.lower()
    pages = [url]
    if is_photo:  # TikTok often leaves the post data out of the /photo/ page; the /video/ page of the same post has it
        pages.append(re.sub(r"/photo/", "/video/", url))
    for pu in pages:
        text = page_text(pu)
        item = tiktok_item(text) if text else None
        if item is not None:
            out = tiktok_items_from(item)
            if out or not (item.get("imagePost") or is_photo):
                return out  # a normal video post: empty list = use the normal video route
    try:  # third source: yt-dlp's TikTok reader
        item = tiktok_ytdlp_item(url)
        if item is not None:
            out = tiktok_items_from(item)
            if out or not (item.get("imagePost") or is_photo):
                return out
    except Exception as e:
        print(f"[toolora] tiktok yt-dlp info failed {url[:120]}: {ANSI.sub('', str(e))[:200]}", flush=True)
    for i in range(2):  # last source: tikwm (free tier allows about 1 request per second, so retry once)
        try:
            out = tikwm_items(url)
            if out:
                return out
        except Exception as e:
            print(f"[toolora] tikwm failed {url[:120]}: {ANSI.sub('', str(e))[:200]}", flush=True)
        if i == 0:
            time.sleep(1.5)
    return []


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
    try:  # TikTok keeps the original link: its /photo/ page is the one that lists the pictures
        items = instagram_items(url) if is_instagram(url) else tiktok_items(key)
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


SNAP_VARIANT = re.compile(r"\.\d+\.(IRZXSOY)$")
SNAP_CLEAN = "1034"  # Snapchat's media variant without the burned-in "Snapchat @user" watermark (".27." carries it)


def snap_clean_urls(u: str) -> list[str]:
    """Watermark-free variants of a Spotlight media link (empty when the link has no variant number).
    The same file is tried on its own host first, then on Snapchat's main CDN, which serves it for every Spotlight."""
    p = urlparse(u)
    if not SNAP_VARIANT.search(p.path):
        return []
    path = SNAP_VARIANT.sub(r".%s.\1" % SNAP_CLEAN, p.path)
    name = path.rsplit("/", 1)[-1]
    return _uniq([f"https://{p.hostname}{path}", f"https://cf-st.sc-cdn.net/d/{name}"])


def snap_story_urls(page: str, url: str) -> list[str]:
    """Media links of the Spotlight that `url` points to, read from the page's Next.js data."""
    m = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    if not m:
        return []
    try:
        props = json.loads(m.group(1))["props"]["pageProps"]
    except (ValueError, KeyError, TypeError):
        return []
    want = re.search(r"/spotlight/([\w-]+)", urlparse(url).path)
    want = want.group(1) if want else None
    stories = ((props.get("spotlightFeed") or {}).get("spotlightStories")) or []
    picked = [s for s in stories if want and (((s or {}).get("story") or {}).get("storyId") or {}).get("value") == want]
    if not picked and not want:
        picked = [s for s in stories if ((s or {}).get("story") or {}).get("snapList")][:1]
    out = []
    for s in picked:
        for snap in ((s.get("story") or {}).get("snapList") or []):
            mu = ((snap or {}).get("snapUrls") or {}).get("mediaUrl")
            if isinstance(mu, str):
                out.append(mu)
        cu = (((s.get("metadata") or {}).get("videoMetadata") or {}).get("contentUrl"))
        if isinstance(cu, str):
            out.append(cu)
    return [x for x in _uniq(out) if media_ok(x)]


def snap_video_urls(page: str) -> list[str]:
    t = re.sub(r"\\+u002[Ff]", "/", _unesc(page))  # Next.js pages write "/" as \u002F
    found = re.findall(r'<meta[^>]+property=["\']og:video(?::secure_url|:url)?["\'][^>]+content=["\'](https?://[^"\']+)', t)
    found += re.findall(r'<meta[^>]+content=["\'](https?://[^"\']+)["\'][^>]+property=["\']og:video', t)
    found += re.findall(r'"contentUrl"\s*:\s*"(https?://[^"]+)"', t)
    found += [x for x in re.findall(r'(https?://[A-Za-z0-9.-]*sc-cdn\.net/[^"\'\s<>\\]+)', t) if ".mp4" in urlparse(x).path.lower()]
    return [x for x in _uniq(found) if media_ok(x)]


def snapchat_fallback(url: str, folder: str, mode: str, clean_only: bool = False) -> str:
    """Download a Spotlight. Watermark-free variants are always tried first; with clean_only, nothing else."""
    text = page_text(url)
    if not text:
        raise RuntimeError("Snapchat didn't return the page for this link")
    raw = snap_story_urls(text, url) or snap_video_urls(text)
    clean = _uniq([c for u in raw for c in snap_clean_urls(u) if media_ok(c)])
    cands = clean if clean_only else _uniq(clean + raw)
    last = None
    for u in cands:
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
    if mode == "video":  # TikTok / X / others serve ready-made MP4s: take one, skip the slow audio+video merge
        return [{"format": "b[ext=mp4]/bv*+ba/b", "format_sort": SORT}] + formats[1:]
    return formats


def pick_file(folder: str, mode: str) -> str | None:
    want = (".mp3",) if mode == "audio" else (".mp4", ".mkv", ".webm", ".mov")
    files = [f for f in os.listdir(folder) if f.lower().endswith(want) and not FRAGMENT.search(f)]
    if not files and mode == "video":  # photo posts: hand back the picture
        files = [f for f in os.listdir(folder) if f.lower().endswith(IMAGE_EXT) and not FRAGMENT.search(f)]
    if not files:
        return None
    return os.path.join(folder, max(files, key=lambda f: os.path.getsize(os.path.join(folder, f))))


def pinterest_fallback(url: str, folder: str) -> str:
    """Fetch a public Pinterest Pin's direct video or image when yt-dlp cannot extract it."""
    page = re.sub(r"\\+u002[Ff]", "/", _unesc(page_text(url)))
    if not page:
        raise RuntimeError("Pinterest did not return the Pin page. It may be private or temporarily unavailable.")

    candidates = []
    # Explicit media metadata first; support either attribute order.
    for prop in ("og:video:secure_url", "og:video", "og:video:url", "twitter:player:stream", "og:image:secure_url", "og:image", "twitter:image"):
        patterns = (
            rf'<meta[^>]+property=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']+)',
            rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']{re.escape(prop)}["\']',
            rf'<meta[^>]+name=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']+)',
            rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']{re.escape(prop)}["\']',
        )
        for pattern in patterns:
            candidates.extend(re.findall(pattern, page, re.I))
    # Pinterest often embeds direct CDN media URLs in serialized page data.
    candidates.extend(re.findall(r'https?:\\?/\\?/[^"\'\s<>]+(?:pinimg\.com|pinterest\.com)[^"\'\s<>]*', page, re.I))
    candidates = [x.replace(r'\/', '/').replace(r'\u0026', '&').rstrip('),;') for x in candidates]
    candidates = _uniq([urljoin(url, html.unescape(x)) for x in candidates])

    # Prefer video streams when present, but allow a normal photo Pin as the primary result.
    ordered = [x for x in candidates if re.search(r'\.(?:mp4|m3u8)(?:[?#]|$)|/videos?/', urlparse(x).path, re.I)]
    ordered += [x for x in candidates if x not in ordered and "pinimg.com" in (urlparse(x).hostname or "").lower()]
    last = None
    for media_url in ordered:
        if not media_ok(media_url):
            continue
        try:
            path = os.path.join(folder, "pinterest_media")
            ctype = download_media(media_url, path)
            ext = MEDIA_EXT.get(ctype) or os.path.splitext(urlparse(media_url).path)[1].lower()
            if ext not in MEDIA_EXT.values():
                ext = ".mp4" if ctype.startswith("video/") else ".jpg" if ctype.startswith("image/") else ""
            media_type_ok = ctype.startswith("image/") or ctype.startswith("video/")
            if not ext or (not media_type_ok and ctype != "application/octet-stream"):
                raise RuntimeError("Pinterest did not return a supported image or video file.")
            if ctype == "application/octet-stream" and ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mov", ".webm"):
                raise RuntimeError("Pinterest returned an unsupported media format.")
            final_path = os.path.join(folder, "pinterest_download" + ext)
            os.replace(path, final_path)
            return final_path
        except Exception as e:
            last = e
    raise last or RuntimeError("Pinterest did not expose a downloadable image or video for this Pin. The Pin may be private, restricted, or temporarily blocked.")


# ---- YouTube audio through RapidAPI (optional) ----
# Set RAPIDAPI_KEY on the server to use it. yt-dlp stays as the automatic backup if the API fails or the daily cap is reached.
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "").strip()
RAPIDAPI_HOST = os.getenv("RAPIDAPI_HOST", "youtube-mp3-audio-video-downloader.p.rapidapi.com").strip()
RAPIDAPI_QUALITY = os.getenv("RAPIDAPI_MP3_QUALITY", "low").strip()
RAPIDAPI_DAILY = int(os.getenv("RAPIDAPI_DAILY_LIMIT", "200"))
_rapid_used: dict[str, int] = {}
RAPIDAPI_HIDE_ERRORS = os.getenv("RAPIDAPI_HIDE_ERRORS", "").strip() == "1"  # 1 = hide the technical reason from visitors


def yt_video_id(url: str) -> str:
    p = urlparse(url.strip())
    host = (p.hostname or "").lower()
    if host == "youtu.be" or host.endswith(".youtu.be"):
        vid = p.path.strip("/").split("/")[0]
    else:
        q = parse_qs(p.query).get("v")
        parts = [x for x in p.path.split("/") if x]
        vid = q[0] if q else (parts[1] if len(parts) > 1 and parts[0] in ("shorts", "embed", "live", "v") else "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid or ""):
        raise RuntimeError("could not read the YouTube video id")
    return vid


def _rapid_quota_ok(bucket: str = "audio", limit: int | None = None) -> bool:
    day = time.strftime("%Y-%m-%d", time.gmtime()) + bucket
    cap = RAPIDAPI_DAILY if limit is None else limit
    for k in [k for k in _rapid_used if k.endswith(bucket) and k != day]:
        del _rapid_used[k]
    if _rapid_used.get(day, 0) >= cap:
        return False
    _rapid_used[day] = _rapid_used.get(day, 0) + 1
    return True


def _rapid_json(path: str, timeout: int = 75, host: str | None = None):
    host = host or RAPIDAPI_HOST
    req = urllib.request.Request("https://" + host + path,
                                 headers={"x-rapidapi-key": RAPIDAPI_KEY, "x-rapidapi-host": host, "Accept": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:160]
        except Exception:
            pass
        raise RuntimeError(f"RapidAPI answered HTTP {e.code} {body}".strip())


def _find_link(obj):
    """The API's JSON shape is not documented, so look for the file link under the usual key names, then anywhere."""
    prefer = ("file", "link", "url", "download", "downloadurl", "download_url", "dlink", "download_link", "mp3", "audio")
    if isinstance(obj, dict):
        low = {str(k).lower(): v for k, v in obj.items()}
        for k in prefer:
            v = low.get(k)
            if isinstance(v, str) and v.startswith("http") and not re.match(r"https?://([a-z0-9-]+\.)?(youtube\.com|youtu\.be)/", v, re.I):
                return v
        for v in obj.values():
            r = _find_link(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_link(v)
            if r:
                return r
    elif isinstance(obj, str) and obj.startswith("https://") and not re.search(r"^https://([a-z0-9-]+\.)?(youtube\.com|youtu\.be)/", obj, re.I) and not re.search(r"\.(jpe?g|png|webp|gif)(\?|$)", obj, re.I) and "ytimg.com" not in obj:
        return obj
    return None


def _public_https(u: str) -> bool:
    try:
        p = urlparse(u)
        if p.scheme != "https" or not p.hostname:
            return False
        for info in socket.getaddrinfo(p.hostname, p.port or 443, proto=socket.IPPROTO_TCP):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        return True
    except Exception:
        return False


class _PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _public_https(newurl):
            raise urllib.error.URLError("redirect to a host that is not allowed")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _save_file(u: str, path: str, once: bool = False) -> None:
    """Download the finished file. Some links are picky about headers or need a few seconds to become ready,
    so each attempt uses a different set of headers and a few retries are allowed."""
    if not _public_https(u):
        raise RuntimeError("the download link was not usable")
    host = (urlparse(u).hostname or "").lower()
    limit = MAX_MB * 1024 * 1024
    variants = [
        {"User-Agent": UA, "Accept": "*/*"},
        {"User-Agent": UA, "Accept": "*/*", "Referer": "https://" + RAPIDAPI_HOST.replace(".p.rapidapi.com", "") + "/", "Origin": "https://rapidapi.com"},
        {"User-Agent": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36", "Accept": "*/*", "Range": "bytes=0-"},
        {"Accept": "*/*"},
    ]
    if host.endswith(".rapidapi.com"):  # files served by the API itself may need the key
        variants.insert(0, {"x-rapidapi-key": RAPIDAPI_KEY, "x-rapidapi-host": host, "Accept": "*/*"})
    last = None
    for attempt in range(len(variants) + 2):
        try:
            req = urllib.request.Request(u, headers=variants[attempt % len(variants)])
            got = 0
            with urllib.request.build_opener(_PublicRedirect()).open(req, timeout=45) as r, open(path, "wb") as fh:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    got += len(chunk)
                    if got > limit:
                        raise RuntimeError(f"the file is larger than the {MAX_MB} MB limit")
                    fh.write(chunk)
            if got > 10_000:
                return
            last = RuntimeError("the file was empty")
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = re.sub(r"\s+", " ", e.read().decode("utf-8", "replace"))[:90]
            except Exception:
                pass
            last = RuntimeError(f"HTTP {e.code} from {host} {body}".strip())
            if once and e.code == 404:  # quick readiness check: report "not ready yet" at once
                break
            if e.code not in (401, 403, 404, 409, 423, 425, 429, 500, 502, 503):
                break
        except urllib.error.URLError as e:
            last = e
        time.sleep(2)
    raise RuntimeError(f"could not fetch the media file: {last}")


def rapid_audio(url: str, folder: str) -> str:
    if not _rapid_quota_ok():
        raise RuntimeError("daily RapidAPI limit reached")
    vid = yt_video_id(url)
    data = _rapid_json(f"/get_mp3_download_link/{vid}?quality={quote(RAPIDAPI_QUALITY)}&wait_until_the_file_is_ready=true")
    link = _find_link(data)
    if not link:
        raise RuntimeError("RapidAPI gave no download link: " + json.dumps(data)[:200])
    title = ""
    if isinstance(data, dict):
        title = str(data.get("title") or data.get("name") or "")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", title).strip("._")[:80] or "YouTube"
    out = os.path.join(folder, f"{stem}_{vid}.mp3")
    _save_file(link, out)
    return out


RAPIDAPI_VIDEO_HOST = os.getenv("RAPIDAPI_VIDEO_HOST", "youtube-video-and-shorts-downloader.p.rapidapi.com").strip()
RAPIDAPI_VIDEO_DAILY = int(os.getenv("RAPIDAPI_VIDEO_DAILY_LIMIT", "100"))
_URL_KEYS = ("url", "link", "src", "download", "downloadUrl", "download_url", "file", "stream", "streamUrl")


def _height(it: dict) -> int:
    for k in ("height", "quality", "qualityLabel", "label", "resolution", "format_note", "formatNote"):
        m = re.search(r"(\d{3,4})", str(it.get(k) or ""))
        if m:
            return int(m.group(1))
    return 0


def _collect_streams(obj, out: list, in_list: bool = False) -> None:
    """The service's JSON layout is not documented, so gather every object that carries a file link."""
    if isinstance(obj, dict):
        u = next((obj[k] for k in _URL_KEYS if isinstance(obj.get(k), str) and obj[k].startswith("http")), None)
        if u:
            out.append((u, obj))
        for v in obj.values():
            if isinstance(v, (dict, list)):
                _collect_streams(v, out)
    elif isinstance(obj, list):
        for v in obj:
            if isinstance(v, str) and v.startswith("http"):
                out.append((v, {}))
            else:
                _collect_streams(v, out, True)


def _describe(u: str, it: dict) -> dict:
    q = parse_qs(urlparse(u).query)
    mime = str(it.get("mimeType") or it.get("mime") or it.get("mime_type") or it.get("type") or (q.get("mime") or [""])[0]).lower()
    ext = str(it.get("ext") or it.get("extension") or it.get("container") or "").lower().lstrip(".")
    if not ext:
        ext = (re.search(r"\.([a-z0-9]{2,4})(?:[?#]|$)", urlparse(u).path.lower()) or [None, ""])[1]
    if not ext and "/" in mime:
        ext = mime.split("/")[1].split(";")[0]
    audio_only = ("audio" in mime and "video" not in mime) or ext in ("m4a", "mp3", "opus", "aac") or str(it.get("vcodec") or "").lower() == "none"
    img = ext in ("jpg", "jpeg", "png", "webp", "gif") or "ytimg.com" in u
    has_audio = None
    for k in ("hasAudio", "has_audio", "audio", "withAudio"):
        if isinstance(it.get(k), bool):
            has_audio = it[k]
    if has_audio is None and str(it.get("acodec") or "").lower() == "none":
        has_audio = False
    size = it.get("size") or it.get("contentLength") or it.get("filesize") or (q.get("clen") or [None])[0]
    try:
        size = int(size) if size is not None else None
    except (TypeError, ValueError):
        size = None
    return {"url": u, "host": urlparse(u).hostname or "", "height": _height(it), "ext": ext, "mime": mime, "audio_only": audio_only,
            "image": img, "has_audio": has_audio, "size": size}


def _video_candidates(data) -> tuple[list, list]:
    found: list = []
    _collect_streams(data, found)
    seen, items = set(), []
    for u, it in found:
        if u in seen:
            continue
        seen.add(u)
        items.append(_describe(u, it))
    cap = MAX_MB * 1024 * 1024
    vids = [d for d in items if not d["audio_only"] and not d["image"] and d["ext"] in ("mp4", "", "m4v", "mov")
            and d["height"] <= 1080 and (d["size"] is None or d["size"] <= cap)]
    # muxed files first (sound included or unknown), then the sharpest
    vids.sort(key=lambda d: (d["has_audio"] is not False, d["height"]), reverse=True)
    auds = [d for d in items if d["audio_only"] and d["ext"] in ("m4a", "mp4", "aac", "mp3")]
    return vids, auds


def _video_json(vid: str):
    return _rapid_json(f"/download.php?id={quote(vid)}", host=RAPIDAPI_VIDEO_HOST)


RAPIDAPI_FAST_HOST = os.getenv("RAPIDAPI_FAST_VIDEO_HOST", "youtube-video-fast-downloader-24-7.p.rapidapi.com").strip()
RAPIDAPI_FAST_MAX_HEIGHT = int(os.getenv("RAPIDAPI_FAST_MAX_HEIGHT", "1080"))


def _save_when_ready(u: str, path: str, wait: int = 100) -> None:
    """The fast service prepares the file on its own server; the link answers 404 until it is ready."""
    deadline = time.monotonic() + wait
    time.sleep(5)  # give the service 5 seconds to prepare the file, then fetch it
    while True:
        try:
            _save_file(u, path, once=True)  # one quick check per round instead of six slow retries
            return
        except Exception as e:
            if "HTTP 404" not in str(e):
                _save_file(u, path)  # not a "not ready" answer: use the normal retries once
                return
            if time.monotonic() > deadline:
                raise
            time.sleep(1.5)


def rapid_video_fast(url: str, vid: str, folder: str, quality: str | None = None) -> str:
    """Video from youtube-video-fast-downloader-24-7. Its file links live on the API's own server,
    so they are not locked to the API's IP like raw googlevideo.com links (those give HTTP 403 from Render)."""
    opts = _rapid_json(f"/get_available_quality/{quote(vid)}", host=RAPIDAPI_FAST_HOST)
    if isinstance(opts, dict) and isinstance(opts.get("url"), str) and not any(isinstance(v, list) for v in opts.values()):
        raise RuntimeError("RapidAPI fast video: unexpected answer " + json.dumps(opts)[:160])
    items = opts if isinstance(opts, list) else next((v for v in opts.values() if isinstance(v, list)), []) if isinstance(opts, dict) else []
    cap = MAX_MB * 1024 * 1024
    cands = []
    for it in items:
        if not isinstance(it, dict) or str(it.get("type", "video")).lower() != "video" or it.get("id") is None:
            continue
        h = _height(it)
        try:
            size = int(str(it.get("size") or 0).strip('"\\ ') or 0)
        except ValueError:
            size = 0
        if h > RAPIDAPI_FAST_MAX_HEIGHT or (size and size > cap):
            continue
        mp4 = "mp4" in str(it.get("mime") or "").lower()
        cands.append((mp4, h, it))
    if not cands:
        raise RuntimeError("RapidAPI fast video: no usable quality " + json.dumps(items)[:160])
    cands.sort(key=lambda c: (c[0], c[1]), reverse=True)
    if quality:
        cands = [c for c in cands if str(c[2].get("id")) == str(quality)]
        if not cands:
            raise RuntimeError("The selected YouTube quality is no longer available. Fetch qualities again.")
    kind = "download_short" if "/shorts/" in url.lower() else "download_video"
    errors = []
    for mp4, h, it in cands[:1] if quality else cands[:2]:
        try:
            data = _rapid_json(f"/{kind}/{quote(vid)}?quality={quote(str(it['id']))}", host=RAPIDAPI_FAST_HOST)
            link = data.get("file") if isinstance(data, dict) else None
            if not isinstance(link, str) or not link.startswith("http"):
                link = _find_link(data)
            if not link:
                raise RuntimeError("no file link: " + json.dumps(data)[:160])
            title = str(data.get("title") or "") if isinstance(data, dict) else ""
            stem = re.sub(r"[^A-Za-z0-9._-]+", "_", title).strip("._")[:80] or "YouTube"
            ext = ".mp4" if mp4 or "mp4" in str(data.get("mime") if isinstance(data, dict) else "").lower() else ".webm"
            out = os.path.join(folder, f"{stem}_{vid}{ext}")
            _save_when_ready(link, out)
            return out
        except Exception as e:
            errors.append(str(e)[:140])
            clear_folder(folder)
    raise RuntimeError("fast video: " + "; ".join(errors))


def rapid_video(url: str, folder: str, quality: str | None = None) -> str:
    if not _rapid_quota_ok("video", RAPIDAPI_VIDEO_DAILY):
        raise RuntimeError("daily RapidAPI video limit reached")
    vid = yt_video_id(url)
    try:
        return rapid_video_fast(url, vid, folder, quality)
    except Exception as e:
        first = str(e)[:200]
        print(f"[toolora] rapidapi fast video failed {vid}: {first}", flush=True)
        clear_folder(folder)
        if quality:
            raise RuntimeError(first)
    try:
        return _rapid_video_raw(vid, folder)
    except Exception as e:
        raise RuntimeError(f"{first} | backup: {str(e)[:150]}")


def _rapid_video_raw(vid: str, folder: str) -> str:
    """Backup: youtube-video-and-shorts-downloader. Its links are often IP-locked, so it only sometimes works."""
    data = _video_json(vid)
    if isinstance(data, dict) and (
        data.get("error")
        or data.get("success") is False
        or data.get("status") is False
        or str(data.get("status", "")).strip().lower() == "error"
        or (isinstance(data.get("status_code"), int) and data["status_code"] >= 400)
    ):
        message = data.get("message") or data.get("error") or "API returned an error"
        raise RuntimeError(f"RapidAPI: {message}")
    vids, auds = _video_candidates(data)
    if not vids:
        raise RuntimeError("RapidAPI returned no usable video: " + json.dumps(data)[:200])
    title = str(data.get("title") or "") if isinstance(data, dict) else ""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", title).strip("._")[:80] or "YouTube"
    out = os.path.join(folder, f"{stem}_{vid}.mp4")
    errors = []
    for pick in vids[:3]:
        try:
            if pick["has_audio"] is False and auds:
                vpath, apath = os.path.join(folder, "v.part.mp4"), os.path.join(folder, "a.part.m4a")
                _save_file(pick["url"], vpath)
                _save_file(auds[0]["url"], apath)
                r = subprocess.run(["ffmpeg", "-y", "-i", vpath, "-i", apath, "-c", "copy", "-movflags", "+faststart", out], capture_output=True, timeout=150)
                if r.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) < 10_000:
                    raise RuntimeError("could not join the video and audio")
                for p in (vpath, apath):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            else:
                _save_file(pick["url"], out)
            return out
        except Exception as e:
            errors.append(str(e)[:120])
            clear_folder(folder)
    raise RuntimeError("; ".join(errors))


def youtube_available_qualities(url: str) -> list[dict]:
    """Return the available video qualities quickly, without preparing/downloading media."""
    vid = yt_video_id(url)
    data = _rapid_json(f"/get_available_quality/{quote(vid)}", host=RAPIDAPI_FAST_HOST)
    items = data if isinstance(data, list) else next((v for v in data.values() if isinstance(v, list)), []) if isinstance(data, dict) else []
    out, seen = [], set()
    cap = MAX_MB * 1024 * 1024
    for it in items:
        if not isinstance(it, dict) or str(it.get("type", "video")).lower() != "video" or it.get("id") is None:
            continue
        h = _height(it)
        try: size = int(str(it.get("size") or 0).strip('"\\ ') or 0)
        except (TypeError, ValueError): size = 0
        if h <= 0 or (size and size > cap): continue
        key = str(it["id"])
        if key in seen: continue
        seen.add(key)
        out.append({"id": key, "height": h, "label": f"{h}p" + (" (MP4)" if "mp4" in str(it.get("mime") or "").lower() else "")})
    out.sort(key=lambda x: x["height"], reverse=True)
    return out


def fetch_youtube(url: str, folder: str, mode: str, quality: str | None = None) -> str:
    """YouTube is served only by RapidAPI (no yt-dlp)."""
    if not RAPIDAPI_KEY:
        raise RuntimeError("YouTube downloads are not available right now (RAPIDAPI_KEY is not configured).")
    try:
        return rapid_audio(url, folder) if mode == "audio" else rapid_video(url, folder, quality)
    except Exception as e:
        detail = re.sub(r"\s+", " ", ANSI.sub("", str(e))).strip()
        print(f"[toolora] rapidapi {mode} failed {url[:120]}: {detail[:500]}", flush=True)
        clear_folder(folder)
        low = detail.lower()
        if ("daily" in low and "limit" in low) or "http 429" in low:
            msg = "YouTube downloads have reached today's limit. Please try again later."
        elif "larger than" in low:
            msg = f"that video is larger than the {MAX_MB} MB limit."
        else:
            msg = "YouTube could not be downloaded right now. Please try again later."
        if not RAPIDAPI_HIDE_ERRORS:
            msg += f" [{detail[:180]}]"
        raise RuntimeError(msg)


def fetch(url: str, folder: str, mode: str = "video", quality: str | None = None) -> str:
    orig = url.strip()
    url = normalize_url(url)
    if is_youtube(url):
        return fetch_youtube(url, folder, mode, quality)  # YouTube is served only by RapidAPI
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
    if OUT_PROXY:
        base["proxy"] = OUT_PROXY
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
    if snap:  # yt-dlp only knows the watermarked file, so the watermark-free version is tried first
        try:
            return snapchat_fallback(url, folder, mode, clean_only=True)
        except Exception as e:
            print(f"[toolora] snapchat clean download failed {url[:120]}: {ANSI.sub('', str(e))[:200]}", flush=True)
            clear_folder(folder)
    try:
        capped = False  # a download that finished without a file means the size cap was hit
        started, blocked = time.monotonic(), 0
        for extra in attempts_for(mode, url):
            if last is not None and time.monotonic() - started > BUDGET:
                break  # stop trying new methods; the visitor gets the last reason instead of an endless spinner
            lower = bool(extra.get("_lower"))
            extra = {k: v for k, v in extra.items() if k != "_lower"}
            if lower and not capped and "requested format" not in str(last or "").lower():
                continue  # smaller versions only help with size or format problems, not with blocks
            clear_folder(folder)
            try:
                with yt_dlp.YoutubeDL({**base, **extra}) as ydl:
                    ydl.download([url])
            except Exception as e:
                last = e
                if insta or snap or is_pinterest(url):  # these platforms can fail one extractor while another strategy still works
                    continue
                if is_tiktok(url) and mode == "video" and isinstance(e, yt_dlp.utils.DownloadError):
                    break  # e.g. a photo post yt-dlp can't read: go straight to the picture fallback below
                if not isinstance(e, yt_dlp.utils.DownloadError):
                    raise
                msg = str(e).lower()
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
        if is_tiktok(url) and mode == "video":  # photo posts that yt-dlp can't read: hand back the first picture
            clear_folder(folder)
            try:
                pics = [x for x in info_items(orig) if x.get("kind") == "image" and x.get("src")]
                if pics:
                    return fetch_media_file(pics[0]["src"], folder)[0]
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
        if is_pinterest(url):
            clear_folder(folder)
            try:
                return pinterest_fallback(orig, folder)
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



@app.get("/rapid-test")
def rapid_test(id: str = "dQw4w9WgXcQ", token: str = "", kind: str = "video"):
    """Owner-only check of the RapidAPI services. Works only when RAPIDAPI_TEST_TOKEN is set on the server."""
    expected = os.getenv("RAPIDAPI_TEST_TOKEN", "")
    if not expected or not hmac.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(404, "Not found")
    if not RAPIDAPI_KEY:
        return JSONResponse({"ok": False, "problem": "RAPIDAPI_KEY is empty on this server"})
    try:
        vid = yt_video_id("https://youtu.be/" + id) if len(id) == 11 else yt_video_id(id)
        if kind == "mp3":
            data = _rapid_json(f"/get_mp3_download_link/{vid}?quality={quote(RAPIDAPI_QUALITY)}&wait_until_the_file_is_ready=true")
            link = _find_link(data)
            cands = [_describe(link, {})] if link else []
        else:
            data = _video_json(vid)
            vids, auds = _video_candidates(data)
            cands = vids[:4] + auds[:1]
    except Exception as e:
        return JSONResponse({"ok": False, "problem": str(e)[:300]})
    report = []
    for d in cands:
        row = {k: d[k] for k in ("host", "height", "ext", "mime", "has_audio", "size")}
        try:
            req = urllib.request.Request(d["url"], headers={"User-Agent": UA, "Range": "bytes=0-1"})
            with urllib.request.urlopen(req, timeout=25) as r:
                row["fetch_from_this_server"] = f"OK (HTTP {r.status})"
        except urllib.error.HTTPError as e:
            row["fetch_from_this_server"] = f"REFUSED (HTTP {e.code})"
        except Exception as e:
            row["fetch_from_this_server"] = "ERROR " + str(e)[:80]
        report.append(row)
    keys = list(data.keys())[:20] if isinstance(data, dict) else type(data).__name__
    return JSONResponse({"ok": True, "kind": kind, "top_level_keys": keys, "candidates": report,
                         "preview": re.sub(r"https?://[^\"\s]+", "<link>", json.dumps(data))[:700]})


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
        raise HTTPException(400, "Paste a link from YouTube, TikTok, Instagram, X (Twitter), Snapchat or Pinterest.")

    if req.mode == "info" and is_youtube(link):
        try:
            if not RAPIDAPI_KEY:
                return JSONResponse({"items": [], "qualities": [], "error": "YouTube service is not configured."}, headers={"Cache-Control": "no-store"})
            qualities = await asyncio.to_thread(youtube_available_qualities, link)
            return JSONResponse({"items": [], "qualities": qualities}, headers={"Cache-Control": "no-store"})
        except Exception as e:
            print(f"[toolora] youtube quality lookup failed {link[:120]}: {ANSI.sub('', str(e))[:300]}", flush=True)
            return JSONResponse({"items": [], "qualities": []}, headers={"Cache-Control": "no-store"})

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
            path = await asyncio.to_thread(fetch, link, folder, req.mode, req.quality)
    except Exception as e:
        shutil.rmtree(folder, ignore_errors=True)
        print(f"[toolora] download failed ({req.mode}) {link[:120]}: {ANSI.sub('', str(e))[:600]}", flush=True)
        reason = clean_error(e)
        what = "extract the audio from" if req.mode == "audio" else "download"
        target = "that media" if is_pinterest(link) else "that video"
        raise HTTPException(422, f"Couldn't {what} {target}: {reason}")
    ext = os.path.splitext(path)[1].lower()
    media = {".mp3": "audio/mpeg", ".mp4": "video/mp4", ".webm": "video/webm", ".mkv": "video/x-matroska", ".mov": "video/quicktime", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}.get(ext, "application/octet-stream")
    return FileResponse(path, media_type=media, filename=os.path.basename(path), background=cleanup)


# Optional: if a "static" folder sits next to this file, serve it as the website (handy for local testing).
# In production the site is served by the Cloudflare worker, so the backend runs without this folder.
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(STATIC_DIR):
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="site")
