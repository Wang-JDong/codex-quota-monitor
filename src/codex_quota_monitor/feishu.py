import base64
import hashlib
import hmac
from http.client import HTTPException, InvalidURL
import json
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .models import Decision, HealthTransition, Notification, Post, ResetStatus


LABELS = {
    ResetStatus.COMPLETED: "已经重置",
    ResetStatus.IN_PROGRESS: "正在重置",
    ResetStatus.PLANNED: "计划重置",
    ResetStatus.BANKED_AVAILABLE: "可保存重置次数已发放",
}


class FeishuError(RuntimeError):
    """A delivery failure whose message is safe to surface in logs."""

    def __init__(
        self,
        message: str,
        *,
        outcome_unknown: bool,
        retryable: bool,
    ) -> None:
        super().__init__(message)
        self.outcome_unknown = outcome_unknown
        self.retryable = retryable


def notification_for_post(post: Post, decision: Decision) -> Notification:
    if decision.status is None:
        raise ValueError("matched decision requires status")
    label = LABELS[decision.status]
    beijing_time = post.published_at.astimezone(ZoneInfo("Asia/Shanghai")).strftime(
        "%Y-%m-%d %H:%M"
    )
    banked_note = (
        "\n**说明：** 这是可手动使用的重置次数，不会自动表示当前额度已恢复。\n"
        if decision.status is ResetStatus.BANKED_AVAILABLE
        else ""
    )
    body = (
        f"**状态：** {label}\n"
        f"{banked_note}"
        f"**来源：** @{post.author}\n"
        f"**时间：** {beijing_time}（北京时间）\n\n"
        f"**官方原文：**\n{post.text[:1500]}"
    )
    title = (
        f"Codex 额度通知｜{label}"
        if decision.status is ResetStatus.BANKED_AVAILABLE
        else f"Codex 额度重置通知｜{label}"
    )
    return Notification(title, body, "green", post.url)


def health_notification(transition: HealthTransition) -> Notification:
    if transition is HealthTransition.ALERT:
        return Notification(
            "Codex 额度监控异常",
            "四个可信来源已连续三轮无法抓取，或 X 登录会话已失效。"
            "请检查日志并更新 Cookie。",
            "red",
        )
    if transition is HealthTransition.RECOVERED:
        return Notification(
            "Codex 额度监控已恢复", "四个可信来源均已恢复正常抓取。", "blue"
        )
    raise ValueError("NONE has no notification")


class FeishuClient:
    def __init__(
        self, webhook_url: str, secret: str, retries: int, timeout: int
    ) -> None:
        if retries <= 0 or timeout <= 0:
            raise ValueError("retries and timeout must be positive")
        self.webhook_url = webhook_url
        self.secret = secret
        self.retries = retries
        self.timeout = timeout

    def _signature(self, timestamp: str) -> str:
        signing_key = f"{timestamp}\n{self.secret}".encode()
        digest = hmac.new(signing_key, digestmod=hashlib.sha256).digest()
        return base64.b64encode(digest).decode()

    def _payload(self, notification: Notification) -> dict[str, object]:
        timestamp = str(int(time.time()))
        elements: list[dict[str, object]] = [
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": notification.body},
            }
        ]
        if notification.url:
            elements.append(
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {
                                "tag": "plain_text",
                                "content": "查看 X 原帖",
                            },
                            "url": notification.url,
                            "type": "primary",
                        }
                    ],
                }
            )
        return {
            "timestamp": timestamp,
            "sign": self._signature(timestamp),
            "msg_type": "interactive",
            "card": {
                "header": {
                    "template": notification.template,
                    "title": {"tag": "plain_text", "content": notification.title},
                },
                "elements": elements,
            },
        }

    def send(self, notification: Notification) -> None:
        payload = self._payload(notification)
        try:
            request = Request(
                self.webhook_url,
                data=json.dumps(payload, ensure_ascii=False).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        except (HTTPException, TypeError, ValueError):
            raise FeishuError(
                "Feishu send failed: invalid webhook request",
                outcome_unknown=False,
                retryable=False,
            ) from None

        for attempt in range(1, self.retries + 1):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    raw_result = response.read()
            except HTTPError as exc:
                if exc.code == 429:
                    if attempt == self.retries:
                        raise FeishuError(
                            "Feishu send failed: rate limited",
                            outcome_unknown=False,
                            retryable=True,
                        ) from None
                    time.sleep(2 ** (attempt - 1))
                    continue
                if 500 <= exc.code < 600:
                    raise FeishuError(
                        "Feishu send failed: delivery outcome unknown",
                        outcome_unknown=True,
                        retryable=False,
                    ) from None
                raise FeishuError(
                    f"Feishu send failed: permanent HTTP {self._safe(exc.code)}",
                    outcome_unknown=False,
                    retryable=False,
                ) from None
            except InvalidURL:
                raise FeishuError(
                    "Feishu send failed: invalid webhook request",
                    outcome_unknown=False,
                    retryable=False,
                ) from None
            except (TypeError, ValueError):
                raise FeishuError(
                    "Feishu send failed: invalid webhook request",
                    outcome_unknown=False,
                    retryable=False,
                ) from None
            except URLError as exc:
                if isinstance(exc.reason, ssl.SSLCertVerificationError):
                    raise FeishuError(
                        "Feishu send failed: TLS certificate verification failed",
                        outcome_unknown=False,
                        retryable=False,
                    ) from None
                if isinstance(exc.reason, (InvalidURL, TypeError, ValueError)):
                    raise FeishuError(
                        "Feishu send failed: invalid webhook request",
                        outcome_unknown=False,
                        retryable=False,
                    ) from None
                raise FeishuError(
                    "Feishu send failed: delivery outcome unknown",
                    outcome_unknown=True,
                    retryable=False,
                ) from None
            except (HTTPException, OSError, TimeoutError):
                raise FeishuError(
                    "Feishu send failed: delivery outcome unknown",
                    outcome_unknown=True,
                    retryable=False,
                ) from None
            else:
                self._validate_response(raw_result)
                return

    def _safe(self, value: object) -> str:
        text = str(value)
        text = text.replace(self.webhook_url, "[redacted]")
        if self.secret:
            text = text.replace(self.secret, "[redacted]")
        return text[:200]

    def _validate_response(self, raw_result: bytes) -> None:
        try:
            result = json.loads(raw_result)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise FeishuError(
                "Feishu returned an invalid response",
                outcome_unknown=True,
                retryable=False,
            ) from None
        if not isinstance(result, dict) or "code" not in result:
            raise FeishuError(
                "Feishu returned an invalid response",
                outcome_unknown=True,
                retryable=False,
            )
        if result["code"] != 0:
            raise FeishuError(
                "Feishu rejected request",
                outcome_unknown=False,
                retryable=False,
            )
