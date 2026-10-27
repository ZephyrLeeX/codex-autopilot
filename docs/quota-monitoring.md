# 额度监控设计(quota-monitoring.md)

| 项 | 值 |
| --- | --- |
| 状态 | Phase 1 定稿 v1.0(字段级事实以 phase0-research.md 实证为准) |
| 覆盖需求 | FR-03、FR-04、FR-02-A5/A6/A7 |
| 关联 | [persistence.md](persistence.md) §3(快照表)、[early-recovery.md](early-recovery.md) |

---

## 1. 额度域模型

`account/rateLimits/read` 响应的顶层结构(参考项目实证 + 本机 schema 导出,两源一致):

```
{
  "rateLimitsByLimitId": {            # 新版键;旧版回退键为 "rateLimits"(单一快照)
    "<limit_id>": {                    # 额度桶:实测值 "codex"、"premium"(常全 null)、"legacy"、其他
      "primary":   { usedPercent, windowDurationMins, resetsAt },   # 五小时窗(300)
      "secondary": { usedPercent, windowDurationMins, resetsAt },   # 每周窗(10080)
      "spendControlReached": bool,
      "rateLimitReachedType": "rate_limit_reached" | "workspace_owner_credits_depleted" | ...,
      "planType": ..., "credits": {...}                              # 展示用,白名单外字段解析时丢弃
    }
  },
  "account": { "email": ... },        # 展示用
  "ordinaryUsageAllowed": bool|null    # 权威放行标志(见 §5.2)
}
```

一个账户在任一时刻的额度状态 = 一个快照(snapshot),模型化为:

- **桶集合**(`limit_id` → bucket,数量不限,模型不设上限);每桶内 **窗口集合**(第一版观察为 primary+secondary 两个):`used_percent`、`resets_at`(Unix 秒,可空)、`window_minutes`(可空);
- **spend control / credits 标志**:`spendControlReached`、`rateLimitReachedType`、`credits.hasCredits`;
- `plan_type`(展示用,不参与判定);
- App Server 响应另有 **`ordinaryUsageAllowed`**(bool|null):后端对"当前是否允许普通用量"的权威回答。

## 2. 两个数据源

| | App Server(实时) | rollout JSONL(历史) |
| --- | --- | --- |
| 获取方式 | `codex app-server` 子进程 + JSON-RPC `account/rateLimits/read` | 解析会话文件中的 `token_count` 事件的 `rate_limits` 字段 |
| 实时性 | 查询即时刻 | 最后一次写事件的时刻(通常滞后于"现在",且只反映该会话写过的时点) |
| 字段命名 | camelCase(`usedPercent`、`resetsAt`、`windowDurationMins`) | snake_case(`used_percent`、`resets_at`、`window_minutes`)——本机 0.162.1 双源实测命名不一致,解析层必须两套兼容 |
| 用途 | 等待期刷新、投递前守门(G6)、`quota --refresh` | 失败检测时的伴随证据、App Server 不可用时的降级展示 |
| 永远不得 | 写任何东西 | 被当作实时数据展示 |

**来源标注是硬要求(FR-04-A2)**:任何对外输出(命令行/通知/日志)的每条窗口数据都携带 `source`、`captured_at`、`stale` 三元组。`stale` 判定:`captured_at` 距今 > `fresh_ttl`(默认 600s,可配)。

## 3. 窗口类型判定(FR-03-A2)

```
window_kind =
    window_minutes == 300   → five_hour
    window_minutes == 10080 → weekly
    有显式 window_type 字段(旧版本/其他来源) → 按字段值
    primary/secondary 位置  → 仅当分钟数字段缺失:primary→five_hour(inferred=1),secondary→weekly(inferred=1)
    其他/未知              → unknown(保守阻塞)
```

- `inferred=1` 的窗口在输出中必须可见标记;守门 G6 对 inferred 窗口要求更严格(见 §5)。
- `unknown` 窗口参与最保守处理:若该窗口 `used_percent ≥ 阈值` 或缺失 → 视为阻塞;何时解除:只能靠该窗口后续快照显示低用量或 `ordinaryUsageAllowed=true`。
- 双写兼容:解析器接受 camelCase 与 snake_case 两套键名(同名映射表集中一处,禁止散落)。

## 4. 快照采集与合并(FR-02-A7)

每 tick(间隔见 §6):

1. 按账户分组;每账户至多一次 App Server 查询(失败记错误快照,`ok=0`,该账户本轮 = unknown,S-08/S-18);
2. 同时,各会话 rollout 增量解析得到的 `token_count.rate_limits` 也作为快照入库(source=jsonl,captured_at=事件时间);
3. 账户"当前视图" = 最新一条 `ok=1` 快照(不分来源,按 captured_at);但 G6 守门只接受 **appserver 来源且新鲜** 的证据(§5);
4. 同账户 15 分钟内同源同 captured_at 快照去重(控体积,persistence.md §8)。

