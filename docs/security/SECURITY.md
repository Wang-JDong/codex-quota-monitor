# 安全说明

## 安全目标

Codex Quota Monitor 的安全目标是在不扩大 VPS 现有攻击面的前提下，保护 X 会话和飞书机器人凭据，并确保不可信社交内容不能扩展成代码执行或动态信任。

## 秘密模型

| 秘密 | 用途 | 存储 | 泄露影响 |
| --- | --- | --- | --- |
| `TWITTER_AUTH_TOKEN` | RSSHub 读取 X 用户 feed | VPS `.env` | 攻击者可冒用该 X 会话 |
| `FEISHU_WEBHOOK_URL` | 向指定飞书机器人发送消息 | VPS `.env` | 攻击者可尝试向群推送内容 |
| `FEISHU_SIGNING_SECRET` | 对机器人请求签名 | VPS `.env` | 攻击者可伪造有效签名 |

`.env.example` 只包含占位符。生产 `.env` 应是 `root:codex-monitor` + `0640`，不得提交 Git、进入镜像、出现在 shell 参数或被打印到日志。数据库不存储上述任何秘密。

项目不存储 X 账号密码，也不自动完成登录。建议使用专用、低权限的 X 账号会话，并在可疑泄露后立即从 X 端撤销会话、重置飞书机器人凭据。

## 最小权限

- 运行用户 `codex-monitor` 是无登录 shell 的专用用户，不需要 sudo。
- systemd 使用 `NoNewPrivileges=true`、`ProtectSystem=strict`、`ProtectHome=true`、`PrivateTmp=true` 和限制的 `ReadWritePaths`。
- X Query ID 刷新作为单独 root 预处理命令运行，只能写入锁定 RSSHub 包的 `dist-lib`；后续 RSSHub/Monitor 均为无特权 `codex-monitor` 进程，依赖文件由 root 所有。
- RSSHub 仅绑定 `127.0.0.1`，不向公网暴露。runner 校验监听地址和 PID 所有权。
- Python 生产代码仅使用标准库；Node.js 和 RSSHub 位于项目私有目录。
- 安装不修改防火墙、SSH 或现有节点，不安装系统级 Node.js 或 Docker。

## 威胁模型

### T1：仓库或日志中的凭据泄露

缓解：`.gitignore`、占位配置、文档/测试扫描、不记录 HTTP body 中的认证数据，并对日志错误使用安全分类标签。

### T2：伪造或污染的 RSS 条目

缓解：严格校验 HTTPS X status URL、路径中的作者和配置路由 handle 一致；把帖子作为数据解析，不作为 shell、HTML 模板指令或日志格式串。

### T3：利用转发/引用扩展信任

缓解：可信来源是固定白名单：`@OpenAI`、`@OpenAIDevs`、`@thsottiaux`、`@sama`。引用元数据（包括 `quoted_text`）仅用于审计和展示上下文，永不作为分类匹配证据；转发也不作为证据。引用作者是否属于白名单都不能扩展证据边界。

### T4：重放或重复通知

缓解：帖子 ID 持久去重、发送前原子 claim、发送内容哈希和 `uncertain` 人工核对。只对确认未被接受的 429 自动重试。

### T5：监控进程抢占宿主机资源

缓解：按需 oneshot、384 MiB 内存、30% CPU、5 分钟超时、Node.js heap 限制，以及确保任务结束后 RSSHub 被回收的 cleanup trap。

### T6：部署损害已有服务

缓解：preflight/postflight 保存并对比受保护服务、unit 哈希和端口监听者。失败时停止上线，不尝试自动修复其他服务。

## 可信来源与引用策略

白名单中的账号身份是信任边界，但帖子文本本身仍是不可信输入。每条业务通知应保留原帖链接供人工确认。若账号被盗、删帖或更正，监控无法证明实际用户的 Codex 额度状态。不应使用这个通知作为任何不可逆操作的唯一依据。

## 依赖与供应链

- Python 测试依赖由 `uv.lock` 锁定；生产执行不需要 pip 包。
- RSSHub npm 依赖由 `rsshub/package-lock.json` 锁定，安装使用 `npm ci` 与 `--ignore-scripts`。
- Node.js 版本和官方归档 SHA-256 写入安装脚本。
- Dependabot 只检查 GitHub Actions 和 Python 依赖。RSSHub/npm 锁定更新必须人工审阅和验证，不自动提升。

## 漏洞报告

不要在公开 issue 中粘贴 Cookie、Webhook、签名密钥、真实主机信息或可利用详情。如果尚未配置仓库的私密安全报告通道，请只创建一个不含技术细节的 issue，说明“需要与维护者建立私密联系”。仓库维护者应尽快启用并在此处记录实际的私密报告机制，而不放置虚构邮箱。

## 响应步骤

1. 立即停止本项目 timer，但不修改现有节点。
2. 撤销受影响的 X 会话和飞书机器人凭据。
3. 保留去秘密化的日志、时间线和变更哈希。
4. 为缺陷先写回归测试，再修复与审查。
5. dry-run、全量测试、preflight/postflight 与资源校验均通过后再上线。
