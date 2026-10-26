import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

import database
from api_client import APIClient
from config import load_config
from handlers.common import normalize_mobile

config = load_config()
_rate_windows: dict[str, deque] = defaultdict(deque)


def _check_rate(key_prefix: str) -> bool:
    now = time.monotonic()
    q = _rate_windows[key_prefix]
    while q and now - q[0] >= 60:
        q.popleft()
    limit = int(getattr(config, "api_user_rate_limit_per_minute", 60))
    if len(q) >= limit:
        return False
    q.append(now)
    return True


def _authorize_api_key(x_api_key: str | None, endpoint: str = "") -> dict:
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing API key")

    key_row = database.validate_api_key(x_api_key)
    if not key_row:
        # Do not reveal whether a key once existed or has expired.
        raise HTTPException(status_code=401, detail="Invalid or expired API key")

    prefix = key_row.get("key_prefix", "unknown")
    if not _check_rate(prefix):
        try:
            database.record_api_request(key_row, endpoint, rate_limited=True)
        except Exception:
            pass
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    try:
        database.record_api_request(key_row, endpoint, rate_limited=False)
    except Exception:
        # Usage accounting must never break a valid API request.
        pass
    return key_row


def _records_from_upstream(data) -> list[dict]:
    """Normalize one direct upstream lookup response into a public records list."""
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]

    if isinstance(data, dict):
        results = data.get("results")
        if isinstance(results, list):
            return [item for item in results if isinstance(item, dict)]
        return [data]

    return []


def _raise_for_upstream_status(status: str, invalid_message: str):
    if status == "not_found":
        raise HTTPException(status_code=404, detail="No records found")
    if status == "invalid":
        raise HTTPException(status_code=400, detail=invalid_message)
    if status in {"timeout", "unavailable"}:
        raise HTTPException(status_code=503, detail="Lookup service unavailable")
    if status == "auth_failed":
        raise HTTPException(status_code=502, detail="Upstream authentication failed")
    raise HTTPException(status_code=502, detail="Lookup failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    client = APIClient(config.api_url, config.api_key)
    await client.start()
    app.state.upstream = client
    yield
    await client.close()


app = FastAPI(
    title="Lookup API",
    version="1.1.0",
    description="Authenticated direct lookup API for active monthly API plans.",
    lifespan=lifespan,
)


@app.get("/")
async def root():
    return {"status": "ok", "service": "lookup-api"}


@app.get("/api/lookup")
async def lookup_mobile(
    request: Request,
    number: str = Query(..., description="10-digit Indian mobile number"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    _authorize_api_key(x_api_key, "/api/lookup")

    mobile = normalize_mobile(number)
    if not mobile:
        raise HTTPException(status_code=400, detail="Invalid mobile number")

    result = await request.app.state.upstream.lookup_mobile(mobile)
    status = result.get("status")
    if status != "ok":
        _raise_for_upstream_status(status, "Invalid mobile number")

    records = _records_from_upstream(result.get("data"))
    if not records:
        raise HTTPException(status_code=404, detail="No records found")

    return {
        "success": True,
        "type": "mobile",
        "query": mobile,
        "count": len(records),
        "data": records,
    }


@app.get("/api/id")
async def lookup_id(
    request: Request,
    id: str = Query(..., description="Exact 12-digit record ID"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    _authorize_api_key(x_api_key, "/api/id")

    record_id = str(id or "").strip()
    if not record_id.isdigit() or len(record_id) != 12:
        raise HTTPException(status_code=400, detail="ID must be exactly 12 digits")

    result = await request.app.state.upstream.lookup_id(record_id)
    status = result.get("status")
    if status != "ok":
        _raise_for_upstream_status(status, "ID must be exactly 12 digits")

    records = _records_from_upstream(result.get("data"))
    if not records:
        raise HTTPException(status_code=404, detail="No records found")

    return {
        "success": True,
        "type": "id",
        "query": record_id,
        "count": len(records),
        "data": records,
    }


@app.exception_handler(HTTPException)
async def http_error(_request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"success": False, "error": exc.detail},
    )
