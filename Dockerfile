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

# yt-dlp[default] includes the matching yt-dlp-ejs package needed by current
# YouTube extraction. The curl-cffi extra is REQUIRED for Instagram (and helps
# TikTok): current yt-dlp reads Instagram by impersonating a real browser, which
# only works when curl-cffi is installed. The second command upgrades only the
# yt-dlp package itself to its newest nightly build (no pre-release dependencies),
# because Instagram/TikTok/YouTube fixes land there first.
RUN pip install --no-cache-dir -U "yt-dlp[default,curl-cffi]" fastapi "uvicorn[standard]" \
    && pip install --no-cache-dir -U --pre --no-deps yt-dlp \
    && python -c "import yt_dlp, curl_cffi; print('yt-dlp', yt_dlp.version.__version__, '| curl_cffi', curl_cffi.__version__)"

COPY main.py .

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
