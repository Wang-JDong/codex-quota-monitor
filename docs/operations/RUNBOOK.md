# 运维 Runbook

本 Runbook 默认操作目录为 `/opt/codex-quota-monitor`。核心原则是只操作本项目，不修改现有节点、SSH、防火墙、代理或订阅服务。

## 日常状态检查

```bash
cd /opt/codex-quota-monitor
make status
systemctl status codex-quota-monitor.timer
systemctl list-timers codex-quota-monitor.timer
tail -n 100 /var/log/codex-quota-monitor/monitor.log
make resource-check
make postflight
```

`make status` 输出 JSON，重点关注：

- `sources`：是否已建立基线、最后成功时间和最后安全错误。
- `pending`：可以在下一轮尝试的匹配帖子。
- `in_flight`：正在投递的短暂状态；任务结束后仍存在需要调查。
- `uncertain`：服务器可能已收到，不自动重发，需人工核对。
- `permanent_failed`：飞书明确拒绝，需修正配置并人工决定。
- `full_outages` / `alert_active`：抓取健康和是否已确认发出告警。
- `health_transition` / `health_delivery_state` / `health_transition_epoch`：当前健康通知身份及持久投递状态。`uncertain` 绝不自动重发。

没有匹配时不收到飞书消息是预期行为。首次基线静默也是预期行为。

## Cookie rotation

### 触发条件

- 飞书收到明确的 X 认证失败告警。
- `make dry-run` 显示 authentication failed。
- 安全策略要求主动轮换会话。

### 操作

1. 在本地浏览器登录专用 X 帐号，从开发者工具中取得新的 `auth_token` Cookie 值。不要把账号密码放入项目或 VPS。
2. 仅在 VPS 上编辑 `.env`：

   ```bash
   cd /opt/codex-quota-monitor
   sudoedit .env
   make dry-run
   ```

3. dry-run 必须成功读取四来源。无需重启 timer，下一次 service 会重新读取 `.env`。
4. 轮换后确认 `.env` 仍为 `root:codex-monitor` + `0640`。

不要通过 shell 环境导出、命令行参数、issue 或聊天来传递 Cookie。

## 抓取故障排查

### 四个来源全部失败

1. 保持 timer 禁用或暂停，只停止本项目：

   ```bash
   sudo systemctl disable --now codex-quota-monitor.timer
   sudo systemctl stop codex-quota-monitor.service
   ```

2. 查看日志中的安全错误类别，不要增加打印秘密的调试语句。
3. 若为认证失败，执行 Cookie rotation。
4. 若 RSSHub 无法启动，先检查日志中的 Query ID refresh，再检查项目私有 Node.js、`rsshub/node_modules` 和端口占用；不得杀死未识别进程抢占端口，也不得放宽整个 `/opt/codex-quota-monitor` 的写权限。

当前免费 X 适配器的运维基线是：只使用 `UserTweets` 监控四个账号的顶层原创帖，转发、引用和回复不作为证据。`UserTweetsAndReplies` 当前返回 HTTP 404；不要通过打开 `includeReplies` 规避故障。只有上游恢复，且 JSON 解析、作者校验、去重和非证据边界均在测试保护下通过后，才可启用回复。
5. `make dry-run` 通过后，再执行 `make postflight` 和 `make resource-check`，然后 `make enable`。

### 只有个别来源失败

先在 dry-run 重现。查看原路由是否返回空 feed、格式错误或作者/URL 不一致。不要为了“恢复绿色”而自动信任其他账号、转发或不可信引用。只有在产品需求和测试样例同时更新后才能改白名单或解析规则。

## 飞书故障排查

```bash
make test-notification
make status
```

- HTTP 429：客户端仅对这种可确认未接受的限流做有界重试。
- 明确的 4xx 拒绝：检查 Webhook 是否失效、签名密钥是否对应同一机器人；帖子状态为 `permanent_failed`。
- 超时、断连、5xx 或无法确认的 200：服务器可能已接受，状态为 `uncertain`，不自动重发。

