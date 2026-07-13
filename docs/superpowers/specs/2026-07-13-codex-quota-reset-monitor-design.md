# Codex 额度重置监控与飞书推送：设计说明

## 1. 背景与目标

OpenAI 会不定期通过 X 发布 Codex 使用额度已重置、正在重置或即将重置的消息。本项目在洛杉矶 VPS 上每小时检查一次可信账号，仅在可信来源明确谈及 Codex 额度重置时，通过飞书机器人通知用户。

首版追求低成本、低误报和可维护性，不购买 X API，不依赖大模型做内容判断。

## 2. 已确认需求

### 2.1 信息源白名单

首版只监控以下账号：

- `@OpenAI`
- `@OpenAIDevs`
- `@thsottiaux`
- `@sama`

账号列表使用配置文件维护，不自动发现或信任其他“OpenAI 员工”账号。新增来源必须人工审核后加入白名单。

`@thsottiaux` 对应 Thibault “Tibo” Sottiaux。OpenAI 官方活动页将其列为 OpenAI 技术成员及 Codex 负责人，因此属于可信的工作人员来源。`@sama` 对应 OpenAI CEO Sam Altman；他本人也曾发布 Codex usage limits 正在重置以及后续重置计划，因此纳入固定白名单。

### 2.2 推送范围

以下三种状态均可推送：

- 已经重置；
- 正在重置；
- 官方明确表示将在某个时间或时间窗口内重置。

普通用户猜测、催促、愿望、提问、条件性奖励、泛泛讨论额度或服务恢复，不推送。

### 2.3 执行频率与静默规则

- 每小时执行一次；
- 没有符合条件的新消息时完全静默；
- 首次启动只建立基线，不补发历史消息；
- 同一 X 帖子最多成功推送一次；
- 连续三个周期无法监控时允许发送一次运维异常告警，恢复后发送一次恢复通知。

## 3. 方案选择

### 3.1 采用方案

采用“按需启动的自建 RSSHub + Python Monitor + SQLite + systemd timer + 飞书自定义机器人”。

- RSSHub 使用一个普通免费 X 小号的会话凭据读取白名单账号时间线；
- Python 3.12 标准库 Monitor 负责标准化、规则判定、去重、状态记录和飞书推送；
- SQLite 保存运行状态；
- systemd timer 每小时启动临时 RSSHub 子进程和一次性 Monitor，抓取结束后停止 RSSHub；
- RSSHub 使用项目目录内的便携式 Node.js，不安装 Docker，不写入系统 Node 环境。

### 3.2 未采用方案

**自写 Playwright 抓取器：** 浏览器资源占用更高，页面结构变化会直接破坏解析。保留“来源适配器”接口，以便 RSSHub 长期失效时替换，但首版不实现。

**匿名公开页面与搜索引擎聚合：** 未登录 X 个人主页不保证按时间展示最新帖子，无法满足尽早发现的要求，只能作为人工排障时的辅助来源。

**付费 X API：** 用户明确不接受相关费用，排除。

## 4. 系统架构

```text
systemd timer（每小时）
          |
          v
启动 RSSHub（仅监听 127.0.0.1）
          |
          v
Monitor 一次性 Python 命令
          |          |
          |          v
          |     四个白名单时间线
          |
          v
标准化 -> 新帖筛选 -> 规则判定 -> SQLite 幂等检查
                                      |
                                      v
                                飞书 Webhook
                                      |
                                      v
                                记录推送结果
```

### 4.1 RSSHub

职责仅限于从 X 获取时间线并输出结构化 Feed。RSSHub 使用专用低权限 X 小号的会话凭据，不保存账号密码。服务只绑定 VPS 本机地址，不提供公网入口。RSSHub 不常驻：systemd 每小时使用项目目录内的便携式 Node.js 启动进程，等待健康检查通过，Monitor 抓取结束后无论成功或失败都终止该进程。

### 4.2 Monitor

Monitor 是一次性 Python 3.12 命令，只使用标准库，不在 VPS 安装 pip 包或创建 venv。每次运行完成以下步骤：

