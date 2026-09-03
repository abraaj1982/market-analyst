FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e .

COPY config ./config
COPY web ./web
COPY seeds ./seeds
COPY scripts ./scripts

RUN mkdir -p /app/data
ENV ANALYST_DATABASE_URL=sqlite:////app/data/analyst.db

EXPOSE 8000
CMD ["sh", "-c", "analyst serve --host 0.0.0.0 --port ${PORT:-8000}"]
