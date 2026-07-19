FROM python:3.13-bookworm

WORKDIR /app

# Install system dependencies:
# - Xvfb + display: virtual framebuffer for headed browser in container
# - Chromium runtime libs: libnss3, libgbm1, libasound2, etc. (needed by SeleniumBase's Chromium)
# - Fonts: realistic font rendering (missing fonts = bot signal)
# - Locale: en_US.UTF-8 (mismatched locale = bot signal)
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
    # Chromium runtime dependencies (critical for slim/container images)
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
    # Fonts
    fonts-liberation \
    fonts-noto \
    fonts-noto-color-emoji \
    fonts-indic \
    && rm -rf /var/lib/apt/lists/*

# Set locale (critical for stealth — mismatched locale is a bot signal)
RUN sed -i '/en_US.UTF-8/s/^# //g' /etc/locale.gen && locale-gen
ENV LANG=en_US.UTF-8
ENV LC_ALL=en_US.UTF-8

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download Chromium binary during build time to eliminate first-run delay.
RUN sbase get chromedriver --path && sbase get uc_driver --path || true

COPY . .

# Railway sets PORT env var automatically
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
