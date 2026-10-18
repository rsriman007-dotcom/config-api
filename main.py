import hmac
from collections import defaultdict
from fastapi import Header

import redis
from fastapi import HTTPException

import os
from pathlib import Path

import yaml
from dotenv import dotenv_values
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

redis_client = redis.Redis.from_url(
    REDIS_URL,
    decode_responses=True
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = Path(__file__).resolve().parent

DEFAULTS = {
    "port": 8000,
    "workers": 1,
    "debug": False,
    "log_level": "info",
    "api_key": "default-secret-000",
}


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


@app.get("/")
async def health():
    return {"status": "ok"}

@app.get("/healthz")
async def healthz():
    try:
        redis_client.ping()
        return {"status": "ok", "redis": "up"}
    except redis.RedisError:
        raise HTTPException(
            status_code=503,
            detail={"status": "error", "redis": "down"}
        )


@app.post("/hit/{key}")
async def hit(key: str):
    try:
        count = redis_client.incr(f"hit:{key}")
        return {"key": key, "count": count}
    except redis.RedisError:
        raise HTTPException(status_code=503, detail="Redis unavailable")


@app.get("/count/{key}")
async def count(key: str):
    try:
        value = redis_client.get(f"hit:{key}")
        return {"key": key, "count": int(value) if value else 0}
    except redis.RedisError:
        raise HTTPException(status_code=503, detail="Redis unavailable")
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
        raise HTTPException(status_code=401, detail="Unauthorized")

    events = body.get("events", [])
    user_totals = defaultdict(float)
    revenue = 0.0

    for event in events:
        user = event["user"]
        amount = float(event["amount"])

        if amount > 0:
            revenue += amount
            user_totals[user] += amount

    top_user = max(user_totals, key=user_totals.get) if user_totals else None

    return {
        "email": os.getenv("USER_EMAIL", ""),
        "total_events": len(events),
        "unique_users": len({event["user"] for event in events}),
        "revenue": revenue,
        "top_user": top_user
    }