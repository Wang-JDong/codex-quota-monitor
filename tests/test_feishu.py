from datetime import UTC, datetime
from http.client import IncompleteRead, RemoteDisconnected
import json
import ssl
from urllib.error import HTTPError, URLError
from unittest.mock import patch

import pytest

from codex_quota_monitor.feishu import (
    FeishuClient,
    FeishuError,
    health_notification,
    notification_for_post,
)
from codex_quota_monitor.models import (
    Decision,
    HealthTransition,
    Notification,
    Post,
    ResetStatus,
)


class Response:
    def __init__(
        self,
        body: bytes = b'{"code": 0, "msg": "success"}',
        read_error: Exception | None = None,
    ) -> None:
        self.body = body
        self.read_error = read_error

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        if self.read_error is not None:
            raise self.read_error
        return self.body


def sample_post() -> Post:
    return Post(
        "1",
        "sama",
        "We will reset Codex usage limits.",
        datetime(2026, 7, 13, 4, 30, tzinfo=UTC),
        "https://x.com/sama/status/1",
    )


def test_sends_signed_interactive_card() -> None:
    client = FeishuClient(
        "https://open.feishu.cn/open-apis/bot/v2/hook/example",
        "secret",
        retries=1,
        timeout=10,
    )
    note = notification_for_post(
        sample_post(), Decision(True, ResetStatus.PLANNED, "explicit")
    )

    with (
        patch("codex_quota_monitor.feishu.time.time", return_value=1720872000),
        patch(
            "codex_quota_monitor.feishu.urlopen", return_value=Response()
        ) as mocked,
    ):
        client.send(note)

    request = mocked.call_args.args[0]
    payload = json.loads(request.data)
    assert mocked.call_args.kwargs["timeout"] == 10
    assert request.full_url.endswith("/hook/example")
    assert request.get_method() == "POST"
    assert request.headers["Content-type"] == "application/json"
    assert payload["msg_type"] == "interactive"
    assert payload["timestamp"] == "1720872000"
    assert payload["sign"] == "HhIwBjlTqLCi/5CSTkVWR1tdlWQSSCEkxSAxZm4Wf2E="
    assert "计划重置" in payload["card"]["header"]["title"]["content"]
    assert "@sama" in payload["card"]["elements"][0]["text"]["content"]
    assert payload["card"]["elements"][1]["actions"][0]["url"] == sample_post().url


@pytest.mark.parametrize(
    ("status", "label"),
    [
        (ResetStatus.COMPLETED, "已经重置"),
        (ResetStatus.IN_PROGRESS, "正在重置"),
        (ResetStatus.PLANNED, "计划重置"),
    ],
)
def test_post_notification_identifies_status_source_time_and_original_link(
    status: ResetStatus, label: str
) -> None:
    note = notification_for_post(sample_post(), Decision(True, status, "explicit"))

    assert note.title == f"Codex 额度重置通知｜{label}"
    assert f"**状态：** {label}" in note.body
    assert "**来源：** @sama" in note.body
    assert "2026-07-13 12:30（北京时间）" in note.body
    assert "We will reset Codex usage limits." in note.body
    assert note.template == "green"
    assert note.url == "https://x.com/sama/status/1"


def test_post_notification_rejects_decision_without_status() -> None:
    with pytest.raises(ValueError, match="requires status"):
        notification_for_post(sample_post(), Decision(False, None, "no match"))


def test_health_notifications_are_clear_and_have_no_link() -> None:
    alert = health_notification(HealthTransition.ALERT)
    recovered = health_notification(HealthTransition.RECOVERED)

    assert alert.title == "Codex 额度监控异常"
    assert "连续三轮" in alert.body
    assert "Cookie" in alert.body
    assert alert.template == "red"
    assert alert.url == ""
    assert recovered.title == "Codex 额度监控已恢复"
    assert "恢复正常抓取" in recovered.body
    assert recovered.template == "blue"
    assert recovered.url == ""


def test_health_notification_rejects_none_transition() -> None:
    with pytest.raises(ValueError, match="NONE"):
        health_notification(HealthTransition.NONE)


def test_retries_only_http_429_then_succeeds() -> None:
    client = FeishuClient("https://example.invalid/hook/private", "secret", 2, 10)
    note = Notification("title", "body", "blue")

    with (
        patch(
            "codex_quota_monitor.feishu.urlopen",
            side_effect=[HTTPError("redacted", 429, "rate limited", {}, None), Response()],
        ) as mocked,
        patch("codex_quota_monitor.feishu.time.sleep") as sleep,
    ):
        client.send(note)

    assert mocked.call_count == 2
    sleep.assert_called_once_with(1)


