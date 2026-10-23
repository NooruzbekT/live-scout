FROM python:3.13.5-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Almaty \
    LANG=C.UTF-8 \
    HOME=/tmp \
    DISPLAY=:99 \
    TTMON_CONFIG_OVERLAY=docker/config.docker.yaml

# Xvfb — Chrome с окном, как на Windows; x11vnc + noVNC — только для входа в TikTok
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        xvfb x11vnc novnc websockify \
        fonts-dejavu-core fonts-noto-core fonts-noto-color-emoji tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt \
    && playwright install --with-deps chrome \
    && rm -rf /var/lib/apt/lists/*

COPY --chmod=755 docker/entrypoint.sh /usr/local/bin/ttmon-entrypoint
# сборка из Windows-копии: CRLF в shebang даёт «No such file or directory»
RUN sed -i 's/\r$//' /usr/local/bin/ttmon-entrypoint
COPY . .

USER 1000:1000
EXPOSE 8765
ENTRYPOINT ["ttmon-entrypoint"]
CMD ["serve"]
