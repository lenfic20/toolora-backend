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

# Install order matters. First the newest yt-dlp nightly build on its own (nightlies get Instagram/TikTok/
# YouTube fixes first). Then its dependencies WITHOUT -U, so pip keeps that nightly and installs the exact
# yt-dlp-ejs version it was built against (YouTube's challenge solver must match) plus curl-cffi, which
# current yt-dlp needs to read Instagram by impersonating a real browser.
RUN pip install --no-cache-dir -U --pre --no-deps yt-dlp \
    && pip install --no-cache-dir "yt-dlp[default,curl-cffi]" fastapi "uvicorn[standard]" \
    && python -c "import yt_dlp, yt_dlp_ejs, curl_cffi; print('yt-dlp', yt_dlp.version.__version__, '| ejs', yt_dlp_ejs.version, '| curl_cffi', curl_cffi.__version__)"

COPY main.py .

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
