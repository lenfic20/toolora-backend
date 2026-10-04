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
import asyncio, hmac, os, re, shutil, tempfile, time, subprocess
from typing import Literal
from collections import defaultdict, deque
from urllib.parse import urlparse

import yt_dlp
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
    if not is_youtube(url):
        return formats

    # YouTube's extractor changes frequently. Try current clients in a deliberate order.
    clients = ["mweb", "ios", "android", "android_vr", "tv", "web_embedded", "web_safari", "web", "tv_embedded"]
    out = []
    for client in clients:
        for item in formats[:4 if mode == "video" else 1]:
            x = dict(item)
            x["extractor_args"] = {"youtube": {"player_client": [client]}}
            x["remote_components"] = {"ejs:github", "ejs:npm"}
            out.append(x)
    # Final generic attempts let yt-dlp choose a newly supported client.
    out.extend({**x, "remote_components": {"ejs:github", "ejs:npm"}} for x in formats)
    return out


def pick_file(folder: str, mode: str) -> str | None:
    want = (".mp3",) if mode == "audio" else (".mp4", ".mkv", ".webm", ".mov", ".jpg", ".jpeg", ".png", ".webp", ".gif")
    files = [f for f in os.listdir(folder) if f.lower().endswith(want) and not FRAGMENT.search(f)]
    if not files:
        return None
    return os.path.join(folder, max(files, key=lambda f: os.path.getsize(os.path.join(folder, f))))


def fetch(url: str, folder: str, mode: str = "video") -> str:
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
        "remote_components": {"ejs:github", "ejs:npm"},
        "js_runtimes": {"deno": {}},
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        },
    }
    last = None
    for extra in attempts_for(mode, url):
        clear_folder(folder)
        try:
            with yt_dlp.YoutubeDL({**base, **extra}) as ydl:
                ydl.download([url])
        except yt_dlp.utils.DownloadError as e:
            msg = str(e).lower()
            if is_youtube(url) and any(x in msg for x in ("player response", "sign in to confirm", "requested format", "unable to extract", "http error 400", "http error 403", "http error 429", "forbidden", "po token", "javascript", "challenge")):
                last = e
                continue
            # Different public posts can expose different media formats. Keep trying the
            # remaining extractor/format strategies before returning an error.
            last = e
            continue
        path = pick_file(folder, mode)
        if path:
            if mode == "video":
                ext = os.path.splitext(path)[1].lower()
                if ext in (".webm", ".mkv", ".mov"):
                    mp4 = os.path.join(folder, os.path.splitext(os.path.basename(path))[0] + ".mp4")
                    try:
                        subprocess.run(
                            [
                                "ffmpeg", "-y", "-i", path,
                                "-map", "0:v:0", "-map", "0:a?",
                                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                                "-c:a", "aac", "-b:a", "128k",
                                "-movflags", "+faststart", mp4
                            ],
                            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180
                        )
                        if os.path.exists(mp4) and os.path.getsize(mp4) > 0:
                            return mp4
                    except (subprocess.SubprocessError, OSError):
                        pass
            return path
    if last:
        raise last
    raise RuntimeError("no file produced (it may exceed the size limit)")


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
