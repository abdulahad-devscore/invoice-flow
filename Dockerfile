ARG REGISTRY=docker.io
FROM ${REGISTRY}/library/python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[demo,textract]"
COPY scripts ./scripts
RUN useradd --create-home flow
USER flow
EXPOSE 8200
CMD ["invoice-flow", "api"]
