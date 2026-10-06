# Toolora update package

This package updates the existing Toolora site and keeps the existing Google/email account system and Render downloader.

## Included changes
- Premium button and Premium code removed from the visible site.
- New visitors see Sign in at the top right.
- Light/dark toggle is immediately to the left of Sign in; first visit follows the device's system theme.
- Signed-in users see a profile button at the top right.
- Signed-in favorites are stored in Cloudflare D1 through `/api/auth/favorites`, so the same account can retrieve favorites on PC, iOS, and Android.
- The second tool process still opens the sign-in prompt for signed-out visitors.
- Privacy and Terms pages use the same saved theme and Toolora logo.
- Page/tool transitions are animated with reduced-motion support.
- Password rules are enforced in both the browser and Worker.
- The `/api/auth/` route path is corrected to use the proper `/api/auth/` prefix length.

## Cloudflare Worker
The `src/index.js` file is the complete Worker based on the existing Google/email auth implementation, with the favorites endpoint added.

The Worker automatically creates the `favorites` table if it is missing. The included `schema.sql` also contains the table definition.

Deploy from the package root with:

```powershell
npx.cmd wrangler deploy
```

If your current Worker already has the same D1 database, do not create a new database.

## Render backend
The backend files are included under `backend/` and contain the Deno/yt-dlp YouTube fix from v12.

For Render, replace the backend files with the files in `backend/` and deploy the existing `toolora-backend` service. Use **Clear build cache & deploy** if Render offers that option.

## Important
Do not replace or delete the existing D1 database. The Worker uses the existing database ID in `wrangler.toml`.

The `PROXY_SECRET` remains a Worker secret and is intentionally not included in this package. Keep the existing secret configured on both Cloudflare and Render.

## Downloader fix (Instagram and other links)
- `backend/Dockerfile` and both `requirements.txt` files now install `yt-dlp[default,curl-cffi]`. Current yt-dlp needs `curl-cffi` to read Instagram; without it Instagram links fail. The image also upgrades yt-dlp to its newest nightly build.
- `backend/main.py` retries Instagram with several strategies and finally reads the public embed page directly. It also handles `instagr.am` and `instagram.com/share/...` links, photo posts, and never returns an empty error message.
- Redeploy the Render service with **Clear build cache & deploy**. The Cloudflare site needs a redeploy too (`public/index.html` changed).
- Optional Render environment variables (only if some links still say "login required" or "sign in to confirm"):
  - `COOKIES_TXT`: browser cookies in Netscape `cookies.txt` format from a throwaway Instagram/YouTube account (or upload the file as a Render Secret File named `cookies.txt`).
  - `YTDLP_PROXY`: a residential proxy URL such as `http://user:pass@host:port`.

## Speed
- `wrangler.toml` now has a cron trigger and `src/index.js` a `scheduled` handler that pings the Render backend every 2 minutes, so the free Render server stays awake (a cold start takes 30-60 s). Opening a social tool also warms it via `/api/warm`. Redeploy the worker with `npx.cmd wrangler deploy`.
- TikTok, Instagram and X now fetch a ready-made MP4 instead of merging separate audio and video, which removes a slow ffmpeg step.
- Instagram audio is fetched in the background, so the video appears without waiting for it.

## YouTube fix
- `backend/Dockerfile` installs yt-dlp nightly first and then its matching dependencies (the previous order could leave a mismatched `yt-dlp-ejs`, which YouTube's challenge solver needs). The build now fails loudly if yt-dlp, yt-dlp-ejs or curl-cffi is missing.
- `backend/main.py` tries yt-dlp's own default YouTube client first, then ten other clients, each with one fast format chain. Downloads are capped at 1080p H.264/AAC so they play on phones and stay under the size limit; if a video is still too big it falls back to 720p, 480p and then "best".
- Private, removed or copyright-blocked videos stop immediately with a clear message.
- Failures are written to the Render logs as `[toolora] download failed ...` with the full reason.
- If YouTube answers "Sign in to confirm you're not a bot", YouTube is blocking Render's datacenter IP. No code change can fully fix that; set `COOKIES_TXT` (cookies exported from a throwaway Google account) or `YTDLP_PROXY` (a residential proxy) on Render.

## YouTube no longer hangs
- YouTube methods that need no heavy JavaScript solving now go first (fast on the small Render server). The server gives up after `MAX_SECONDS` (default 80) or after YouTube blocks four methods in a row, and shows a clear message instead of spinning forever.
- The website also stops waiting after 150 seconds and shows "This is taking too long. Please try again in a moment."

## Page behavior
- The downloader button says **Start** (other tools keep "Calculate" / "Process").
- Refreshing a tool page keeps you on that tool (signing-in checks no longer redraw the home page). Favoriting from a tool page also keeps you on it. "Back to all tools" always goes home.

## Daily reminder notification (signed-in users)
- Signed-in users see a small "Daily reminder" prompt once; if they tap **Turn on** and allow notifications, they get one notification every 24 hours. Signing out stops them.
- Needs one secret on the worker (the matching public key is already in `wrangler.toml`):
  `npx.cmd wrangler secret put VAPID_PRIVATE_KEY`  (paste the private key you were given)
- Then redeploy: `npx.cmd wrangler deploy`. The `push_subscriptions` table is created automatically (it is also in `schema.sql`).
- Without the secret, the prompt simply never appears and nothing else changes.
- Works in Chrome/Edge/Firefox on Android and desktop. iPhone/iPad only allow web notifications for sites added to the Home Screen.

## Picture slides (TikTok and Instagram)
- TikTok photo slideshows and Instagram posts with several pictures/videos now show a swipeable viewer (arrows on desktop). Each picture or video has its own **Download** button. Posts that mix pictures and videos show all of them in order.
- Normal single videos work exactly as before. Instagram reels skip the extra check.
- The backend has two new modes on the same `/download` route (`info` lists the post's media, `file` fetches one picture/video from a TikTok, Instagram or Snapchat media host only), so the Cloudflare worker needs no routing change.
- If TikTok's own page can't be read from Render's server, the backend asks tikwm.com for the picture list as a backup (pictures only).

## Snapchat Spotlight
- New **Snapchat** tool for Spotlight links (`snapchat.com/spotlight/...` and `snapchat.com/t/...` short links). The video is the original file served on the public page, so it has no watermark.

## YouTube: optional YouTube-only proxy
- `YT_PROXY` (Render environment variable, optional): a proxy URL used only for YouTube links, so a paid residential proxy is not used for TikTok/Instagram/X/Snapchat. If it is not set, nothing changes. `YTDLP_PROXY` still applies to everything.

