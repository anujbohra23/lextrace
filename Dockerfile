FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN useradd --create-home --uid 10001 lextrace
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install '.[retrieval,research]'
RUN mkdir -p /app/runtime && chown lextrace:lextrace /app/runtime
USER lextrace
EXPOSE 8000
CMD ["uvicorn", "lextrace.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
