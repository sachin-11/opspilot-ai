# Verification sandbox image for OpsPilot remediation (Module 6).
# Contains only language toolchains. No application code, credentials, or network clients are added.
# Containers started from it run with: --network none, --read-only, --cap-drop ALL,
# no-new-privileges, non-root uid 10001, and CPU/memory/PID limits (see remediation/sandbox.py).
FROM python:3.15-slim

COPY --from=node:24-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:24-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && pip install --no-cache-dir "pytest>=8" "ruff>=0.5" "mypy>=1.10" \
    && npm install --global --no-audit --no-fund typescript@5 \
    && groupadd --gid 10001 sandbox \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin sandbox

USER 10001:10001
WORKDIR /workspace
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
