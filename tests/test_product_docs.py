from pathlib import Path
import ipaddress
import re
import subprocess
import tomllib


ROOT = Path(__file__).resolve().parents[1]

PRODUCT_FILES = (
    "README.md",
    "CONTRIBUTING.md",
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    "docs/product/PRD.md",
    "docs/architecture/ARCHITECTURE.md",
    "docs/operations/DEPLOYMENT.md",
    "docs/operations/RUNBOOK.md",
    "docs/security/SECURITY.md",
    "docs/testing/TESTING.md",
    "docs/decisions/ADR-001-free-x-ingestion.md",
    "docs/decisions/ADR-002-at-most-once-delivery.md",
    ".github/workflows/ci.yml",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
    ".github/pull_request_template.md",
    ".github/dependabot.yml",
)


def _text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_product_documentation_has_a_clear_public_hierarchy() -> None:
    missing = [path for path in PRODUCT_FILES if not (ROOT / path).is_file()]
    assert not missing, f"缺少产品化文件: {missing}"

    readme = _text("README.md")
    for heading in (
        "# Codex Quota Monitor",
        "## 为什么需要它",
        "## 核心价值",
        "## 功能与边界",
        "## 快速理解架构",
        "## 文档导航",
        "## 快速开始",
        "## 安全与项目状态",
    ):
        assert heading in readme
    assert "```mermaid" in readme

    navigation = (
        "docs/product/PRD.md",
        "docs/architecture/ARCHITECTURE.md",
        "docs/operations/DEPLOYMENT.md",
        "docs/operations/RUNBOOK.md",
        "docs/security/SECURITY.md",
        "docs/testing/TESTING.md",
        "CONTRIBUTING.md",
        "CHANGELOG.md",
    )
    assert all(f"]({path})" in readme for path in navigation)


def test_product_and_operator_docs_disclose_top_level_only_x_adapter() -> None:
    for path in (
        "README.md",
        "docs/product/PRD.md",
        "docs/architecture/ARCHITECTURE.md",
        "docs/decisions/ADR-001-free-x-ingestion.md",
        "docs/operations/RUNBOOK.md",
    ):
        contents = _text(path)
        assert "UserTweets" in contents, path
        assert "UserTweetsAndReplies" in contents, path
        assert "404" in contents, path
        assert "顶层原创帖" in contents, path
        assert "回复不作为证据" in contents, path
        assert "测试保护" in contents, path


def test_product_docs_capture_real_requirements_and_boundaries() -> None:
    prd = _text("docs/product/PRD.md")
    for heading in (
        "## 问题",
        "## 目标用户",
        "## Jobs to be Done",
        "## 功能需求",
        "## 非功能需求",
        "## 验收标准",
        "## 风险与边界",
        "## 路线图",
    ):
        assert heading in prd

    architecture = _text("docs/architecture/ARCHITECTURE.md")
    assert "```mermaid" in architecture
    for item in (
        "1200",
        "1201",
        "384 MiB",
        "30%",
        "pending",
        "in_flight",
        "uncertain",
        "permanent_failed",
        "delivery-resolve",
    ):
        assert item in architecture

    all_core_docs = "\n".join(
        _text(path)
        for path in (
            "README.md",
            "docs/product/PRD.md",
            "docs/architecture/ARCHITECTURE.md",
            "docs/operations/RUNBOOK.md",
            "docs/security/SECURITY.md",
        )
    )
    for source in ("@OpenAI", "@OpenAIDevs", "@thsottiaux", "@sama"):
        assert source in all_core_docs
    for phrase in (
        "首次基线静默",
        "转发",
        "引用",
        "429",
        "uncertain",
        "不自动重发",
    ):
        assert phrase in all_core_docs

    for path in (
        "docs/product/PRD.md",
        "docs/architecture/ARCHITECTURE.md",
        "docs/operations/RUNBOOK.md",
        "docs/decisions/ADR-002-at-most-once-delivery.md",
    ):
        document = _text(path)
        assert "health-resolve" in document
        assert "uncertain" in document


def test_docs_never_use_quotes_or_retweets_as_classification_evidence() -> None:
    boundary = (
        "引用元数据（包括 `quoted_text`）仅用于审计和展示上下文，"
        "永不作为分类匹配证据；转发也不作为证据。"
    )
    documents = (
        "README.md",
        "docs/product/PRD.md",
        "docs/architecture/ARCHITECTURE.md",
        "docs/security/SECURITY.md",
        "docs/superpowers/specs/2026-07-13-codex-quota-reset-monitor-design.md",
    )
    combined = "\n".join(_text(path) for path in documents)
    for path in documents:
        assert boundary in _text(path), f"{path} 未明确非证据边界"

    contradictory = (
        r"引用(?:内容|作者|正文).{0,30}(?:辅助判定|可作为|强证据|时可用)",
        r"正文与可信引用正文合并",
    )
    assert not [pattern for pattern in contradictory if re.search(pattern, combined)]


