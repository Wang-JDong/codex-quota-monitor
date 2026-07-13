import json
import subprocess
import sys
from contextlib import nullcontext
from dataclasses import dataclass

from codex_quota_monitor import cli


def test_module_help() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "codex_quota_monitor", "--help"],
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0
    assert "test-notification" in result.stdout
    assert "dry-run" in result.stdout


@dataclass
class Summary:
    fetched_sources: int = 1
    new_posts: int = 2
    matched_posts: int = 1
    sent_posts: int = 1


class FakeService:
    def __init__(self) -> None:
        self.dry_runs = []

    def run(self, dry_run=False):
        self.dry_runs.append(dry_run)
        return Summary()

    def reprocess(self, post_id):
        self.reprocessed = post_id
        return {
            "post_id": post_id,
            "matched": True,
            "changed": True,
            "sent": True,
        }


class FakeStore:
    def __init__(self) -> None:
        self.locked = 0

    def run_lock(self):
        self.locked += 1
        return nullcontext()

    def status(self):
        return {"pending": 0, "alert_active": False, "sources": []}

    def resolve_delivery(self, post_id, resolution):
        self.resolved = (post_id, resolution)
        return "pending" if resolution == "retry" else "sent"

    def resolve_health_delivery(self, transition, resolution):
        self.health_resolved = (transition, resolution)
        return "pending" if resolution == "retry" else "sent"


class FakeFeishu:
    def __init__(self) -> None:
        self.sent = []

    def send(self, note):
        self.sent.append(note)


def run_cli(monkeypatch, capsys, command, *extra):
    service, store, feishu = FakeService(), FakeStore(), FakeFeishu()
    monkeypatch.setattr(cli, "build", lambda _config: (service, store, feishu))
    monkeypatch.setattr(sys, "argv", ["codex-quota-monitor", command, *extra])

    cli.main()

    return service, store, feishu, capsys.readouterr()


def test_run_uses_lock_and_prints_json(monkeypatch, capsys) -> None:
    service, store, _feishu, captured = run_cli(monkeypatch, capsys, "run")

    assert service.dry_runs == [False]
    assert store.locked == 1
    assert json.loads(captured.out)["sent_posts"] == 1


def test_dry_run_uses_read_only_service_mode(monkeypatch, capsys) -> None:
    service, store, feishu, captured = run_cli(monkeypatch, capsys, "dry-run")

    assert service.dry_runs == [True]
    assert store.locked == 1
    assert feishu.sent == []
    assert json.loads(captured.out)["fetched_sources"] == 1


def test_status_does_not_send_notification(monkeypatch, capsys) -> None:
    service, store, feishu, captured = run_cli(monkeypatch, capsys, "status")

    assert service.dry_runs == []
    assert store.locked == 0
    assert feishu.sent == []
    assert json.loads(captured.out) == {
        "pending": 0,
        "alert_active": False,
        "sources": [],
    }


def test_test_notification_sends_only_a_system_test(monkeypatch, capsys) -> None:
    service, store, feishu, captured = run_cli(
        monkeypatch, capsys, "test-notification"
    )

    assert service.dry_runs == []
    assert store.locked == 0
    assert len(feishu.sent) == 1
    assert feishu.sent[0].title == "Codex 额度监控｜系统测试"
    assert feishu.sent[0].body == "飞书机器人签名和网络连接正常。"
    assert captured.out.strip() == "test notification sent"


def test_delivery_resolve_requires_explicit_resolution(monkeypatch, capsys) -> None:
    service, store, feishu, captured = run_cli(
        monkeypatch,
        capsys,
        "delivery-resolve",
        "post-123",
        "--as",
        "retry",
    )

    assert service.dry_runs == []
    assert feishu.sent == []
    assert store.resolved == ("post-123", "retry")
    assert json.loads(captured.out) == {
        "post_id": "post-123",
        "delivery_state": "pending",
    }


def test_health_resolve_requires_transition_and_explicit_resolution(
    monkeypatch, capsys
) -> None:
    service, store, feishu, captured = run_cli(
        monkeypatch,
        capsys,
        "health-resolve",
        "recovered",
        "--as",
        "sent",
    )

    assert service.dry_runs == []
    assert feishu.sent == []
    assert store.health_resolved == ("recovered", "sent")
    assert json.loads(captured.out) == {
        "transition": "recovered",
        "delivery_state": "sent",
    }


def test_reprocess_post_uses_lock_and_prints_json(monkeypatch, capsys) -> None:
    service, store, feishu, captured = run_cli(
        monkeypatch, capsys, "reprocess-post", "2076735790567338203"
    )

    assert service.reprocessed == "2076735790567338203"
    assert store.locked == 1
    assert feishu.sent == []
    assert json.loads(captured.out) == {
        "post_id": "2076735790567338203",
        "matched": True,
        "changed": True,
        "sent": True,
    }
