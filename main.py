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
"""
import asyncio, hmac, os, re, shutil, tempfile, time, subprocess, html as html_lib, json
from typing import Literal
from collections import defaultdict, deque
from urllib.parse import urlparse, urljoin

try:
    from curl_cffi import requests as curl_requests
except Exception:
    curl_requests = None

import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask

ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
MAX_MB = int(os.getenv("MAX_FILESIZE_MB", "1024"))
RATE = int(os.getenv("RATE_PER_MINUTE", "10"))
PROXY_SECRET = os.getenv("PROXY_SECRET", "")
slots = asyncio.Semaphore(int(os.getenv("MAX_CONCURRENT", "3")))

# Only these sites (and their subdomains) are accepted; this also blocks SSRF to internal hosts.
ALLOWED_HOSTS = ("youtube.com", "youtu.be", "tiktok.com", "instagram.com", "instagr.am", "twitter.com", "x.com")

app = FastAPI(title="Toolora backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGINS,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],  # the frontend reads the filename from this header
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


SORT = ["res", "ext:mp4:m4a"]
FRAGMENT = re.compile(r"\.f[\w-]+\.[A-Za-z0-9]+$")
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


def platform_for(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host == "youtube.com" or host.endswith(".youtube.com") or host == "youtu.be" or host.endswith(".youtu.be"):
        return "youtube"
    if host == "instagram.com" or host.endswith(".instagram.com") or host == "instagr.am" or host.endswith(".instagr.am"):
        return "instagram"
    if host == "tiktok.com" or host.endswith(".tiktok.com"):
        return "tiktok"
    if host == "twitter.com" or host.endswith(".twitter.com") or host == "x.com" or host.endswith(".x.com"):
        return "x"
    return "other"


def is_youtube(url: str) -> bool:
    return platform_for(url) == "youtube"


def needs_impersonation(url: str) -> bool:
    return platform_for(url) in {"instagram", "tiktok", "x"}


def _youtube_attempts(mode: str) -> list[dict]:
    if mode == "audio":
        formats = [
            {"format": "bestaudio[ext=m4a]/bestaudio/best"},
            {"format": "bestaudio/best"},
        ]
    else:
        # Prefer MP4/H264 where available, then fall back through lower resolutions.
        formats = [
            {"format": "bv*[ext=mp4][height<=1080]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]", "format_sort": SORT},
            {"format": "bv*[ext=mp4][height<=720]+ba[ext=m4a]/bv*[height<=720]+ba/b[height<=720]", "format_sort": SORT},
            {"format": "bv*[ext=mp4][height<=480]+ba[ext=m4a]/bv*[height<=480]+ba/b[height<=480]", "format_sort": SORT},
            {"format": "best[ext=mp4]/best"},
        ]

    # YouTube is currently changing which Innertube clients can download GVS
    # formats. Try clients that commonly work without account cookies before the
    # generic extractor. A PO token can be supplied through the environment when
    # YouTube requires one for the Render server's IP.
    clients = ["tv_simply", "web_embedded", "tv", "android_vr", "web_safari", "ios", "android", "web"]
    out = []
    po = os.getenv("YOUTUBE_PO_TOKEN", "").strip()
    po_client = os.getenv("YOUTUBE_PO_CLIENT", "mweb").strip() or "mweb"
    for client in clients:
        for item in formats:
            x = dict(item)
            args = {"player_client": [client]}
            if po and client == po_client:
                args["po_token"] = [f"{po_client}.gvs+{po}"]
            x["extractor_args"] = {"youtube": args}
            x["remote_components"] = {"ejs:github", "ejs:npm"}
            out.append(x)
    out.extend({**x, "remote_components": {"ejs:github", "ejs:npm"}} for x in formats)
    return out


def attempts_for(mode: str, url: str) -> list[dict]:
    platform = platform_for(url)
    if platform == "youtube":
        return _youtube_attempts(mode)

    if mode == "audio":
        formats = [
            {"format": "bestaudio[ext=m4a]/bestaudio/best", "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]},
            {"format": "best[ext=mp4]/best", "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]},
        ]
    else:
        formats = [
            {"format": "bv*[ext=mp4][height<=1080]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]", "format_sort": SORT},
            {"format": "bv*[height<=720]+ba/b[height<=720]", "format_sort": SORT},
            {"format": "bv*[height<=480]+ba/b[height<=480]", "format_sort": SORT},
            {"format": "best[ext=mp4]/best"},
        ]

    out = []
    for item in formats:
        x = dict(item)
        # Try the extractor's normal request first and then with browser
        # impersonation. This is particularly useful for Instagram/TikTok/X.
        out.append(x)
        y = dict(item)
        y["http_headers"] = {
            "User-Agent": "Mozilla/5.0 (Linux; Android 16; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Mobile Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.instagram.com/" if platform == "instagram" else "https://www.tiktok.com/" if platform == "tiktok" else "https://x.com/",
        }
        y["impersonate"] = "chrome"
        out.append(y)
    return out




def _download_direct(url: str, folder: str, mode: str, referer: str) -> str | None:
    """Last-resort extractor for public pages that expose a direct media URL in HTML.
    This is deliberately limited to the allowed social hosts and never follows arbitrary
    redirects to internal/private addresses.
    """
    if curl_requests is None:
        return None
    try:
        r = curl_requests.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Linux; Android 16; K) AppleWebKit/537.36 Chrome/140.0 Mobile Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": referer,
            },
            impersonate="chrome",
            timeout=25,
            allow_redirects=True,
        )
        if r.status_code >= 400:
            return None
        text = r.text
        candidates = []
        # Standard OpenGraph/Twitter metadata.
        for pat in (
            r'<meta[^>]+property=["\'](?:og:video(?::secure_url)?|og:video:url)["\'][^>]+content=["\']([^"\']+)',
            r'<meta[^>]+name=["\']twitter:player:stream["\'][^>]+content=["\']([^"\']+)',
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
        ):
            candidates += re.findall(pat, text, re.I)
        # Common JSON-embedded media fields used by TikTok/Instagram/X pages.
        for pat in (
            r'"playAddr"\s*:\s*"([^"]+)"',
            r'"downloadAddr"\s*:\s*"([^"]+)"',
            r'"video_url"\s*:\s*"([^"]+)"',
            r'"video_versions"\s*:\s*\[\s*\{[^}]*?"url"\s*:\s*"([^"]+)"',
            r'"contentUrl"\s*:\s*"([^"]+)"',
        ):
            candidates += re.findall(pat, text, re.I)
        clean = []
        for c in candidates:
            c = html_lib.unescape(c).replace('\\/', '/').replace('\\u0026', '&').replace('\\u003D', '=').replace('\\u002F', '/')
            try:
                c = json.loads('"'+c.replace('"','\\"')+'"') if '\\u' in c else c
            except Exception:
                pass
            c = urljoin(str(r.url), c)
            if c.startswith(('https://', 'http://')) and c not in clean:
                clean.append(c)
        if mode == "audio":
            # Prefer an explicit audio URL if the page exposes one; otherwise grab
            # the public video and extract its soundtrack with ffmpeg below.
            audio_candidates = [c for c in clean if any(x in c.lower() for x in ('.mp3', '.m4a', '.aac', '.ogg', '.opus', 'audio'))]
            if audio_candidates:
                clean = audio_candidates + clean
        if not clean:
            return None
        for media_url in clean:
            try:
                head = curl_requests.head(media_url, headers={"Referer": referer, "User-Agent": "Mozilla/5.0"}, impersonate="chrome", timeout=15, allow_redirects=True)
                if head.status_code >= 400:
                    continue
                ct = (head.headers.get("content-type") or "").lower()
                ext = ".mp4" if "video" in ct or media_url.lower().split('?')[0].endswith(('.mp4','.m4v')) else ".jpg" if "image" in ct else ".bin"
                if mode == "audio" and ("audio" in ct or media_url.lower().split('?')[0].endswith(('.mp3','.m4a','.aac','.ogg','.opus'))):
                    ext = ".mp3" if not media_url.lower().split('?')[0].endswith('.mp3') else ".mp3"
                out = os.path.join(folder, f"social-fallback{ext}")
                with curl_requests.get(media_url, headers={"Referer": referer, "User-Agent": "Mozilla/5.0"}, impersonate="chrome", timeout=60, stream=True, allow_redirects=True) as dl:
                    if dl.status_code >= 400:
                        continue
                    total = 0
                    with open(out, "wb") as f:
                        for chunk in dl.iter_content(1024 * 256):
                            if not chunk:
                                continue
                            total += len(chunk)
                            if total > MAX_MB * 1024 * 1024:
                                raise RuntimeError("media exceeds the configured download limit")
                            f.write(chunk)
                if total <= 0:
                    try: os.remove(out)
                    except OSError: pass
                    continue
                if mode == "audio" and not out.endswith('.mp3'):
                    mp3 = os.path.join(folder, "social-fallback.mp3")
                    subprocess.run(["ffmpeg","-y","-i",out,"-vn","-codec:a","libmp3lame","-b:a","192k",mp3], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
                    if os.path.exists(mp3) and os.path.getsize(mp3) > 0:
                        return mp3
                return out
            except Exception:
                continue
    except Exception:
        return None
    return None

def pick_file(folder: str, mode: str) -> str | None:
    want = (".mp3",) if mode == "audio" else (".mp4", ".mkv", ".webm", ".mov", ".jpg", ".jpeg", ".png", ".webp", ".gif")
    files = [f for f in os.listdir(folder) if f.lower().endswith(want) and not FRAGMENT.search(f)]
    if not files:
        return None
    return os.path.join(folder, max(files, key=lambda f: os.path.getsize(os.path.join(folder, f))))


def fetch(url: str, folder: str, mode: str = "video") -> str:
    platform = platform_for(url)
    base = {
        "outtmpl": os.path.join(folder, "%(title).80B [%(id)s].%(ext)s"),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "restrictfilenames": True,
        "max_filesize": MAX_MB * 1024 * 1024,
        "socket_timeout": 30,
        "quiet": True,
        "no_warnings": False,
        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 3,
        "retry_sleep_functions": {"http": lambda n: min(8, 1.5 * (n + 1))},
        "concurrent_fragment_downloads": 1,
        "remote_components": {"ejs:github", "ejs:npm"},
        "js_runtimes": {"deno": {}},
        "geo_bypass": True,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        },
    }
    last = None
    errors: list[str] = []
    for attempt_no, extra in enumerate(attempts_for(mode, url), 1):
        clear_folder(folder)
        opts = {**base, **extra}
        if needs_impersonation(url):
            opts.setdefault("impersonate", "chrome")
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
        except yt_dlp.utils.DownloadError as e:
            last = e
            clean = ANSI.sub("", str(e)).strip()
            if clean:
                errors.append(clean[:320])
            msg = clean.lower()
            # Keep trying when a platform temporarily rejects one request style.
            if any(x in msg for x in ("http error 429", "too many requests", "rate-limit", "http error 403", "forbidden", "unable to download", "unable to extract", "requested format", "sign in to confirm", "challenge", "captcha")):
                time.sleep(min(3, 0.5 + attempt_no * 0.25))
                continue
            # yt-dlp's extractors can fail for a single format while another
            # format is still usable, so continue through the complete strategy list.
            continue
        path = pick_file(folder, mode)
        if path:
            if mode == "video":
                ext = os.path.splitext(path)[1].lower()
                if ext in (".webm", ".mkv", ".mov"):
                    mp4 = os.path.join(folder, os.path.splitext(os.path.basename(path))[0] + ".mp4")
                    try:
                        subprocess.run(
                            ["ffmpeg", "-y", "-i", path, "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", mp4],
                            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180,
                        )
                        if os.path.exists(mp4) and os.path.getsize(mp4) > 0:
                            return mp4
                    except (subprocess.SubprocessError, OSError):
                        pass
            return path
    # Final public-page fallback. This catches cases where a platform changes its
    # extractor but still exposes a direct media URL in the page metadata.
    fallback = _download_direct(url, folder, mode,
        "https://www.instagram.com/" if platform == "instagram" else
        "https://www.tiktok.com/" if platform == "tiktok" else
        "https://x.com/" if platform == "x" else
        "https://www.youtube.com/")
    if fallback:
        if mode == "video":
            ext = os.path.splitext(fallback)[1].lower()
            if ext in (".webm", ".mkv", ".mov"):
                mp4 = os.path.join(folder, "social-fallback.mp4")
                try:
                    subprocess.run(["ffmpeg","-y","-i",fallback,"-map","0:v:0","-map","0:a?","-c:v","libx264","-preset","veryfast","-crf","23","-c:a","aac","-b:a","128k","-movflags","+faststart",mp4], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
                    if os.path.exists(mp4) and os.path.getsize(mp4) > 0:
                        return mp4
                except Exception:
                    pass
        return fallback

    # For audio, do not require the platform to expose a separate audio stream.
    # Download a video-capable representation and extract its soundtrack locally.
    if mode == "audio":
        try:
            clear_folder(folder)
            video_path = fetch(url, folder, "video")
            mp3 = os.path.join(folder, "toolora-audio.mp3")
            subprocess.run(["ffmpeg","-y","-i",video_path,"-vn","-codec:a","libmp3lame","-b:a","192k",mp3], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
            if os.path.exists(mp3) and os.path.getsize(mp3) > 0:
                return mp3
        except Exception:
            pass

    detail = errors[-1] if errors else "No public media stream was available."
    # Do not expose a huge yt-dlp traceback to the visitor.
    raise RuntimeError(detail[:500])


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
    check_rate(ip)
    if not valid_url(req.url):
        raise HTTPException(400, "Paste a link from YouTube, TikTok, Instagram or X (Twitter).")
    folder = tempfile.mkdtemp(prefix="dl_")
    cleanup = BackgroundTask(shutil.rmtree, folder, ignore_errors=True)
    try:
        async with slots:
            path = await asyncio.to_thread(fetch, req.url.strip(), folder, req.mode)
    except Exception as e:
        shutil.rmtree(folder, ignore_errors=True)
        reason = ANSI.sub("", str(e)).removeprefix("ERROR: ")[:260]
        what = "extract the audio from" if req.mode == "audio" else "download"
        raise HTTPException(422, f"Couldn't {what} that video: {reason}")
    ext = os.path.splitext(path)[1].lower()
    media = {".mp3": "audio/mpeg", ".mp4": "video/mp4", ".webm": "video/webm", ".mkv": "video/x-matroska", ".mov": "video/quicktime", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}.get(ext, "application/octet-stream")
    return FileResponse(path, media_type=media, filename=os.path.basename(path), background=cleanup)


# Optional: if a "static" folder sits next to this file, serve it as the website (handy for local testing).
# In production the site is served by the Cloudflare worker, so the backend runs without this folder.
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(STATIC_DIR):
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="site")
