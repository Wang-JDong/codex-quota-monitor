# 贡献指南

感谢你帮助 Codex Quota Monitor 变得更可靠。本项目优先保持低误报、可审计、低资源和不干扰宿主机现有服务。

## 开始之前

- 阅读 [PRD](docs/product/PRD.md)、[架构](docs/architecture/ARCHITECTURE.md) 和 [安全说明](docs/security/SECURITY.md)。
- 功能、可信来源或投递语义变更应先通过 issue 对齐；不要在 PR 中顺带扩大信任边界。
- 不要上传真实 Cookie、Webhook、签名密钥、账号、邮箱、VPS 地址或运维日志。
- 当前仓库尚未确定开源许可证；外部复用方式应先与维护者确认。

## 本地开发

```bash
uv sync --locked --extra test
uv run pytest
uv run python -m compileall -q src tests
find deploy -type f -name '*.sh' -print0 | xargs -0 -n1 bash -n
git diff --check
```

生产代码不能因便利测试而引入非标准库依赖。测试不能呼叫真实 X、飞书、VPS 或 systemd。详细策略见 [测试指南](docs/testing/TESTING.md)。

## 变更流程

1. 先写一个最小的失败测试，确认它因缺少所需行为而失败。
2. 用最小实现使其通过，然后再重构。
3. 对放宽的分类规则同时添加误报负例。
4. 用户可见变更写入 `CHANGELOG.md`；架构取舍写入新 ADR。
5. 运行全部发布门禁，再提交一个单一目标的 PR。

## PR 检查清单

- [ ] 变更与 PRD 和安全边界一致。
- [ ] 已观察到新测试在实现前正确失败。
- [ ] 全量 pytest、compileall、Shell 语法和 diff 检查通过。
- [ ] 文档、fixture、日志与 workflow 不含个人信息或秘密。
- [ ] 未改变 SSH、防火墙、现有节点或订阅服务。
