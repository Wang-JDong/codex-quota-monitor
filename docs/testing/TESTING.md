# 测试与发布门禁

## 测试目标

测试套件必须在不使用真实 X Cookie、飞书 Webhook、VPS、systemd 状态或外网 feed 的情况下完成。它既验证业务判定，也验证“不破坏现有服务”这个部署产品契约。

## 测试层次

| 层次 | 目录 | 关键覆盖 |
| --- | --- | --- |
| 单元测试 | 验证无网络纯逻辑 | 分类规则、配置、投递签名与错误分类 |
| 组件测试 | 验证边界与持久状态 | RSS/Atom/JSON fixture 解析、UserTweets + UserMedia 合并去重、SQLite 基线/状态机 |
| 服务编排测试 | 验证跨组件语义 | 正常静默、健康迁移、pre-send claim、uncertain |
| Shell 行为测试 | 在临时目录与伪系统命令上运行部署脚本 | 端口/PID 归属、受保护服务快照、资源上限、dry-run 隔离 |
| 产品结构测试 | 确保仓库可理解、可安全协作 | 文档层级、相对链接、秘密扫描、CI 门禁 |

## 本地环境

推荐使用 uv 和锁文件：

Python 测试依赖必须使用已跟踪的 `uv.lock`，RSSHub 生产依赖必须使用已跟踪的 `rsshub/package-lock.json`。CI 在安装任何依赖前验证两个文件都来自干净 checkout。

```bash
uv sync --locked --extra test --no-editable
```

也可在已经准备好 Python 3.12 + pytest 8 的隔离环境中运行。生产 VPS 不需要安装 pytest、uv 或任何 Python 运行时依赖。

## 常用命令

全量测试：

```bash
uv run --no-sync pytest
```

单文件/单用例：

```bash
uv run --no-sync pytest tests/test_classifier.py
uv run --no-sync pytest tests/test_service.py::test_first_run_baselines_then_new_match_sends_once
```

Python 语法与导入前置编译：

```bash
uv run --no-sync python -m compileall -q src tests
```

Shell 语法：

```bash
find deploy -type f -name '*.sh' -print0 | xargs -0 -n1 bash -n
```

空白和 patch 健康：

```bash
git diff --check
```

## Shell 行为测试模型

`tests/test_deploy_files.py` 不会在开发机上调用真实 `systemctl`、`ss`、`useradd` 或网络下载。它通过 `CODEX_MONITOR_TESTING=1` 和临时命令目录模拟：

- 受保护 unit 的 active 状态与 FragmentPath。
- 受保护端口的 PID/cgroup 归属以及替换攻击。
- 项目端口占用、RSSHub 健康与 cleanup。
- transient dry-run 的环境覆盖和 systemd 资源属性。
- install 的复制边界，不会真正写入系统 unit 目录。

测试模式只为行为验证存在，不是生产绕过开关。新部署脚本必须使用相同的临时边界测试，不能只做字符串断言。

## 新增判定规则

1. 先在 `tests/test_classifier.py` 添加一个会失败的最小正例或负例。
2. 确认失败原因是缺少该规则，不是 fixture、拼写或时区问题。
3. 使用最小规则让用例通过，再运行全部分类与全量测试。
4. 对每个放宽规则补一个容易误报的负例。
5. 如果改变“转发/引用不作证据”等信任边界，必须同时更新 PRD、架构和安全文档。

## 发布门禁

每个候选版本必须全部通过：

1. Python 3.12 上使用锁定测试依赖的全量 pytest。
2. `compileall` 覆盖 `src` 与 `tests`。
3. 所有 `deploy/*.sh` 的 `bash -n`。
4. `git diff --check`。
5. 产品文档相对链接、个人标识/秘密扫描和危险部署建议检查。
6. 无任何测试需要 CI secret、真实 VPS、飞书或 X 可用。
7. 实际部署前由运维者在目标 VPS 上手动执行 [部署验收](../operations/DEPLOYMENT.md)；CI 绝不执行真实部署。
