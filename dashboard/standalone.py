"""Standalone Zero Factory API and lightweight dashboard server."""

from __future__ import annotations

import argparse
import ipaddress
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from dashboard.plugin_api import router


def _loopback_host(value: str) -> str:
    """Accept only literal loopback IP addresses for standalone binding."""
    try:
        if ipaddress.ip_address(value).is_loopback:
            return value
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("--host must be a loopback IP address")


def _origin_parts(value: str) -> tuple[str, str, int | None] | None:
    """Parse a serialized HTTP origin into comparable scheme/host/port parts."""
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or value != value.strip()
        ):
            return None
        scheme = parsed.scheme.lower()
        port = parsed.port
        if port is None:
            port = {"http": 80, "https": 443}[scheme]
        return scheme, parsed.hostname.lower(), port
    except ValueError:
        return None


app = FastAPI(title="Zero Factory")
app.include_router(router, prefix="/api/plugins/zerofactory")
_ASSET_DIR = Path(__file__).with_name("standalone_assets")


@app.middleware("http")
async def guard_cross_origin_mutations(request: Request, call_next):
    """Reject cross-origin browser mutations while preserving headerless API clients."""
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        request_origin = _origin_parts(f"{request.url.scheme}://{request.url.netloc}")
        fetch_site = request.headers.get("sec-fetch-site", "").lower()
        if fetch_site in {"cross-site", "same-site"} or (
            origin is not None and _origin_parts(origin) != request_origin
        ):
            return JSONResponse(
                status_code=403,
                content={"detail": "Cross-origin mutations are not allowed"},
            )
    return await call_next(request)


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return (Path(__file__).with_name("standalone.html")).read_text(encoding="utf-8")


@app.get("/assets/style.css")
def dashboard_styles() -> Response:
    styles = (
        Path(__file__)
        .with_name("dist")
        .joinpath("style.css")
        .read_text(encoding="utf-8")
    )
    return Response(styles, media_type="text/css")


app.mount("/assets", StaticFiles(directory=_ASSET_DIR), name="standalone-assets")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the standalone Zero Factory dashboard"
    )
    parser.add_argument("--host", type=_loopback_host, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9119)
    args = parser.parse_args()
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
