# VPS 部署指南

本文档面向 Ubuntu 24.04 + systemd 的低配 VPS。固定安装路径是 `/opt/codex-quota-monitor`。安装脚本不修改现有节点、SSH、防火墙或系统 Node.js，也不安装 Docker。

## 1. 部署前提

- Linux x86_64，systemd 255 或兼容版本。
- 系统提供 Python 3.12、`curl`、`tar`、`xz`、`rsync`、`sha256sum`、`ss` 和 `useradd`。
- 至少约 512 MiB 可用内存与可用 swap；正式任务会被限制在 384 MiB / 30% CPU。
- 一个专用的、已登录 X 会话的 `auth_token` Cookie。项目不需要也不接受账号密码。
- 一个启用签名校验的飞书自定义机器人。
- root 或等价管理权限。

## 2. 从开发机同步文件

在项目根目录执行，将 `VPS_HOST` 替换为自己的 SSH 主机别名或地址：

```bash
rsync -a \
  --exclude='.git/' --exclude='.worktrees/' --exclude='.venv/' \
  --exclude='.env' --exclude='data/' \
  ./ root@VPS_HOST:/opt/codex-quota-monitor/
```

不要同步 `.env`、SQLite 数据库或本地 Git 元数据，不要覆盖 VPS 上其他服务的目录。

## 3. 上线前快照

```bash
cd /opt/codex-quota-monitor
make preflight
```

preflight 必须在任何安装动作之前通过。它会：

- 确认项目端口 `1200` 和 `1201` 未被占用。
- 确认 `ssh.service`、`sing-box.service`、`cdn-subscription.service`、`friend-clash-sub.service` 和 `share-100gb-sub.service` 处于 active。
- 保存这些 unit 的路径与 SHA-256。
- 保存受保护端口 `22`、`22222`、`2082`、`2086`、`2095`、`2052` 和 `8880` 的监听者归属。
- 显示当前内存与磁盘概况。

任何一项失败都应停止部署，先单独恢复宿主机本身的健康状态。

## 4. 配置秘密

```bash
cp .env.example .env
sudo chown root:root .env
sudo chmod 600 .env
sudoedit .env
```

需填写：

- `TWITTER_AUTH_TOKEN`：浏览器中已登录 X 会话的 `auth_token` Cookie 值。
- `FEISHU_WEBHOOK_URL`：飞书自定义机器人 Webhook。
- `FEISHU_SIGNING_SECRET`：该机器人的签名密钥。

保留 `RSSHUB_BASE_URL=http://127.0.0.1:1200` 和默认数据库路径。首次安装时服务用户尚未存在，因此先用 `root:root` + `0600`。安装脚本创建 `codex-monitor` 后会将其改为 `root:codex-monitor` + `0640`。

Cookie、Webhook 和签名密钥不得出现在 shell 参数、聊天记录、README、issue、日志或 Git 中。

## 5. 安装

```bash
make install
```

安装脚本会：

1. 再次执行 preflight。
2. 创建无登录 shell 的 `codex-monitor` 用户和项目专属可写目录。
3. 下载 Node.js 22.20.0 Linux x64 到项目 `runtime` 目录并校验固定 SHA-256。
4. 在 512 MiB / 40% CPU 的一次性 systemd unit 内执行 `npm ci --omit=dev --ignore-scripts --no-audit --no-fund`。
5. 安装本项目的 service、timer 和 logrotate 配置，但保持 timer 禁用。

安装过程不执行系统包升级，不修改网络规则，不停止或重启现有节点。

## 6. 隔离 dry-run

```bash
make dry-run
```

dry-run 使用固定名称 `codex-quota-monitor-dry-run` 的 transient unit。它强制使用 `http://127.0.0.1:1201` 和 PrivateTmp 中的 `/tmp/codex-quota-monitor-dry-run.db`，与生产 `1200`/SQLite 隔离，并使用同样的 384 MiB、30% CPU 和 5 分钟边界。它不写入正式基线，不发送业务消息。同名 dry-run 已运行时，新的调用应安全失败。

必须确认四个来源均抓取成功，否则先参考 [Runbook](RUNBOOK.md)。

## 7. 验证飞书

```bash
make test-notification
```

飞书群应收到标题为“Codex 额度监控｜系统测试”的一条消息。该命令不读取 feed，不写入业务帖子。

## 8. 建立首次基线并验收隔离

```bash
make run
make postflight
make resource-check
```

`make run` 第一次成功读取每个来源时，会把已有帖子记为基线，不发送历史业务消息。`make postflight` 必须证明受保护服务、unit 哈希和端口监听者与 preflight 完全一致，且项目端口没有残留。

`make resource-check` 必须输出且校验：

```text
MemoryMax=402653184
CPUQuotaPerSecUSec=300ms
```

它们分别对应 384 MiB 和 unit 中的 `CPUQuota=30%`。校验失败时返回非零状态，禁止启用 timer。

## 9. 规则升级后的有界补处理

代码升级并完成 dry-run 后，如需把最近漏报的未匹配帖子按新规则补处理，执行：

```bash
sudo ./deploy/reprocess-unmatched.sh --days 7 --limit 100
```

该命令只扫描最近 7 天、最多 100 条 `matched=0` 记录；每个来源只抓取一次，成功候选才进入现有的原子投递状态机。输出中的 `scanned`、`changed`、`sent`、`skipped` 可用于审计，重复运行必须不新增已发送卡片。低置信度 `possible_reset` 会以橙色卡片标注“可能是额度重置，请确认”，应先核对原文再决定是否需要人工处理。禁止直接改 SQLite。

## 10. 启用 30 分钟定时器

```bash
make enable
```

`make enable` 会先强制执行 `make postflight` 和 `make resource-check`，任一失败都会在调用 `systemctl enable` 前停止。

启用后做一次最终检查：

```bash
sudo systemctl start codex-quota-monitor.service
make postflight
systemctl list-timers codex-quota-monitor.timer
tail -n 100 /var/log/codex-quota-monitor/monitor.log
```

验收标准：日志不包含任何秘密；timer 显示每 30 分钟的下一次运行；没有历史业务卡片；任务结束后 `1200`、`1201` 和项目进程全部消失；受保护服务完全未变。

## 更新与重新部署

代码更新后重复“同步文件 → `make preflight` → `make install` → `make dry-run` → `make postflight` → `make resource-check`”。数据库保留在 `/opt/codex-quota-monitor/data`，不应通过覆盖或删除它来解决部署问题。方法变更前阅读 [变更日志](../../CHANGELOG.md)。
