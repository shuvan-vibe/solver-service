FROM python:3.12-slim-bookworm

WORKDIR /app

# Install dependencies needed by Playwright and SeleniumBase
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers (Chromium only needed)
RUN playwright install chromium
RUN playwright install-deps chromium

# Download SeleniumBase UC chrome driver
RUN seleniumbase install chromedriver

COPY . .


# Run Uvicorn server using Xvfb (virtual display) since SeleniumBase needs a display for non-headless UC mode
# We use shell form (sh -c) so that the dynamic $PORT environment variable provided by Railway is evaluated
CMD sh -c "xvfb-run --server-args=\"-screen 0 1024x768x24\" uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"
