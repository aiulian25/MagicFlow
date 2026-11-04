# ── Stage 1: compile Tailwind to a static CSS file (F15) ──────────────────────
# Build-time only; the final image gains just the compiled tailwind.css. The
# content scanner reads static/index.html (incl. the #_safelist div listing
# JS-generated classes). Needs network for npm, like the python/apt stages.
FROM node:20-bookworm-slim@sha256:3d0f05455dea2c82e2f76e7e2543964c30f6b7d673fc1a83286736d44fe4c41c AS css
WORKDIR /build
COPY build/package.json build/package-lock.json ./
COPY app/tailwind.config.js .
COPY app/static/index.html ./static/index.html
RUN npm ci --ignore-scripts --no-audit --no-fund \
    && printf '@tailwind base;\n@tailwind components;\n@tailwind utilities;\n' > in.css \
    && npx tailwindcss -c tailwind.config.js -i in.css -o tailwind.css --minify

# ── Stage 2: the application image ────────────────────────────────────────────
FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337

# Install system dependencies
# ffmpeg provides the delegate ImageMagick-6 uses to (de)code APNG.
# ghostscript (gs) is the delegate ImageMagick uses to rasterize PDF pages
# (Extract / Re-convert of a MagicFlow-made PDF).
# Freeze the whole Debian dependency graph, retaining signature verification.
COPY scripts/fetch-debs.py /tmp/fetch-debs.py
RUN sed -i 's|http://deb.debian.org/debian-security|https://snapshot.debian.org/archive/debian-security/20261102T000000Z|; s|http://deb.debian.org/debian|https://snapshot.debian.org/archive/debian/20261102T000000Z|' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Check-Valid-Until=false -o Acquire::Retries=2 -o Acquire::https::Timeout=30 -o Acquire::ForceIPv4=true update \
    && python3 /tmp/fetch-debs.py imagemagick libwebp-dev webp ffmpeg ghostscript curl libavif-bin \
    && apt-get install -y --no-download --no-install-recommends imagemagick libwebp-dev webp ffmpeg ghostscript curl libavif-bin \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/* /tmp/fetch-debs.py

# Tune the ImageMagick-6 security policy:
#  - raise memory/map/disk limits for large image sets
#  - grant READ AND WRITE rights to the PDF coder (Debian denies both).
#    Both are required: coder policy gates the encoder as well as the decoder,
#    whether or not a delegate is involved, so read-only silently broke every
#    advertised PDF output. Write is ImageMagick's own PDF writer; ghostscript
#    is still only ever invoked to rasterize pages on READ.
#    Everything Debian denies for a reason stays denied — PS/PS2/PS3/EPS/XPS
#    (the ghostscript-executing coders this app never produces) and the remote
#    coders URL/HTTP/HTTPS.
# The assertions below replace a former blanket `|| true`: a policy edit that
# silently fails to match would ship an image whose PDF support is broken
# again, so the build fails instead.
RUN sed -i \
    -e 's/name="memory" value="[^"]*"/name="memory" value="2GiB"/' \
    -e 's/name="map" value="[^"]*"/name="map" value="4GiB"/' \
    -e 's/name="disk" value="[^"]*"/name="disk" value="8GiB"/' \
    -e '/pattern="PDF"/s/rights="[^"]*"/rights="read|write"/' \
    /etc/ImageMagick-6/policy.xml \
 && POLICY=/etc/ImageMagick-6/policy.xml \
 && grep -q 'name="memory" value="2GiB"' "$POLICY" \
 && grep -q 'name="map" value="4GiB"' "$POLICY" \
 && grep -q 'name="disk" value="8GiB"' "$POLICY" \
 && grep -q 'rights="read|write" pattern="PDF"' "$POLICY" \
 && for denied in PS PS2 PS3 EPS XPS URL HTTP HTTPS; do \
        grep -q "rights=\"none\" pattern=\"$denied\"" "$POLICY" \
            || { echo "policy assertion failed: $denied is no longer denied" >&2; exit 1; }; \
    done

# Append the app-owned decoder allowlist after distro rules; retain resource limits.
COPY app/imagemagick-policy.xml /tmp/magicflow-policy.xml
RUN python3 -c 'from pathlib import Path; p=Path("/etc/ImageMagick-6/policy.xml"); text=p.read_text(); assert text.count("</policymap>")==1; p.write_text(text.replace("</policymap>", Path("/tmp/magicflow-policy.xml").read_text()+"\n</policymap>"))' \
 && rm /tmp/magicflow-policy.xml

# Create non-root user
RUN groupadd -r appuser && useradd -r -g appuser -u 1000 appuser

# Create data directories owned by appuser
RUN mkdir -p /data/input /data/output /data/tmp /data/config \
    && chown -R appuser:appuser /data \
    && chmod 750 /data /data/input /data/output /data/tmp /data/config

# Prove the policy above does what the app advertises, in this image, as the
# unprivileged user that will run it: two stills must become a two-page PDF and
# rasterize back into two frames, while a ghostscript-executing coder the app
# never produces stays refused. A build that cannot do this has broken PDF
# modes, so it fails here instead of in someone's library.
RUN su appuser -s /bin/sh -c "set -e; cd /tmp; \
        convert -size 8x8 xc:red one.png; \
        convert -size 8x8 xc:blue two.png; \
        convert one.png two.png smoke.pdf; \
        identify -format '%n\n' smoke.pdf | head -1 | grep -qx 2; \
        convert -density 72 smoke.pdf -coalesce page_%02d.png; \
        test -f page_00.png && test -f page_01.png; \
        ! convert one.png denied.eps 2>/dev/null" \
    && rm -f /tmp/one.png /tmp/two.png /tmp/smoke.pdf /tmp/page_*.png

WORKDIR /app

# Install Python dependencies (as root for system site-packages)
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r requirements.txt && pip check

# Copy application source
COPY app/ .
COPY LICENSE /app/LICENSE

# Drop in the Tailwind CSS compiled in the css stage (self-hosted, no CDN)
COPY --from=css /build/tailwind.css static/vendor/tailwind.css

# Runtime source is read-only to the service user. Data belongs under /data.
RUN chown -R root:root /app && chmod -R go-w /app

# Copy entrypoint that initializes mount roots; nested repair is explicit.
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ARG VERSION=dev
ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="MagicFlow" \
      org.opencontainers.image.source="https://github.com/aiulian25/MagicFlow" \
      org.opencontainers.image.version=$VERSION \
      org.opencontainers.image.revision=$VCS_REF
ENV MAGICFLOW_VERSION=$VERSION MAGICFLOW_REVISION=$VCS_REF PYTHONDONTWRITEBYTECODE=1
COPY build/inputs.json build-inputs.json
COPY build/package.json build/package-lock.json build/requirements.in Dockerfile /app/build-locks/
COPY scripts/fetch-debs.py scripts/release_manifest.py /app/build-locks/
COPY scripts/release_manifest.py /tmp/release_manifest.py
RUN PYTHONPATH=/app python3 /tmp/release_manifest.py && rm /tmp/release_manifest.py

# Only mount initialization runs as root; entrypoint execs the service as appuser.
EXPOSE 3876

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -sf http://localhost:3876/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
# --timeout-graceful-shutdown bounds how long a SIGTERM waits for in-flight
# requests. Without it uvicorn waits indefinitely, so a long encode would hold
# shutdown open until Docker's own grace period ran out and SIGKILLed the
# container — and the app's shutdown hook, which stops running encoders, would
# never get to run. Eight seconds leaves room inside Docker's default ten.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "3876", "--workers", "1", \
     "--timeout-graceful-shutdown", "8"]
