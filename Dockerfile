FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /app
# Dependencies resolve from the lockfile alone, so they cache independently of application code.
# There is no browser layer in this image: the Ad Library is read with curl_cffi's Chrome TLS
# impersonation, which is a ~10 MB wheel, not a 1.45 GB Chromium install.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

RUN useradd --create-home scraper && chown -R scraper:scraper /app

# Everything below is invalidated by a code change, so keep it small.
COPY --chown=scraper:scraper src ./src
RUN uv sync --frozen --no-dev

USER scraper

EXPOSE 8003
HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
  CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8003') + '/health').status==200 else 1)"
CMD ["uv", "run", "--no-sync", "facebook-ad-library", "serve"]
