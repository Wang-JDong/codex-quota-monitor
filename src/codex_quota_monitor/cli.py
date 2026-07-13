import argparse
from dataclasses import asdict
import json
import logging
import os
from pathlib import Path

from .feed import RssHubClient
from .feishu import FeishuClient
from .models import Notification
from .service import MonitorService
from .settings import load_settings
from .store import Store


DEFAULT_CONFIG = Path("/opt/codex-quota-monitor/config/sources.json")


def build(config: Path) -> tuple[MonitorService, Store, FeishuClient]:
    settings = load_settings(config)
    store = Store(settings.database_path, settings.failure_threshold)
    store.initialize()
    feed = RssHubClient(
        settings.rsshub_base_url,
        settings.request_timeout_seconds,
        settings.request_retries,
        settings.feed_count,
    )
    feishu = FeishuClient(
        settings.feishu_webhook_url,
        settings.feishu_signing_secret,
        settings.request_retries,
        settings.request_timeout_seconds,
    )
    return MonitorService(settings.sources, feed, store, feishu), store, feishu


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Monitor trusted Codex quota news")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="run one production check")
    commands.add_parser("dry-run", help="inspect feeds without changing state")
    commands.add_parser("status", help="show local monitor state")
    commands.add_parser("test-notification", help="send one Feishu test card")
    resolve = commands.add_parser(
        "delivery-resolve", help="manually resolve an uncertain delivery"
    )
    resolve.add_argument("post_id")
    resolve.add_argument(
        "--as", dest="resolution", choices=("sent", "retry"), required=True
    )
    health_resolve = commands.add_parser(
        "health-resolve", help="manually resolve an uncertain health delivery"
    )
    health_resolve.add_argument("transition", choices=("alert", "recovered"))
    health_resolve.add_argument(
        "--as", dest="resolution", choices=("sent", "retry"), required=True
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    service, store, feishu = build(args.config)

    if args.command == "run":
        with store.run_lock():
            result = service.run()
        print(json.dumps(asdict(result), ensure_ascii=False))
    elif args.command == "dry-run":
        with store.run_lock():
            result = service.run(dry_run=True)
        print(json.dumps(asdict(result), ensure_ascii=False))
    elif args.command == "status":
        print(json.dumps(store.status(), ensure_ascii=False, indent=2))
    elif args.command == "delivery-resolve":
        state = store.resolve_delivery(args.post_id, args.resolution)
        print(
            json.dumps(
                {"post_id": args.post_id, "delivery_state": state},
                ensure_ascii=False,
            )
        )
    elif args.command == "health-resolve":
        state = store.resolve_health_delivery(args.transition, args.resolution)
        print(
            json.dumps(
                {"transition": args.transition, "delivery_state": state},
                ensure_ascii=False,
            )
        )
    else:
        feishu.send(
            Notification(
                "Codex 额度监控｜系统测试",
                "飞书机器人签名和网络连接正常。",
                "blue",
            )
        )
        print("test notification sent")
