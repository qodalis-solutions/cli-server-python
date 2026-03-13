FROM python:3.12-slim
WORKDIR /app

COPY packages/abstractions/ packages/abstractions/
RUN pip install --no-cache-dir ./packages/abstractions

COPY plugins/ plugins/
RUN pip install --no-cache-dir ./plugins/filesystem \
    && pip install --no-cache-dir ./plugins/filesystem-json \
    && pip install --no-cache-dir ./plugins/filesystem-sqlite \
    && pip install --no-cache-dir ./plugins/weather

COPY pyproject.toml README.md LICENSE ./
COPY src/ src/
RUN pip install --no-cache-dir .

ENV PORT=8048
ENV HOST=0.0.0.0
EXPOSE 8048

CMD ["qodalis-cli-server"]
