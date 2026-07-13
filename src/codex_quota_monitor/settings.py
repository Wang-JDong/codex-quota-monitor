from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Mapping

from .models import Source


EXPECTED_HANDLES = ("OpenAI", "OpenAIDevs", "thsottiaux", "sama")


@dataclass(frozen=True)
class Settings:
    sources: tuple[Source, ...]
    rsshub_base_url: str
    database_path: Path
    feishu_webhook_url: str
    feishu_signing_secret: str
    feed_count: int = 20
    failure_threshold: int = 3
    request_timeout_seconds: int = 20
    request_retries: int = 3


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ValueError(f"missing required environment variable: {name}")
    return value


def _bounded(raw: object, name: str, minimum: int, maximum: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer") from None
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def load_settings(
    config_path: Path, environ: Mapping[str, str] | None = None
) -> Settings:
    env = os.environ if environ is None else environ
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    sources = tuple(
        Source(str(item["handle"]), bool(item.get("include_replies", True)))
        for item in raw.get("sources", [])
    )
    handles = tuple(source.handle for source in sources)
    if handles != EXPECTED_HANDLES:
        raise ValueError(f"sources must be exactly {EXPECTED_HANDLES!r}, got {handles!r}")
    base_url = _required(env, "RSSHUB_BASE_URL").rstrip("/")
    webhook = _required(env, "FEISHU_WEBHOOK_URL")
    secret = _required(env, "FEISHU_SIGNING_SECRET")
    if not base_url.startswith(("http://", "https://")):
        raise ValueError("RSSHUB_BASE_URL must be http(s)")
    if not webhook.startswith("https://open.feishu.cn/"):
        raise ValueError("FEISHU_WEBHOOK_URL must use open.feishu.cn")
    return Settings(
        sources=sources,
        rsshub_base_url=base_url,
        database_path=Path(_required(env, "DATABASE_PATH")),
        feishu_webhook_url=webhook,
        feishu_signing_secret=secret,
        feed_count=_bounded(raw.get("feed_count", 20), "feed_count", 1, 100),
        failure_threshold=_bounded(
            raw.get("failure_threshold", 3), "failure_threshold", 1, 24
        ),
        request_timeout_seconds=_bounded(
            raw.get("request_timeout_seconds", 20),
            "request_timeout_seconds",
            1,
            120,
        ),
        request_retries=_bounded(
            raw.get("request_retries", 3), "request_retries", 1, 5
        ),
    )
