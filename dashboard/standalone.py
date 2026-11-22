"""Standalone Zero Factory API and lightweight dashboard server."""

from __future__ import annotations

import argparse
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from dashboard.plugin_api import router

app = FastAPI(title="Zero Factory")
app.include_router(router, prefix="/api/plugins/zerofactory")


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return (Path(__file__).with_name("standalone.html")).read_text(encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the standalone Zero Factory dashboard"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9119)
    args = parser.parse_args()
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
