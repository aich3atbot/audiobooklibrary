# ffmpeg, built with everything disabled but what the MP3 -> M4B conversion
# uses. Debian's ffmpeg package would add 434 MB to the image, of which 190 MB
# is the Mesa 3D stack (libLLVM, libgallium, libz3) dragged in via libavdevice
# -> libplacebo -> Vulkan; /usr/bin/ffmpeg itself is 1 MB. This yields a 2 MB
# static binary instead, and takes about 90 seconds to build.
#
# The flag list is exact, and a missing entry fails loudly at encode time:
#   ffmetadata demuxer  - chapters are handed to ffmpeg as an input file
#   null muxer + pcm_s16le - the duration-measuring pass decodes into these
#   concat filter + aformat - inputs are routinely mixed rate/channel count
# No image codecs: cover art is written by mutagen, not ffmpeg.
FROM debian:trixie-slim AS ffmpeg-build
RUN apt-get update && apt-get install -y --no-install-recommends \
        git build-essential yasm nasm pkg-config ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch n7.1.1 https://github.com/FFmpeg/FFmpeg.git /src
WORKDIR /src
RUN ./configure \
      --disable-everything --disable-doc --disable-network --disable-autodetect \
      --disable-debug --disable-shared --enable-static --enable-small \
      --disable-ffplay --disable-ffprobe --disable-swscale --disable-postproc \
      --enable-decoder=mp3,mp3float,aac,aac_fixed,alac \
      --enable-encoder=aac,pcm_s16le \
      --enable-demuxer=mp3,mov,concat,ffmetadata \
      --enable-muxer=mp4,ipod,null \
      --enable-parser=mpegaudio,aac \
      --enable-protocol=file,pipe,concat,concatf \
      --enable-filter=aresample,aformat,concat,anull,atrim,aselect,anullsrc \
      --enable-bsf=aac_adtstoasc \
    && make -j"$(nproc)" ffmpeg \
    && strip ffmpeg

FROM python:3.12-slim

COPY --from=ffmpeg-build /src/ffmpeg /usr/local/bin/ffmpeg
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY alembic/ ./alembic/
COPY app/ ./app/
RUN uv sync --frozen --no-dev

ENV PATH="/opt/venv/bin:$PATH"

VOLUME ["/config", "/downloads", "/audiobooks", "/imports"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"

# app.main:asgi = FastAPI wrapped with the socket.io shim for ABS clients
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:asgi --host 0.0.0.0 --port 8000"]
