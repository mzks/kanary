FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN python -m pip install --upgrade pip && \
    python -m pip install .

RUN mkdir -p /etc/kanary/plugins /var/lib/kanary

EXPOSE 8000

CMD ["kanary", "/etc/kanary/plugins", "--state-db", "/var/lib/kanary/kanary.db", "--api-port", "8000"]
