FROM python:3.13-bookworm

WORKDIR /app

# Xvfb + Chromium runtime deps + fonts (same baseline as the upstream solver).
RUN apt-get update && apt-get install -y \
    wget \
    curl \
    unzip \
    gnupg \
    xvfb \
    python3-tk \
    python3-dev \
    python3-xlib \
    scrot \
    locales \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libatspi2.0-0 \
    libgtk-3-0 \
    libdbus-1-3 \
    fonts-liberation \
    fonts-noto \
    fonts-noto-color-emoji \
    fonts-indic \
    && rm -rf /var/lib/apt/lists/*

RUN sed -i '/en_US.UTF-8/s/^# //g' /etc/locale.gen && locale-gen
ENV LANG=en_US.UTF-8
ENV LC_ALL=en_US.UTF-8

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download Chromium drivers at build time.
RUN sbase get chromedriver --path && sbase get uc_driver --path || true

# Bundle Xray so cold starts do not need a download.
ARG XRAY_VERSION=1.8.7
RUN mkdir -p /app/vendor/xray \
    && curl -L "https://github.com/XTLS/Xray-core/releases/download/v${XRAY_VERSION}/Xray-linux-64.zip" -o /tmp/xray.zip \
    && unzip -o /tmp/xray.zip xray -d /app/vendor/xray \
    && chmod +x /app/vendor/xray/xray \
    && rm -f /tmp/xray.zip

ENV XRAY_DIR=/app/vendor/xray
ENV XRAY_BINARY=/app/vendor/xray/xray

COPY . .

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