def test_exhausted_429_is_explicit_retryable_failure() -> None:
    client = FeishuClient("https://example.invalid/hook/private", "secret", 2, 10)
    error = HTTPError("redacted", 429, "rate limited", {}, None)
    with (
        patch("codex_quota_monitor.feishu.urlopen", side_effect=[error, error]) as mocked,
        patch("codex_quota_monitor.feishu.time.sleep") as sleep,
        pytest.raises(FeishuError) as raised,
    ):
        client.send(Notification("title", "body", "blue"))

    assert mocked.call_count == 2
    sleep.assert_called_once_with(1)
    assert raised.value.retryable is True
    assert raised.value.outcome_unknown is False


@pytest.mark.parametrize(
    "failure",
    [
        URLError("temporary DNS error"),
        TimeoutError("timed out"),
        HTTPError("redacted", 503, "unavailable", {}, None),
    ],
)
def test_network_and_5xx_failures_are_unknown_and_never_retried(failure) -> None:
    client = FeishuClient("https://example.invalid/hook/private", "secret", 3, 10)
    with (
        patch("codex_quota_monitor.feishu.urlopen", side_effect=failure) as mocked,
        patch("codex_quota_monitor.feishu.time.sleep") as sleep,
        pytest.raises(FeishuError) as raised,
    ):
        client.send(Notification("title", "body", "blue"))

    assert mocked.call_count == 1
    sleep.assert_not_called()
    assert raised.value.outcome_unknown is True
    assert raised.value.retryable is False


def test_certificate_verification_failure_is_permanent_and_safe() -> None:
    webhook = "https://example.invalid/hook/sensitive-webhook"
    secret = "sensitive-secret"
    client = FeishuClient(webhook, secret, 3, 10)
    reason = ssl.SSLCertVerificationError(
        1, f"certificate verify failed for {webhook} with {secret}"
    )

    with (
        patch(
            "codex_quota_monitor.feishu.urlopen", side_effect=URLError(reason)
        ) as mocked,
        patch("codex_quota_monitor.feishu.time.sleep") as sleep,
        pytest.raises(FeishuError, match="certificate verification") as raised,
    ):
        client.send(Notification("title", "body", "blue"))

    assert mocked.call_count == 1
    sleep.assert_not_called()
    assert webhook not in str(raised.value)
    assert secret not in str(raised.value)
    assert raised.value.outcome_unknown is False
    assert raised.value.retryable is False


def test_direct_certificate_failure_is_permanent() -> None:
    client = FeishuClient("https://example.invalid/hook/private", "secret", 3, 10)
    reason = ssl.SSLCertVerificationError(1, "certificate verify failed")

    with (
        patch("codex_quota_monitor.feishu.urlopen", side_effect=reason),
        pytest.raises(FeishuError) as raised,
    ):
        client.send(Notification("title", "body", "blue"))

    assert raised.value.outcome_unknown is False
    assert raised.value.retryable is False


def test_does_not_retry_permanent_http_error_or_expose_credentials() -> None:
    webhook = "https://example.invalid/hook/sensitive-webhook"
    secret = "sensitive-secret"
    client = FeishuClient(webhook, secret, 3, 10)

    with (
        patch(
            "codex_quota_monitor.feishu.urlopen",
            side_effect=HTTPError(webhook, 400, "bad request", {}, None),
        ) as mocked,
        patch("codex_quota_monitor.feishu.time.sleep") as sleep,
        pytest.raises(FeishuError) as raised,
    ):
        client.send(Notification("title", "body", "blue"))

    assert mocked.call_count == 1
    sleep.assert_not_called()
    message = str(raised.value)
    assert "HTTP 400" in message
    assert webhook not in message
    assert secret not in message
    assert raised.value.outcome_unknown is False
    assert raised.value.retryable is False


def test_rejects_unsuccessful_feishu_json_without_retrying() -> None:
    client = FeishuClient("https://example.invalid/hook/private", "secret", 3, 10)
    response = Response(b'{"code": 19021, "msg": "sign match fail"}')

    with (
        patch("codex_quota_monitor.feishu.urlopen", return_value=response) as mocked,
        patch("codex_quota_monitor.feishu.time.sleep") as sleep,
        pytest.raises(FeishuError, match="rejected request") as raised,
    ):
        client.send(Notification("title", "body", "blue"))

    assert mocked.call_count == 1
    sleep.assert_not_called()
    assert raised.value.outcome_unknown is False
    assert raised.value.retryable is False