## `delivery-resolve` 人工核对

对 `uncertain` 或 `permanent_failed` 帖子：

1. 从 `make status` 和日志确认帖子 ID。
2. 在飞书群中按帖子作者、内容和原文链接核对是否已收到。
3. 已收到时，只记账为 sent：

   ```bash
   sudo -u codex-monitor bash -c 'set -a; source /opt/codex-quota-monitor/.env; set +a; PYTHONPATH=/opt/codex-quota-monitor/src /usr/bin/python3 -m codex_quota_monitor delivery-resolve "$1" --as sent' _ POST_ID
   ```

4. 已确认未收到时，明确放回 pending：

   ```bash
   sudo -u codex-monitor bash -c 'set -a; source /opt/codex-quota-monitor/.env; set +a; PYTHONPATH=/opt/codex-quota-monitor/src /usr/bin/python3 -m codex_quota_monitor delivery-resolve "$1" --as retry' _ POST_ID
   ```

5. 再次执行 `make status`。

不要直接编辑 SQLite，也不要在未查看飞书群时选择 retry。

## `reprocess-post` 补处理历史漏报

当抓取已经保存帖子、但旧规则把它判为未匹配时，先完成代码升级和 dry-run，再按帖子 ID 执行：

```bash
sudo ./deploy/reprocess-post.sh POST_ID
```

脚本在受限的一次性 systemd unit 中启动私有 RSSHub，然后重新从四个可信 feed 找到精确 ID、使用当前规则分类，并仅把数据库中已有的未匹配行提升为待发送。成功输出中的 `changed=true`、`sent=true` 表示本次已补发；重复执行必须返回 `changed=false`、`sent=false`，且 `delivery_attempts` 不增加。帖子不在当前 feed 或仍不匹配时，命令不修改数据库。不要用它导入任意 URL，也不要直接更新 SQLite。

## `health-resolve` 健康通知核对

健康 `alert` 或 `recovered` 在超时、断连、5xx 或响应无法确认后会进入 `uncertain`，下一轮和重启后都不会自动重发。先在飞书群按标题核对，然后执行下列其中一条：

```bash
sudo -u codex-monitor bash -c 'set -a; source /opt/codex-quota-monitor/.env; set +a; PYTHONPATH=/opt/codex-quota-monitor/src /usr/bin/python3 -m codex_quota_monitor health-resolve "$1" --as sent' _ alert
sudo -u codex-monitor bash -c 'set -a; source /opt/codex-quota-monitor/.env; set +a; PYTHONPATH=/opt/codex-quota-monitor/src /usr/bin/python3 -m codex_quota_monitor health-resolve "$1" --as retry' _ alert
```

将末尾 `alert` 换为 `recovered` 即可核对恢复通知。`--as sent` 会在同一事务中确认健康迁移并更新 `alert_active`；`--as retry` 只在已确认群里未收到时使用。不要直接编辑 SQLite。

## 资源或残留进程

```bash
make resource-check
pgrep -af '/opt/codex-quota-monitor/.+rsshub|codex_quota_monitor'
ss -H -ltn 'sport = :1200'
ss -H -ltn 'sport = :1201'
```

单次任务结束后，三条进程/端口查询都不应显示残留。若有残留，停止本项目 service 与 timer，保留现场日志并查明进程归属。不要调整现有节点的端口或内存来为监控腾空间。

## 回滚

```bash
cd /opt/codex-quota-monitor
make rollback
make postflight
```

`make rollback` 只禁用/停止并删除 `codex-quota-monitor.service`、`codex-quota-monitor.timer` 和本项目 logrotate 配置，然后重载 systemd unit 索引。它不删除 SQLite、`.env`、项目文件或现有节点服务。完全删除数据必须是独立、明确审批的操作。

如果 postflight 不通过，不得重新启用本项目；先根据 preflight 快照调查宿主机差异。
