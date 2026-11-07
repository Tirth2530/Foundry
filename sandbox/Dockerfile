# FOUNDRY execution sandbox: generated agents run here with no network and no credentials.
FROM python:3.11-slim
RUN pip install --no-cache-dir "uagents>=0.22" "uagents-core>=0.3" \
    && useradd -m -u 10001 runner
USER runner
WORKDIR /work