1. 从配置中读取来源白名单；
2. 从 RSSHub 获取每个来源的最新条目；
3. 标准化作者、帖子 ID、正文、引用正文、发布时间和原帖链接；
4. 排除纯转发，保留原帖、回复和引用帖；
5. 找出 SQLite 中未见过的新帖子；
6. 执行确定性判定规则；
7. 对命中帖子生成飞书卡片并发送；
8. 以事务方式保存已见、判定和推送状态；
9. 更新各来源和全局健康状态。

抓取、判定和推送使用清晰接口分离，使未来可以替换 RSSHub 或飞书，而不修改其他模块。

### 4.3 SQLite

SQLite 至少保存以下信息：

- 来源账号和帖子 ID；
- 帖子发布时间与首次发现时间；
- 内容哈希和原帖链接；
- 判定结果、状态标签和命中规则；
- 推送状态、尝试次数和最后错误；
- 各来源最近成功抓取时间；
- 全局连续失败次数和告警状态。

帖子 ID 设置唯一约束。业务帖子和健康 `alert` / `recovered` 都在发送前持久化 claim；健康迁移使用递增 epoch 建立唯一身份。超时、断连、5xx 或无法确认的 200 进入 `uncertain` 且不自动重发；只有确认未接受的 429 可自动重试。

## 5. 内容判定

首版使用确定性规则，不调用大模型。每个推送必须同时通过来源、主题和语气三层检查。

### 5.1 来源检查

- 当前帖子作者必须在白名单中；
- 引用元数据（包括 `quoted_text`）仅用于审计和展示上下文，永不作为分类匹配证据；转发也不作为证据。
- 引用作者是否属于白名单都不扩展证据边界。

### 5.2 主题检查

只有当前白名单作者的原创正文进入分类判定。该正文必须同时具有：

- 产品范围：`Codex`；
- 额度语义：例如 `rate limit`、`usage limit`、`quota`、`usage`；
- 重置动作：例如 `reset`、`resetting`、`reset button pressed`、`additional reset`、`credit a reset`。

规则使用大小写不敏感、词形归一化和短语匹配。不能仅因出现单个 `reset` 就推送。

### 5.3 状态分类

- **已经重置：** 例如 `we have reset`、`limits are reset`、`reset button pressed`；
- **正在重置：** 例如 `we're resetting`；
- **计划重置：** 例如 `we will reset`、`give us 24 hours to reset`、`another reset will come`。

推送只复述官方表达，不验证用户账户是否已经实际到账。

### 5.4 排除规则

以下内容不推送：

- 提问或假设，例如 `should we reset`、`would you like a reset`、`if we reset`；
- 催促、愿望或猜测，例如 `please reset`、`hope they reset`；
- 否定或失败状态，例如 `not reset`、`reset failed`；
- 只介绍额度周期、产品机制或 reset bank 功能；
- 需要邀请好友、购买产品或完成其他条件才能获得的额度；
- 只谈提高额度、系统容量、错误率或服务恢复，没有明确重置动作。

排除规则优先于正向关键词，减少否定句和条件句误报。

### 5.5 可审计性

每次判定都保存状态、命中的正向规则、命中的排除规则和最终原因。规则配置可独立修改并由测试覆盖，不把逻辑散落在抓取或推送代码中。

## 6. 首次运行、增量与补偿

首次成功抓取时，Monitor 将当前 Feed 中的帖子全部记录为已见，但不发送业务通知。只有此基线之后首次发现的帖子才进入推送流程。

正常运行时，每轮获取足够覆盖账号近期活动的条目，并按帖子 ID 去重。VPS 短暂离线后，恢复运行会处理 Feed 中仍可获取、但数据库尚未见过的帖子。若抓取中断时间过长，Feed 已不足以覆盖缺口，系统不能保证自动补齐；此时健康告警会提示人工检查四个白名单账号。

所有存储时间使用 UTC，飞书卡片显示 Asia/Shanghai 时间。

## 7. 错误处理与健康状态

### 7.1 RSSHub 或单一来源失败

- 每个来源最多重试三次，使用短间隔指数退避；
- 单一来源失败不阻塞其他来源；
- HTTP 错误、认证错误、超时、Feed 解析错误均视为抓取失败；
- 正常返回且没有新帖子不视为失败。

### 7.2 全局监控失败

当四个来源连续三个每小时周期均无法成功抓取，或 RSSHub 明确报告 X 会话失效时：

