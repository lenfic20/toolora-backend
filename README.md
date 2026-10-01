# Toolora

Browser-first PDF, image, video, and calculator tools, plus a social video downloader.

```
worker/    Cloudflare Worker. Serves the site (public/), handles accounts (/api/auth/*) in D1, and proxies /download.
backend/   FastAPI + yt-dlp downloader. Runs anywhere that can run Docker (Render, your own machine behind a Cloudflare Tunnel).
```

Visitors only ever talk to the Worker. The Worker forwards downloads to the backend with a shared secret and the visitor's IP, so the backend can rate-limit each person separately and refuse calls that do not come from the Worker.

## Accounts

- Email and password, or Google. Both end in the same session cookie (HttpOnly, SameSite=Lax, 30 days).
- Stored in D1: users, sessions (token hashes only), and short-lived sign-in attempt counters (hashed).
- Passwords use PBKDF2-SHA256 with 100,000 iterations, the maximum Cloudflare Workers allows.
- Google tokens are verified on the server against Google's published keys, including audience, issuer, expiry, and verified email.
- Linking Google to an existing email account removes that account's password and ends its other sessions. Emails are not verified at sign-up yet, so this stops someone from pre-registering an address they do not own.

## Set up

Run these inside `worker/`.

1. Create the database and paste the printed `database_id` into `wrangler.toml`:
   ```bash
   npx wrangler d1 create toolora
   npx wrangler d1 execute toolora --remote --file=schema.sql
   ```
2. Deploy the backend (`backend/Dockerfile`). Set the environment variable `PROXY_SECRET` to a long random string, for example the output of `openssl rand -hex 32`.
3. Put the backend address and the same secret on the Worker:
   - Set `BACKEND_URL` in `wrangler.toml` under `[vars]`.
   - `npx wrangler secret put PROXY_SECRET`
4. Google sign-in (optional, the button is hidden until this is set):
   - Google Cloud Console, APIs and Services, Credentials, create an OAuth client ID of type Web application.
   - Add your real site origins to Authorized JavaScript origins, for example `https://yourdomain` and `https://www.yourdomain`. No redirect URI is needed.
   - On the OAuth consent screen, link `/privacy.html` and `/terms.html`.
   - Put the client ID in `GOOGLE_CLIENT_ID` under `[vars]`.
5. `npx wrangler deploy`
6. Connect your custom domain: Workers and Pages, toolora, Settings, Domains and Routes, Add, Custom domain.

## Local development

```bash
# terminal 1
cd backend && pip install -r requirements.txt && uvicorn main:app --reload

# terminal 2
cd worker
cp .dev.vars.example .dev.vars
npx wrangler d1 execute toolora --local --file=schema.sql
npx wrangler dev
```

Open http://localhost:8787. FFmpeg must be on PATH for the downloader.

## Before launch

Do not launch until all of these are complete:

- Connect and verify the real custom domain, and confirm HTTPS.
- Keep the favicon at `/favicon.svg`.
- Replace the placeholder contact details in `privacy.html` and `terms.html` with the real operator details.
- Premium: the plans, prices, and perks are placeholders and checkout is not connected. Remove the Premium window and button, or connect and test a real payment provider and make every listed perk true.
- Currency calculator: the exchange rates are fixed sample numbers. Connect a live rates source or remove the tool.
- Forgot password and email verification are not built. Both need an email sending service.
- The free-use counter is stored in the browser, so it prompts people but does not enforce a limit.
- Test the downloader, sign up, sign in, sign out, Google sign-in, and every browser tool on desktop and mobile.
- Keep the site free of fake reviews, fake metrics, fake accounts, AI-generated imagery and copy, cursor effects, and excessive motion.
