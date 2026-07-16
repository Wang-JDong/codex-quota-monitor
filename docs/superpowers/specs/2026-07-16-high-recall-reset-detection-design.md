# Codex 额度高召回识别与分级提醒设计

## 1. 背景与问题

当前监控已经具备可信来源白名单、私有 RSSHub、SQLite 去重、飞书签名投递和历史帖子补处理能力，但分类器仍采用“预设句式完全命中才匹配”的封闭式逻辑。X 帖子的表达方式持续变化时，会出现“已经抓到帖子，但因状态表达不在正则中而静默丢弃”的漏报。

已观察到的真实漏报：

> Another reset for our Codex and ChatGPT Work users. ... Should have that sweet 100% weekly usage limit back in a few minutes.

该帖子来自可信账号 `@thsottiaux`，包含产品、额度和未来恢复时间，但旧分类器返回 `reset_state_not_explicit`，因此没有推送。

## 2. 目标

1. 在可信来源范围内优先保证召回率，不因新句式导致静默漏报。
2. 对无法确定已完成、进行中或计划中的内容，发送明确标注为“可能相关”的低优先级提醒。
3. 保留对疑问、否定、转发、引用、营销和机制说明的硬过滤，控制误报。
4. 保留 SQLite 持久去重、at-most-once 投递和人工核对能力。
5. 不引入 LLM 或额外常驻服务，维持当前低配 VPS 的资源边界。

## 3. 非目标

- 不抓取白名单以外的账号。
- 不把普通 Codex 产品新闻、模型发布或使用教程变成额度提醒。
- 不把通知当作 Codex 额度控制面；通知只表达可信 X 原文中的信号。
- 不使用付费 X API，不改动现有代理、SSH、订阅或节点服务。

## 4. 总体流程

```text
X / 私有 RSSHub
    -> 可信来源与原创帖硬过滤
    -> 文本规范化
    -> 高召回候选发现
    -> 状态与置信等级判断
    -> SQLite 原子去重与投递状态
    -> 飞书分级提醒
```

候选发现和状态判断必须是两个独立步骤：状态无法识别时，候选仍然可以进入 `possible_reset`，不能直接丢弃。

## 5. 来源与硬过滤

以下条件必须全部满足：

- 作者标准化后属于 `openai`、`openaidevs`、`thsottiaux` 或 `sama`。
- 帖子是账号原创帖；转发不作为证据。
- 引用帖正文、引用作者和媒体说明只用于展示与审计，不参与主要分类证据。
- 帖子不命中明确的硬排除语义。

硬排除包括：

- 询问是否要重置，例如 `Should we reset ...?`。
- 明确否定，例如 `will not reset`、`no plans to reset`、`not reset`。
- 条件或愿望，例如 `if we reset`、`please reset`。
- 失败或故障描述，例如 `reset failed`。
- 邀请、购买、赠品、积分或营销活动。
- 周期性机制、教程、政策或原理说明，例如 `weekly reset mechanism`、`how reset works`。

排除规则必须针对重置语义，而不是看到任意问号、`should` 或 `reset` 就整体排除。

## 6. 文本规范化与证据词

进入分类前统一：

- 转为大小写不敏感形式。
- 统一弯引号与直引号。
- 将连续空白、换行和常见标点规范化。
- 将“额度恢复”表达归一为同一证据类别，而不要求固定英文句式。

证据分为四组。

### 6.1 产品证据

```text
codex
chatgpt work
codex users
codex usage
```

### 6.2 额度证据

```text
usage limit(s)
rate limit(s)
weekly usage
quota
100% weekly usage
usage back
```

### 6.3 重置或恢复动作证据

```text
reset
resetting
another reset
reset again
restore
replenish
refill
back to 100%
have usage back
apply a reset
added a banked reset
```

### 6.4 时间或状态证据

```text
now
currently
again
later
tomorrow
in a few minutes
in a few hours
over the next day
should have ... back
will ... reset
```

这些词只是证据，不单独构成匹配。最终决定仍需经过来源、产品、额度、动作和排除语义检查。

## 7. 候选判定逻辑

可信原创帖在没有硬排除的前提下，满足以下任一组合时进入候选：

```text
产品证据 + 额度证据 + 重置/恢复动作
```

或：

```text
产品证据 + 重置/恢复动作 + 明确时间/恢复表达
```

`banked reset` 还必须同时出现明确发放动作，例如 `added`、`credited`、`given` 或 `have added`。只有解释 banked reset 是什么，而没有说明已发放时，不进入业务提醒。

当前漏报帖子满足第一种组合：

- 产品：`Codex`、`ChatGPT Work`
- 额度：`100% weekly usage limit`
- 动作：`Another reset`
- 时间：`in a few minutes`

因此应该进入候选，而不是返回 `reset_state_not_explicit` 后静默结束。

## 8. 状态与置信等级

候选进入后按以下优先级判断状态：

