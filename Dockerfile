FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

COPY . /opt/clusterhop
RUN python -m pip install --no-cache-dir /opt/clusterhop \
    && install -m 0755 /opt/clusterhop/action/entrypoint.sh \
        /usr/local/bin/clusterhop-action-entrypoint \
    && install -m 0755 /opt/clusterhop/action/cleanup.sh \
        /usr/local/bin/clusterhop-action-cleanup

ENTRYPOINT ["/usr/local/bin/clusterhop-action-entrypoint"]
