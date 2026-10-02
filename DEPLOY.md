# Toolora downloader backend update

This update is for the existing Render `toolora-backend` service.

It keeps the existing API and adds:
- stronger YouTube extraction retries using current yt-dlp EJS/Deno support;
- additional current YouTube player clients;
- support for Instagram video/photo responses;
- audio mode used by the updated Toolora frontend;
- browser-friendly media headers;
- continued support for YouTube, TikTok, Instagram and X.

Replace the existing backend files with `Dockerfile`, `main.py`, and `requirements.txt`, then deploy the existing Render service.

Use **Manual Deploy -> Clear build cache & deploy** if Render keeps an older image.

After deployment, check:
`https://toolora-backend-thze.onrender.com/health`

It should return `{"ok":true}`.

Important limitation: no downloader can guarantee every YouTube/Instagram URL. Private, members-only, age-restricted, login-required, geo-blocked, or otherwise restricted media can require authentication/cookies or platform-specific tokens. Public supported URLs are what this backend is designed to fetch.


### v14 fix
The backend now uses the current yt-dlp Python API format for JavaScript runtimes (`js_runtimes` is a dict such as `{"deno": {}}`) and remote components (a set). This fixes the `Invalid js_runtimes format` error shown by the downloader. The existing YouTube/TikTok/Instagram/X download logic is otherwise preserved.
