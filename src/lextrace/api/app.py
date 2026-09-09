"""FastAPI application and process health endpoint."""

from fastapi import FastAPI

from lextrace.config import APP_TITLE

app = FastAPI(title=APP_TITLE)


@app.get("/health")
def health() -> dict[str, str]:
    """Report process health without external dependency checks."""
    return {"status": "ok"}