def test_feishu_rejection_does_not_expose_credentials() -> None:
    webhook = "https://example.invalid/hook/sensitive-webhook"
    secret = "sensitive-secret"
    client = FeishuClient(webhook, secret, 1, 10)
    response = Response(
        json.dumps(
            {"code": 19021, "msg": f"rejected {webhook} using {secret}"}
        ).encode()
    )

    with (
        patch("codex_quota_monitor.feishu.urlopen", return_value=response),
        pytest.raises(RuntimeError) as raised,
    ):
        client.send(Notification("title", "body", "blue"))

    assert webhook not in str(raised.value)
    assert secret not in str(raised.value)


def test_feishu_rejection_code_does_not_expose_credentials() -> None:
    webhook = "https://example.invalid/hook/sensitive-webhook"
    secret = "sensitive-secret"
    client = FeishuClient(webhook, secret, 1, 10)
    response = Response(
        json.dumps({"code": f"{secret} at {webhook}", "msg": "no"}).encode()
    )

    with (
        patch("codex_quota_monitor.feishu.urlopen", return_value=response),
        pytest.raises(FeishuError) as raised,
    ):
        client.send(Notification("title", "body", "blue"))

    assert webhook not in str(raised.value)
    assert secret not in str(raised.value)


@pytest.mark.parametrize(
    "read_error",
    [
        ConnectionResetError("reset while reading"),
        ConnectionError("connection failed while reading"),
        OSError("socket failed while reading"),
        RemoteDisconnected("peer disconnected while reading"),
        IncompleteRead(b"partial"),
    ],
)
def test_read_failures_are_unknown_and_never_retried(
    read_error: Exception,
) -> None:
    client = FeishuClient("https://example.invalid/hook/private", "secret", 2, 10)

    with (
        patch(
            "codex_quota_monitor.feishu.urlopen",
            return_value=Response(read_error=read_error),
        ) as mocked,
        patch("codex_quota_monitor.feishu.time.sleep") as sleep,
        pytest.raises(FeishuError) as raised,
    ):
        client.send(Notification("title", "body", "blue"))

    assert mocked.call_count == 1
    sleep.assert_not_called()
    assert raised.value.outcome_unknown is True


def test_read_failure_is_safe_feishu_error() -> None:
    webhook = "https://example.invalid/hook/sensitive-webhook"
    secret = "sensitive-secret"
    client = FeishuClient(webhook, secret, 2, 10)
    error = ConnectionResetError(f"failed for {webhook} with {secret}")

    with (
        patch(
            "codex_quota_monitor.feishu.urlopen",
            return_value=Response(read_error=error),
        ) as mocked,
        patch("codex_quota_monitor.feishu.time.sleep") as sleep,
        pytest.raises(FeishuError) as raised,
    ):
        client.send(Notification("title", "body", "blue"))

    assert mocked.call_count == 1
    sleep.assert_not_called()
    assert webhook not in str(raised.value)
    assert secret not in str(raised.value)


def test_invalid_webhook_is_permanent_safe_feishu_error() -> None:
    webhook = "https://["
    secret = "sensitive-secret"
    client = FeishuClient(webhook, secret, 3, 10)

    with (
        patch("codex_quota_monitor.feishu.urlopen") as mocked,
        patch("codex_quota_monitor.feishu.time.sleep") as sleep,
        pytest.raises(FeishuError, match="invalid webhook") as raised,
    ):
        client.send(Notification("title", "body", "blue"))

    mocked.assert_not_called()
    sleep.assert_not_called()
    assert webhook not in str(raised.value)
    assert secret not in str(raised.value)


@pytest.mark.parametrize("body", [b'{}', b'not-json'])
def test_invalid_success_response_is_unknown(body: bytes) -> None:
    client = FeishuClient("https://example.invalid/hook/private", "secret", 1, 10)

    with (
        patch("codex_quota_monitor.feishu.urlopen", return_value=Response(body)),
        pytest.raises(FeishuError, match="invalid response") as raised,
    ):
        client.send(Notification("title", "body", "blue"))

    assert raised.value.outcome_unknown is True
    assert raised.value.retryable is False


@pytest.mark.parametrize(("retries", "timeout"), [(0, 10), (1, 0)])
def test_client_requires_positive_limits(retries: int, timeout: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        FeishuClient("https://example.invalid/hook/private", "secret", retries, timeout)