| 状态 | 典型证据 | 通知等级 |
|---|---|---|
| `banked_available` | 明确说明已添加、发放或 credited banked reset | 高 |
| `completed` | `have reset`、`are back to 100%`、`limits restored` | 高 |
| `in_progress` | `are resetting`、`resetting now` | 高 |
| `planned` | `will reset`、`another reset ... in a few minutes`、`should have ... back` | 高 |
| `possible_reset` | 候选条件成立，但无法确定上述四种状态 | 低 |

`possible_reset` 不是失败状态，而是“可信来源下的相关信号”。其通知必须明确写出“可能是额度重置，请确认原文”，不能伪装成已完成重置。

## 9. 飞书通知

### 9.1 高置信度通知

标题沿用状态区分：

- `Codex 额度监控｜额度已重置`
- `Codex 额度监控｜额度正在重置`
- `Codex 额度监控｜额度即将重置`
- `Codex 额度监控｜已发放可保存重置次数`

### 9.2 可能相关通知

标题固定为：

```text
Codex 额度监控｜可能是额度重置，请确认
```

正文包含：

- 可信账号和发布时间。
- 原文全文或安全截断文本。
- 原帖链接。
- 命中的产品、额度、动作和时间证据。
- `possible_reset` 状态说明。

所有状态继续使用现有原子 claim、发送状态和不确定投递核对机制。同一帖子不能因为状态升级或重复抓取而重复成功推送。

## 10. 持久化与规则升级

保持 `posts.post_id` 作为唯一去重键，并补充以下可审计信息：

- `classification_version`：分类规则版本。
- `confidence`：`high` 或 `low`。
- `candidate_reason`：候选证据摘要。

`status` 增加 `possible_reset`，`matched=1` 表示该帖子已经进入通知候选，不表示一定已经完成额度重置。

当分类规则版本变更时，部署流程自动对最近一段时间的未匹配帖子执行一次有界重处理：

- 只处理当前可信 feed 仍可取得的帖子。
- 默认回看最近 7 天，最多处理 100 条未匹配记录。
- 已发送、已确认或已有投递状态的记录不重新发送。
- 每条帖子仍通过原子 claim 和 content hash 去重。

这样新规则可以补回历史漏报，同时不会重复刷出旧通知。

## 11. 测试要求

### 正例

- `We have reset Codex usage limits ...`
- `We are once again resetting the usage limits ...`
- `Another reset ... Should have ... weekly usage limit back in a few minutes.`
- `We have added a banked reset ...`
- `Codex usage limits are back to 100%.`
- 只说“可能稍后恢复”但没有固定句式的 `possible_reset`。

### 负例

- `Should we reset Codex usage limits?`
- `We will not reset Codex usage limits.`
- `How the Codex reset mechanism works.`
- `Reset failed.`
- 邀请、购买、赠品或推广链接。
- 不可信作者的相同文本。
- 转发或引用中出现重置文本、原创正文没有重置证据。

### 集成与运维

- `possible_reset` 使用低优先级文案。
- 同一帖子重复运行只发送一次。
- 规则升级可以补回未匹配历史帖子。
- 现有飞书 `uncertain`、`delivery-resolve` 和健康通知状态机不回归。
- 全量 pytest、CLI smoke test、Python 编译、Shell/Node 语法、dry-run、postflight 和资源检查必须通过。

## 12. 资源与安全边界

- 继续使用 Python 标准库确定性规则，不部署模型服务。
- RSSHub 仍按需启动，timer 保持每 30 分钟一次。
- systemd `MemoryMax=384M`、`CPUQuota=30%` 和 loopback 端口保持不变。
- 不读取或写入现有节点配置，不重启 SSH、代理、订阅或 CDN 服务。
- Cookie、飞书 Webhook 和签名密钥仍只存在 VPS `.env`，不进入规则日志、Git 或通知正文。

## 13. 分阶段上线

1. 先实现高召回候选与 `possible_reset`，补充真实漏报和负例测试。
2. 在 VPS 上运行 dry-run，对最近 7 天候选数量和误报样例进行核对。
3. 运行有界历史补处理，补发已确认的漏报；每条帖子验证幂等。
4. 部署正式 timer，观察 24 小时日志和飞书消息。
5. 根据用户确认的误报样例微调排除规则，不回退到固定句式完全命中模型。

## 14. 验收标准

- 真实漏报帖子 `2077607697487188198` 被识别为 `planned` 或等价的高置信度计划重置状态。
- 新句式在未知状态下至少被识别为 `possible_reset`，不再静默丢弃。
- 明确否定、疑问、教程、营销、转发和引用负例仍不推送。
- 高置信度和低置信度通知文案可区分，且都包含原文链接。
- 同一帖子重复运行只发送一次。
- 规则升级可在有界范围内补回历史未匹配帖子。
- VPS 资源、节点服务、端口和秘密边界保持现状。
