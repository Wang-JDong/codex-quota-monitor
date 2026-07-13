from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def test_operations_docs_cover_complete_operator_flow() -> None:
    operations = "\n".join(
        (ROOT / path).read_text(encoding="utf-8")
        for path in (
            "README.md",
            "docs/operations/DEPLOYMENT.md",
            "docs/operations/RUNBOOK.md",
            "docs/architecture/ARCHITECTURE.md",
        )
    )

    required = (
        "/opt/codex-quota-monitor",
        "@OpenAI",
        "@OpenAIDevs",
        "@thsottiaux",
        "@sama",
        "make preflight",
        "make install",
        "make dry-run",
        "make test-notification",
        "make run",
        "make postflight",
        "make enable",
        "make status",
        "make resource-check",
        "make rollback",
        "TWITTER_AUTH_TOKEN",
        "FEISHU_WEBHOOK_URL",
        "FEISHU_SIGNING_SECRET",
        "MemoryMax=402653184",
        "CPUQuota=30%",
        "CPUQuotaPerSecUSec=300ms",
        "codex-quota-monitor-dry-run",
        "http://127.0.0.1:1201",
        "/tmp/codex-quota-monitor-dry-run.db",
        "PrivateTmp",
        "校验失败时返回非零状态",
        "首次基线",
        "连续三轮",
        "SQLite",
        "make enable` 会先强制执行 `make postflight` 和 `make resource-check`",
    )
    missing = [item for item in required if item not in operations]
    assert not missing, f"分层运维文档缺少要点: {missing}"


def test_deployment_records_every_protected_service_and_port() -> None:
    deployment = (ROOT / "docs/operations/DEPLOYMENT.md").read_text(encoding="utf-8")

    for service in (
        "ssh.service",
        "sing-box.service",
        "cdn-subscription.service",
        "friend-clash-sub.service",
        "share-100gb-sub.service",
    ):
        assert service in deployment
    for port in ("22", "22222", "2082", "2086", "2095", "2052", "8880"):
        assert re.search(rf"(?<!\d){port}(?!\d)", deployment)


def test_readme_does_not_offer_high_risk_install_commands() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    shell_commands = [
        line.strip()
        for line in readme.splitlines()
        if line.lstrip().startswith(("sudo ", "apt ", "docker ", "pip ", "ufw "))
    ]

    forbidden = ("apt ", "apt-get ", "docker ", "pip install", "-m venv", "ufw ")
    assert not [line for line in shell_commands if any(item in line for item in forbidden)]


def test_env_example_uses_placeholders_and_explains_secret_sources() -> None:
    example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "auth_token Cookie" in example
    assert "飞书自定义机器人" in example
    assert "TWITTER_AUTH_TOKEN=replace-" in example
    assert "FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/replace-" in example
    assert "FEISHU_SIGNING_SECRET=replace-" in example


def test_runbook_documents_manual_delivery_reconciliation() -> None:
    runbook = (ROOT / "docs/operations/RUNBOOK.md").read_text(encoding="utf-8")

    assert "uncertain" in runbook
    assert "不自动重发" in runbook
    assert "delivery-resolve" in runbook
    assert "--as sent" in runbook
    assert "--as retry" in runbook
    assert "在飞书群" in runbook
    assert "不要直接编辑 SQLite" in runbook
