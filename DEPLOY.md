# Toolora Render backend — YouTube build fix

The previous Dockerfile failed while running:

`curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh`

Render's build environment did not have `unzip` or `7z`, so the Deno installer exited with code 1.

This version copies Deno directly from the official `denoland/deno:bin-2.9.7` image instead. Deno's official Docker documentation supports this binary-image approach, and yt-dlp's current EJS documentation recommends Deno for YouTube extraction.

## Deploy

Replace the files in the Render backend repository/service with:

- `Dockerfile`
- `requirements.txt`
- `main.py`

Then trigger a new Render deploy with **Clear build cache & deploy** if that option is available.

Do not create a new Render service. Keep the existing `toolora-backend` service and its environment variables.

After deployment, open:

`https://toolora-backend-thze.onrender.com/health`

It should return JSON containing `"ok": true`.