App Server 子进程生命周期:守护进程内每账户保持一个常驻子进程(按需惰性启动,空闲超时回收),避免每 tick 冷启动;进程死亡自动重启(带退避)。协议细节(参考项目实证,见 phase0-research.md §3):`codex app-server --stdio` 子进程,**NDJSON**(每行一个 JSON-RPC 消息,行上限 1 MiB);握手 `initialize`(params 含 `clientInfo{name,version}` + `capabilities.experimentalApi`)→ 收到结果后发 `initialized` 通知;响应按 `id` 匹配,每调用 25s 超时;服务端反向请求一律以 `-32601` 拒绝;退出时关 stdin → 等待 → terminate。若服务端推送额度更新通知(`account/rateLimits/updated`,schema 已见定义),作为提前恢复的额外信号(early-recovery.md §3)。

## 5. 耗尽与恢复判定

### 5.1 耗尽(FR-03-A3)

- **任一桶任一窗口** `used_percent ≥ 阈值`(默认 99,可配 95–100)⇒ 该窗口 `is_exhausted=1`;账户被阻塞的窗口集合 = 所有命中窗口;调度参考时间 = **max(所有阻塞窗口的 resets_at)**(等最晚,参考项目同款)。99 与 100 同样触发(参考项目实证:真实样本 primary=98% 即失败,服务器侧预扣,99% 阈值即为提前布局)。
- `spendControlReached=true` 或 `rate_limit_reached_type` ∈ credits/workspace 类 ⇒ 记 `quota_credits`/workspace 类失败,blocking 无 resets_at——**只能人工或官方改变**,工具只观察。
- 90–98% 窗口仅作 hint(调度参考,标记 `uncertain`),不作耗尽判定(§5.2 要求现场复查)。

### 5.2 恢复(G6 通过条件,全部满足)

对失败记录的每个 `blocking_window_key`:

1. **窗口滚动证据**:存在更新的快照,其中同 `bucket_key` 窗口 `resets_at` 已变化(新窗口)或 `used_percent < 恢复下限`(默认 `min(resume_percent)=90`,独立于耗尽阈值);**且**
2. **证据新鲜**:该快照 source=appserver 且非 stale;**且**
3. **账户级放行**:`ordinaryUsageAllowed` 为 `true`(若该字段可用;为 `null` 时视为不可用,退回 1+2 并在 evidence 记录 `ordinary_usage_allowed=null`)。

官方 schema 对 `ordinaryUsageAllowed` 的描述原文:*"clients must not infer recovery from percentages or reset times"* ——本设计把"百分比+重置时间"作为第一、二道证据,把该字段作为第三道(有则必须为 true),三者缺一不可;这与任务书"满足经过明确配置的保守恢复条件"一致。**任何配置组合都不允许关闭第 1 条(窗口滚动证据)**——只看时间到了就投,是被明确禁止的(FR-03-A8/D6)。

对 `unknown` kind 的失败:G6 恒不通过(策略 manual_ack)。

## 6. 采集节奏(默认,全部可配)

| 场景 | App Server 查询间隔 |
| --- | --- |
| 账户无等待中计划 | 300s |
| 账户存在 WAITING_RECOVERY 会话 | 60s(密集刷新,服务提前恢复检测) |
| 连续失败退避 | 60→120→300s 封顶;`quota`/`check` 命令手动触发不受限 |

rollout 文件增量读取:每 tick 每会话(只读尾部增量,游标持久化在内存 + 启动时重建)。

## 7. 展示(`quota` 命令)

见 cli-design.md §3;每行强制携带来源徽标:`appserver(实时)` / `jsonl(历史)`、采集时间、stale 标记。多账户并列展示,绝不混合(FR-02-A6)。

## 8. 关键设计决策

| # | 决策 | 理由 | 被否决的替代 |
| --- | --- | --- | --- |
| D-QM-1 | 双源双命名兼容(camel/snake) | 0.162.1 实测两源命名不一致;兼容层集中 | 只支持一套(另一源直接废) |
| D-QM-2 | 恢复三证据(滚动+新鲜+ordinaryUsageAllowed) | 官方明确警告不得只凭百分比/时间推断;三证据把假恢复窗口压到最低 | 到点即恢复(任务书明令禁止) |
| D-QM-3 | 阈值 99% 默认、恢复下限独立(90%) | 耗尽判定偏早布局,恢复判定偏保守,两者解耦 | 同一阈值双向用(抖动敏感) |
| D-QM-4 | unknown 窗口/未知 kind 恒阻塞 | 无法证实 ⇒ 无法行动(fail-closed) | 乐观放行 |
| D-QM-5 | App Server 常驻子进程按账户 | 免每 tick 冷启动;故障重启带退避 | 每次 spawn(慢且浪费) |
| D-QM-6 | JSONL 快照不参与 G6 | 历史数据不能当恢复证据(任务书红线) | 混用最新时间戳(看似合理,实为历史冒充实时) |
