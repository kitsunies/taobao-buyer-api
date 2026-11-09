FROM mcr.microsoft.com/playwright/python:v1.64.0-noble@sha256:07c5362a4512ffca347cc5d1a888ff5e6375ff6b9314e97813effcc0fc7d5593

LABEL org.opencontainers.image.source="https://github.com/kitsunies/taobao-buyer-api" \
      org.opencontainers.image.licenses="MIT"
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/home/buyer
WORKDIR /app
RUN groupadd --gid 10001 buyer && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin buyer
COPY pyproject.toml README.md LICENSE requirements.lock ./
COPY src ./src
RUN python -m pip install --no-cache-dir -r requirements.lock && python -m pip install --no-cache-dir --no-deps .
COPY scripts/sandbox_check.py scripts/offline_check.py ./scripts/
USER 10001:10001
EXPOSE 8787
CMD ["/bin/sh", "-c", "python scripts/sandbox_check.py && exec taobao-buyer --profile /data/profile serve --host 0.0.0.0 --port 8787 --token-file /run/secrets/token"]
