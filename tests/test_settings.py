from pathlib import Path
import json

import pytest

from codex_quota_monitor.settings import load_settings


def test_load_settings_requires_exact_allowlist(tmp_path: Path) -> None:
    config = tmp_path / "sources.json"
    config.write_text(
        '{"sources": ['
        '{"handle": "OpenAI"}, {"handle": "OpenAIDevs"}, '
        '{"handle": "thsottiaux"}, {"handle": "sama"}]}',
        encoding="utf-8",
    )
    env = {
        "RSSHUB_BASE_URL": "http://rsshub:1200",
        "DATABASE_PATH": "/data/monitor.db",
        "FEISHU_WEBHOOK_URL": "https://open.feishu.cn/open-apis/bot/v2/hook/example",
        "FEISHU_SIGNING_SECRET": "secret",
    }

    settings = load_settings(config, env)

    assert [source.handle for source in settings.sources] == [
        "OpenAI",
        "OpenAIDevs",
        "thsottiaux",
        "sama",
    ]
    assert settings.failure_threshold == 3
    assert settings.feed_count == 20


def test_load_settings_rejects_missing_secret(tmp_path: Path) -> None:
    config = tmp_path / "sources.json"
    config.write_text(
        '{"sources": [{"handle": "OpenAI"}, {"handle": "OpenAIDevs"}, '
        '{"handle": "thsottiaux"}, {"handle": "sama"}]}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="FEISHU_SIGNING_SECRET"):
        load_settings(
            config,
            {
                "RSSHUB_BASE_URL": "http://127.0.0.1:1200",
                "DATABASE_PATH": "/data/monitor.db",
                "FEISHU_WEBHOOK_URL": "https://open.feishu.cn/open-apis/bot/v2/hook/example",
            },
        )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("feed_count", 0),
        ("feed_count", 101),
        ("failure_threshold", 0),
        ("failure_threshold", 25),
        ("request_timeout_seconds", 0),
        ("request_timeout_seconds", 121),
        ("request_retries", 0),
        ("request_retries", 6),
    ],
)
def test_load_settings_rejects_resource_heavy_numeric_limits(
    tmp_path: Path, name: str, value: int
) -> None:
    config = tmp_path / "sources.json"
    config.write_text(
        json.dumps(
            {
                "sources": [
                    {"handle": "OpenAI"},
                    {"handle": "OpenAIDevs"},
                    {"handle": "thsottiaux"},
                    {"handle": "sama"},
                ],
                name: value,
            }
        ),
        encoding="utf-8",
    )
    env = {
        "RSSHUB_BASE_URL": "http://127.0.0.1:1200",
        "DATABASE_PATH": "/data/monitor.db",
        "FEISHU_WEBHOOK_URL": "https://open.feishu.cn/open-apis/bot/v2/hook/example",
        "FEISHU_SIGNING_SECRET": "secret",
    }

    with pytest.raises(ValueError, match=name):
        load_settings(config, env)