def test_operations_docs_match_supported_commands_and_protection_model() -> None:
    deployment = _text("docs/operations/DEPLOYMENT.md")
    runbook = _text("docs/operations/RUNBOOK.md")
    operations = f"{deployment}\n{runbook}"

    for command in (
        "make preflight",
        "make install",
        "make dry-run",
        "make test-notification",
        "make run",
        "make postflight",
        "make resource-check",
        "make enable",
        "make status",
        "make rollback",
        "delivery-resolve",
        "health-resolve",
    ):
        assert command in operations
    assert "Cookie" in runbook
    assert "不修改现有节点" in operations

    forbidden_commands = re.compile(
        r"^\s*(?:sudo\s+)?(?:apt(?:-get)?\s+(?:upgrade|dist-upgrade)|"
        r"docker\s|ufw\s|iptables\s|nft\s)",
        re.MULTILINE,
    )
    assert not forbidden_commands.search(operations)


def test_security_docs_and_repository_do_not_embed_operator_secrets() -> None:
    security = _text("docs/security/SECURITY.md")
    for item in (
        "TWITTER_AUTH_TOKEN",
        "FEISHU_WEBHOOK_URL",
        "FEISHU_SIGNING_SECRET",
        "最小权限",
        "威胁模型",
        "可信来源",
        "漏洞报告",
    ):
        assert item in security
    assert "security@example.com" not in security

    public_files = [
        path
        for path in ROOT.rglob("*")
        if path.is_file()
        and not any(
            part in {".git", ".venv", ".pytest_cache", "__pycache__"}
            for part in path.parts
        )
    ]
    content = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore") for path in public_files
    )
    public_ips = {
        match.group(0)
        for match in re.finditer(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", content)
        if ipaddress.ip_address(match.group(0)).is_global
    }
    assert not public_ips, f"仓库不应包含运营者的公网 IP: {public_ips}"

    forbidden_personal_patterns = (
        r"[\w.+-]+@gmail\.com",
        r"TWITTER_" r"AUTH_TOKEN=(?!replace-)[^\s]+",
        r"FEISHU_" r"SIGNING_SECRET=(?!replace-)[^\s]+",
        r"/open-apis/bot/v2/hook/(?!replace-)[A-Za-z0-9_-]{10,}",
    )
    assert not [
        pattern for pattern in forbidden_personal_patterns if re.search(pattern, content)
    ]


def test_all_relative_markdown_links_resolve() -> None:
    link_pattern = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
    broken: list[str] = []
    for document in ROOT.rglob("*.md"):
        if any(part in {".git", ".venv", ".pytest_cache"} for part in document.parts):
            continue
        for target in link_pattern.findall(document.read_text(encoding="utf-8")):
            target = target.strip().split("#", 1)[0]
            if not target or target.startswith(("http://", "https://", "mailto:")):
                continue
            if not (document.parent / target).resolve().exists():
                broken.append(f"{document.relative_to(ROOT)} -> {target}")
    assert not broken, f"无效相对链接: {broken}"


def test_ci_is_offline_safe_and_enforces_release_gates() -> None:
    workflow = _text(".github/workflows/ci.yml")
    for required in (
        "python-version: '3.12'",
        "pytest",
        "compileall",
        "bash -n",
        "git diff --check",
    ):
        assert required in workflow
    for forbidden in ("secrets.", "VPS_HOST", "TWITTER_AUTH_TOKEN", "FEISHU_WEBHOOK_URL"):
        assert forbidden not in workflow

    dependabot = _text(".github/dependabot.yml")
    assert 'package-ecosystem: "github-actions"' in dependabot
    assert 'package-ecosystem: "pip"' in dependabot
    assert 'package-ecosystem: "npm"' not in dependabot


def test_reproducible_dependency_locks_are_tracked_and_checked() -> None:
    lockfiles = ("uv.lock", "rsshub/package-lock.json")
    for lockfile in lockfiles:
        assert (ROOT / lockfile).is_file()

    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", *lockfiles],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert tracked.returncode == 0, tracked.stderr

    workflow = _text(".github/workflows/ci.yml")
    assert "git ls-files --error-unmatch uv.lock rsshub/package-lock.json" in workflow
    testing = _text("docs/testing/TESTING.md")
    assert "uv.lock" in testing
    assert "rsshub/package-lock.json" in testing


def test_pytest_imports_src_layout_from_non_ascii_project_paths() -> None:
    project = tomllib.loads(_text("pyproject.toml"))
    assert project["tool"]["pytest"]["ini_options"]["pythonpath"] == ["src"]
    command = "uv sync --locked --extra test --no-editable"
    assert command in _text("README.md")
    assert command in _text("docs/testing/TESTING.md")
    assert command in _text(".github/workflows/ci.yml")
    assert "uv run --no-sync pytest" in _text("README.md")
    assert "uv run --no-sync pytest" in _text("docs/testing/TESTING.md")
    workflow = _text(".github/workflows/ci.yml")
    assert "uv run --no-sync pytest" in workflow
    assert "uv run --no-sync codex-quota-monitor --help" in workflow
