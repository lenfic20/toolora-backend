# Toolora Render backend
# Python runs FastAPI/yt-dlp; Deno is copied from the official Deno binary image.
# This avoids the deno.land install script, which can fail on Render's build image
# when unzip/7z is not available.
FROM denoland/deno:bin-2.9.7 AS deno

FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Official Deno binary. yt-dlp currently recommends Deno for YouTube JS challenges.
COPY --from=deno /deno /usr/local/bin/deno
RUN chmod +x /usr/local/bin/deno && deno --version

ENV PATH="/usr/local/bin:${PATH}"
ENV DENO_DIR="/deno-dir"

WORKDIR /app

COPY requirements.txt .

# yt-dlp[default,curl-cffi] includes the matching yt-dlp-ejs package needed by current
# YouTube extraction. Install the latest stable package instead of a prerelease
# so Render builds remain reproducible against a released dependency.
RUN pip install --no-cache-dir -U "yt-dlp[default,curl-cffi]" fastapi "uvicorn[standard]"

COPY main.py .

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
