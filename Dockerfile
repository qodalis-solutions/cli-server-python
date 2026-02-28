FROM python:3.12-slim AS build
WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src/ src/
RUN pip install --no-cache-dir .

COPY demo/ demo/

ENV PORT=8048
EXPOSE 8048

CMD ["python", "demo/main.py"]