- 发送一次“Codex 额度监控异常”飞书告警；
- 使用持久化 epoch 和发送前 claim 保证同一次故障期间不自动重复发送；
- 后续周期只有在全部白名单恢复后才创建一次 `recovered` 迁移。

普通的“没有匹配帖子”永远不触发健康告警。

### 7.3 飞书失败

- 只对确认未被接受的 HTTP 429 进行有界重试；
- 超时、断连、5xx 和无法确认的 200 都记为 `uncertain`，不自动重发；
- 业务帖子用 `delivery-resolve`，健康告警/恢复用 `health-resolve alert|recovered --as sent|retry` 人工核对；
- 健康通知人工确认为 sent 时，投递状态与 `alert_active` 在同一 SQLite 事务中更新。

### 7.4 数据库与并发

- systemd 和应用内锁共同避免两个 Monitor 实例同时运行；
- 单帖状态更新使用 SQLite 事务；
- 数据库损坏或不可写时停止业务推送并写入错误日志，避免无法去重时重复轰炸。

## 8. 飞书消息

业务卡片标题根据状态显示：

- `Codex 额度重置通知｜已经重置`
- `Codex 额度重置通知｜正在重置`
- `Codex 额度重置通知｜计划重置`

卡片包含：

- 状态的简短中文说明；
- 来源账号；
- 北京时间；
- 官方英文原文；
- X 原帖链接。

首版不自动翻译完整原文，只生成固定中文状态说明，避免翻译改变官方含义。

异常卡片与业务卡片使用不同标题和颜色，明确标注“监控服务异常”或“监控已恢复”。

## 9. 安全设计

- 使用专门的低权限免费 X 小号；
- 只保存 RSSHub 所需会话凭据，不保存 X 密码；
- X 会话、飞书 Webhook 和飞书签名密钥存放在 VPS 的 `root:codex-monitor`、权限 `0640` 环境文件中，只允许 root 和专用服务用户读取；
- `.env`、SQLite、日志和会话文件均加入 `.gitignore`；
- 日志对 Cookie、Webhook、签名和请求头进行脱敏；
- RSSHub 只绑定 `127.0.0.1`；
- 飞书机器人启用签名校验；
- 新建独立的 `codex-monitor` 系统用户运行 RSSHub 和 Monitor；systemd 使用 `NoNewPrivileges`、`ProtectSystem=strict`、`ProtectHome=true` 和明确的可写目录限制。

X 的非付费时间线获取依赖站点当前行为，可能因反爬或内部接口变化而失效。系统通过低频访问、固定白名单和故障告警降低风险，但不承诺永久免维护。

## 10. 部署设计

首版部署目标为现有的 Ubuntu 24.04 RackNerd KVM VPS，硬件为 2 核 Xeon、2.5 GB 内存和 45 GB 磁盘；同机已有代理、订阅分发、节点监控和其他轻量服务，因此本项目必须限制峰值资源并避免常驻占用。

- 不安装 Docker，不运行 `apt upgrade`，不修改 `nftables`、`iptables`、UFW、SSH 或现有 systemd 服务；
- 使用固定版本的便携式 Node.js，解压到 `/opt/codex-quota-monitor/runtime/`，不写入 `/usr`；
- RSSHub 作为项目私有 npm 依赖安装在 `/opt/codex-quota-monitor/rsshub/`，不安装 Chromium；X 用户时间线路由不需要 Puppeteer；
- Monitor 通过系统自带 `/usr/bin/python3` 和 `PYTHONPATH=/opt/codex-quota-monitor/src` 运行，生产代码仅使用 Python 标准库；
- RSSHub 与 Monitor 处于同一个一次性 systemd cgroup，总上限为 `MemoryMax=384M`、`CPUQuota=30%`，并使用低 CPU/I/O 调度优先级；
- 每轮抓取结束后执行清理，成功、失败、超时或信号中断都必须停止 RSSHub；
- 不部署 Redis、PostgreSQL、Chromium、Web 管理后台、pip 包或 venv；
- systemd timer 使用 `Persistent=true`，重启后补跑错过的周期；
- SQLite 保存在项目专用数据目录，代码更新不影响数据；
- 专用日志文件由 logrotate 限制为单文件最多 `5 MiB`、保留四份；
- 提供安装、启动、停止、手动运行、dry-run、测试飞书、查看日志、更新 Cookie 和查看健康状态的单命令入口。

