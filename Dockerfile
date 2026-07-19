FROM python:3.13-slim-bookworm

WORKDIR /app

# Install system dependencies for Xvfb, fonts, and locale
# SeleniumBase with use_chromium=True downloads its own Chromium binary,
# so we don't need to install Google Chrome separately.
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    xvfb \
    python3-tk \
    python3-dev \
    python3-xlib \
    scrot \
    curl \
    unzip \
    locales \
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

COPY . .

# Railway sets PORT env var automatically
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
