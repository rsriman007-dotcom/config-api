
import json
import time
import uuid
import hmac
import os

from collections import deque, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import redis
import yaml

from dotenv import dotenv_values
from fastapi import FastAPI, Query, Request, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from prometheus_client import Counter, generate_latest, CONTENT_TYPE_LATEST


app = FastAPI()

START_TIME = time.monotonic()

REQUEST_COUNTER = Counter(
    "http_requests_total",
    "Total number of HTTP requests"
)

REQUEST_LOGS = deque(maxlen=1000)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
redis_client = redis.Redis.from_url(
    REDIS_URL,
    decode_responses=True
)

BASE_DIR = Path(__file__).resolve().parent

DEFAULTS = {
    "port": 8000,
    "workers": 1,
    "debug": False,
    "log_level": "info",
    "api_key": "default-secret-000",
}

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def normalize(layer, env_layer=False):
    result = {}

    for key, value in layer.items():
        if value is None:
            continue

        key = str(key).lower()

        if env_layer and key == "num_workers":
            key = "workers"
        elif key.startswith("app_"):
            key = key[4:]

        if key in ("port", "workers"):
            value = int(value)
        elif key == "debug":
            value = str(value).strip().lower() in (
                "true", "1", "yes", "on"
            )
        else:
            value = str(value)

        result[key] = value

    return result


@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    request_id = (
        request.headers.get("X-Request-ID") or str(uuid.uuid4())
    )
    start = time.monotonic()
    REQUEST_COUNTER.inc()
    level = "INFO"
    response = None

    try:
        response = await call_next(request)
        if response.status_code >= 500:
            level = "ERROR"
    except Exception:
        level = "ERROR"
        raise
    finally:
        entry = {
            "level": level,
            "ts": datetime.now(timezone.utc).isoformat(),
            "path": request.url.path,
            "request_id": request_id,
            "method": request.method,
            "duration_ms": round(
                (time.monotonic() - start) * 1000, 2
            )
        }

        REQUEST_LOGS.append(entry)
        print(json.dumps(entry), flush=True)

    if response is not None:
        response.headers["X-Request-ID"] = request_id

    return response


@app.get("/")
async def root():
    return {"status": "ok"}


@app.get("/effective-config")
async def effective_config(
    set_: list[str] = Query(default=[], alias="set")
):
    config = DEFAULTS.copy()

    env_name = os.getenv("APP_ENV", "development")
    yaml_file = BASE_DIR / f"config.{env_name}.yaml"

    if yaml_file.exists():
        with yaml_file.open(encoding="utf-8") as file:
            config.update(normalize(yaml.safe_load(file) or {}))

    config.update(
        normalize(dotenv_values(BASE_DIR / ".env"), env_layer=True)
    )

    os_env = {
        key: value
        for key, value in os.environ.items()
        if key.startswith("APP_") and key != "APP_ENV"
    }
    config.update(normalize(os_env))

    for override in set_:
        key, separator, value = override.partition("=")

        if not separator or not key.strip():
            continue

        key = key.strip().lower()

        if key in ("port", "workers"):
            value = int(value)
        elif key == "debug":
            value = value.strip().lower() in (
                "true", "1", "yes", "on"
            )
        else:
            value = str(value)

        config[key] = value

    config["api_key"] = "****"
    return config


@app.get("/work")
async def work(n: int = Query(default=1, ge=0)):
    for _ in range(n):
        pass

    return {
        "email": os.getenv(
            "USER_EMAIL", "25f2003120@ds.study.iitm.ac.in"
        ),
        "done": n
    }


@app.get("/metrics")
async def metrics():
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST
    )


@app.get("/healthz")
async def healthz():
    return {
        "status": "ok",
        "uptime_s": max(0.0, time.monotonic() - START_TIME)
    }


@app.get("/logs/tail")
async def logs_tail(
    limit: int = Query(default=100, ge=1, le=1000)
):
    return list(REQUEST_LOGS)[-limit:]


@app.post("/hit/{key}")
async def hit(key: str):
    try:
        count = redis_client.incr(f"hit:{key}")
        return {"key": key, "count": count}
    except redis.RedisError:
        raise HTTPException(
            status_code=503, detail="Redis unavailable"
        )


@app.get("/count/{key}")
async def count(key: str):
    try:
        value = redis_client.get(f"hit:{key}")
        return {
            "key": key,
            "count": int(value) if value else 0
        }
    except redis.RedisError:
        raise HTTPException(
            status_code=503, detail="Redis unavailable"
        )


@app.post("/analytics")
async def analytics(
    body: dict,
    x_api_key: str = Header(default=None, alias="X-API-Key")
):
    expected_key = os.getenv("ANALYTICS_API_KEY", "")

    if (
        not expected_key
        or not x_api_key
        or not hmac.compare_digest(x_api_key, expected_key)
    ):
        raise HTTPException(
            status_code=401, detail="Unauthorized"
        )

    events = body.get("events", [])
    user_totals = defaultdict(float)
    revenue = 0.0

    for event in events:
        user = event["user"]
        amount = float(event["amount"])

        if amount > 0:
            revenue += amount
            user_totals[user] += amount

    top_user = (
        max(user_totals, key=user_totals.get)
        if user_totals else None
    )

    return {
        "email": "25f2003120@ds.study.iitm.ac.in",
        "total_events": len(events),
        "unique_users": len({event["user"] for event in events}),
        "revenue": revenue,
        "top_user": top_user
    }