上线前后必须执行节点保护检查。部署前记录 `ssh.service`、`sing-box.service`、`cdn-subscription.service`、`friend-clash-sub.service`、`share-100gb-sub.service` 的状态，以及 `22`、`22222`、`2082`、`2086`、`2095`、`2052`、`8880` 等关键监听端口；部署后逐项复核。任何现有服务或端口异常时，不启用定时器并立即回滚本项目。端口 `1200` 必须在部署前保持空闲，RSSHub 只监听 `127.0.0.1:1200`。

使用 `systemctl show` 检查本项目峰值内存与 CPU 限制，定时任务结束后不得残留 RSSHub 或 Monitor 进程。若进程残留、达到资源上限或影响原服务，不启用定时器。

## 11. 测试策略

### 11.1 单元测试

- 真实历史正例：已完成、进行中和计划重置；
- 反例：普通额度讨论、假设、否定、用户催促、服务恢复、额度提高、邀请奖励；
- 大小写、标点、换行和链接；额外验证引用元数据不能补足任何分类证据；
- 状态分类与命中原因。

### 11.2 状态与幂等测试

- 首次运行只建基线；
- 同一帖子重复出现不重复发送；
- 429 回到待发送；不确定结果进入 `uncertain` 并且不自动重发；
- 进程重启后状态不丢失；
- 并发执行只有一个实例工作。

### 11.3 故障测试

- RSSHub 超时和 5xx；
- X Cookie 失效；
- 单一来源失败与四个来源全局失败；
- 飞书 429、5xx 和网络超时；
- SQLite 不可写；
- 连续三次失败的 `alert` 和全恢复的 `recovered` 各有持久 epoch，不确定结果由 `health-resolve` 核对。

### 11.4 上线验收

1. 用 `dry-run` 获取实时 Feed 并输出判定结果，禁止发送业务通知；
2. 发送一条标题明确包含“系统测试”的飞书卡片；
3. 建立当前时间线基线；
4. 启用 systemd timer；
5. 检查下一周期日志、SQLite 状态和定时器状态。

## 12. 验收标准

- 每小时检查四个固定白名单账号；
- 能识别已重置、正在重置和明确计划重置；
- 已确认的正例全部命中，维护的反例集合全部不推送；
- 首次部署不补发历史帖子；
- 同一帖子最多成功推送一次；
- 飞书临时失败不会丢失待发消息；
- 连续三个周期全局失败只告警一次，恢复只通知一次；
- 没有匹配消息时完全静默；
- Cookie、Webhook 和签名不进入 Git 或日志；
- RSSHub 与 Monitor 合计峰值受 `MemoryMax=384M`、`CPUQuota=30%` 硬限制；
- 每轮结束后 RSSHub 和 Monitor 均不常驻；
- SSH、sing-box、三个订阅分发服务和部署前记录的关键端口在部署后状态不变；
- 安装和运行过程不修改 Docker、系统防火墙、SSH 或任何现有节点文件；
- README 中的单命令操作可以完成手动检查、dry-run、测试通知、更新 Cookie 和查看日志。

## 13. 非目标

首版不包含：

- 付费 X API；
- 自动发现 OpenAI 员工账号；
- 监控非 X 社交平台；
- Web 管理后台；
- 大模型内容分类或全文翻译；
- 自动验证用户 Codex 账户额度是否实际到账；
- Playwright 备用抓取器；
- 自动更新 X Cookie。
- Docker、容器运行时或系统级 Node.js 安装。

## 14. 参考来源

- OpenAI Forum 对 Tibo Sottiaux 身份与职责的说明：<https://forum.openai.com/public/events/codex-is-for-everyone-why-codex-matters-beyond-code-fa40puy7wi>
- OpenAI 对 Sam Altman CEO 身份的说明：<https://openai.com/residency/>
- Sam Altman 的 X 账号：<https://x.com/sama>
- Tibo 发布的计划重置示例：<https://x.com/thsottiaux/status/2066956441173323943>
- RSSHub 项目：<https://github.com/DIYgod/RSSHub>
- RSSHub 的 X GraphQL 兼容修复记录：<https://github.com/DIYgod/RSSHub/issues/18894>
