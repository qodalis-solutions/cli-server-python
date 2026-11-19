FROM python:3.12-slim
WORKDIR /app

COPY packages/abstractions/ packages/abstractions/
RUN pip install --no-cache-dir ./packages/abstractions

RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm \
    && rm -rf /var/lib/apt/lists/*

COPY plugins/ plugins/

# Copy dashboard SPA into admin plugin before installing
RUN cd plugins/admin && npm init -y && npm install @qodalis/cli-server-dashboard \
    && python3 scripts/copy_dashboard.py \
    && rm -rf node_modules package.json package-lock.json

# Copy the portal site into the portal plugin (skipped until the package is published)
RUN cd plugins/portal && (npm install && python3 scripts/copy_portal.py || true) \
    && rm -rf node_modules package-lock.json

RUN pip install --no-cache-dir ./plugins/filesystem \
    && pip install --no-cache-dir ./plugins/filesystem-json \
    && pip install --no-cache-dir ./plugins/filesystem-sqlite \
    && (pip install --no-cache-dir ./plugins/filesystem-s3 || true) \
    && pip install --no-cache-dir ./plugins/weather \
    && pip install --no-cache-dir ./plugins/jobs \
    && pip install --no-cache-dir ./plugins/audit \
    && pip install --no-cache-dir ./plugins/users \
    && pip install --no-cache-dir ./plugins/data-explorer \
    && pip install --no-cache-dir ./plugins/data-explorer-sql \
    && pip install --no-cache-dir ./plugins/data-explorer-mongo \
    && pip install --no-cache-dir ./plugins/data-explorer-postgres \
    && pip install --no-cache-dir ./plugins/data-explorer-mysql \
    && pip install --no-cache-dir ./plugins/data-explorer-mssql \
    && pip install --no-cache-dir ./plugins/data-explorer-redis \
    && pip install --no-cache-dir ./plugins/data-explorer-elasticsearch \
    && pip install --no-cache-dir ./plugins/admin \
    && pip install --no-cache-dir ./plugins/public-api \
    && pip install --no-cache-dir ./plugins/docs \
    && (pip install --no-cache-dir ./plugins/portal || pip install --no-cache-dir -e ./plugins/portal)

COPY pyproject.toml README.md LICENSE ./
COPY src/ src/
RUN pip install --no-cache-dir .

COPY demo/ demo/

ENV PORT=8048
ENV HOST=0.0.0.0
EXPOSE 8048

CMD ["python", "demo/main.py"]
