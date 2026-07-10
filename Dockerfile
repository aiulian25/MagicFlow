# ── Stage 1: compile Tailwind to a static CSS file (F15) ──────────────────────
# Build-time only; the final image gains just the compiled tailwind.css. The
# content scanner reads static/index.html (incl. the #_safelist div listing
# JS-generated classes). Needs network for npm, like the python/apt stages.
FROM node:20-slim AS css
WORKDIR /build
COPY app/tailwind.config.js .
COPY app/static/index.html ./static/index.html
RUN npm install --no-save tailwindcss@3 @tailwindcss/forms \
    && printf '@tailwind base;\n@tailwind components;\n@tailwind utilities;\n' > in.css \
    && npx tailwindcss -c tailwind.config.js -i in.css -o tailwind.css --minify

# ── Stage 2: the application image ────────────────────────────────────────────
FROM python:3.12-slim-bookworm

# Install system dependencies
# ffmpeg provides the delegate ImageMagick-6 uses to (de)code APNG.
# ghostscript (gs) is the delegate ImageMagick uses to rasterize PDF pages
# (Extract / Re-convert of a MagicFlow-made PDF).
RUN apt-get update && apt-get install -y --no-install-recommends \
    imagemagick \
    libwebp-dev \
    webp \
    ffmpeg \
    ghostscript \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Tune the ImageMagick-6 security policy:
#  - raise memory/map/disk limits for large image sets
#  - grant READ-only rights to the PDF coder (blocked by default in Debian).
#    Read-only is deliberate: IM writes PDF natively (no gs needed for output),
#    so we never grant write; this is the minimum needed to split/re-convert a
#    PDF and keeps the ghostscript attack surface as small as possible.
RUN sed -i \
    -e 's/name="memory" value="[^"]*"/name="memory" value="2GiB"/' \
    -e 's/name="map" value="[^"]*"/name="map" value="4GiB"/' \
    -e 's/name="disk" value="[^"]*"/name="disk" value="8GiB"/' \
    -e '/pattern="PDF"/s/rights="none"/rights="read"/' \
    /etc/ImageMagick-6/policy.xml || true

# Create non-root user
RUN groupadd -r appuser && useradd -r -g appuser -u 1000 appuser

# Create data directories owned by appuser
RUN mkdir -p /data/input /data/output /data/tmp \
    && chown -R appuser:appuser /data \
    && chmod 750 /data /data/input /data/output /data/tmp

WORKDIR /app

# Install Python dependencies (as root for system site-packages)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY app/ .

# Drop in the Tailwind CSS compiled in the css stage (self-hosted, no CDN)
COPY --from=css /build/tailwind.css static/vendor/tailwind.css

# Fix ownership
RUN chown -R appuser:appuser /app

# Copy entrypoint script that fixes bind-mount permissions at startup
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Run as root so entrypoint can chown /data, then drops to appuser
EXPOSE 3876

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -sf http://localhost:3876/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "3876", "--workers", "1"]
