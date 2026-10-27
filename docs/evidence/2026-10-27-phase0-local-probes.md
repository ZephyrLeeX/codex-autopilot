# 证据记录:2026-10-27 Phase 0 本地接口探测(L2 级,全只读)

- 机器:开发机(Arch Linux,Python 3.13 环境)
- 本地 Codex CLI:`codex-cli 0.162.1`(npm/mise 安装,`~/.local/share/mise/installs/node/24/bin/codex`)
- 探测性质:全部只读(`--version` / `--help` / schema 导出 / 目录与文件结构扫描);**未投递任何消息、未启动 app-server 会话、未消耗额度**
- 记录者:Phase 0 调研会话

## 1. `codex queue --help` 实测(节选)

```
Usage: codex queue [OPTIONS] --thread <THREAD> --message <TEXT>
      --thread <THREAD>    Session UUID or exact session name
      --message <TEXT>     Message text to queue
```

结论:queue 子命令存在;`--thread` 与 `--message` 拼写确认;参数同时接受 UUID 或名称(本项目设计只允许 UUID 形式)。

## 2. `codex app-server` 实测

```
Usage: codex app-server [OPTIONS] [COMMAND]
Commands: daemon / proxy / generate-ts / generate-json-schema / help
```

用 `generate-json-schema --out /tmp/codex-ap-schema` 离线导出协议 schema(v1+v2),据此确认:

- 请求方法共 105 个;只读额度方法名为 **`account/rateLimits/read`**(精确,含大小写);
- **`account/rateLimitResetCredit/consume`** 存在(消耗重置券——本项目永久黑名单);
- `RateLimitWindow`(v2)= `usedPercent`(必填)/ `resetsAt`(int64 秒,可空)/ `windowDurationMins`(可空);**无 window_type 字段**,窗口类型靠时长区分(300/10080);
- `GetAccountRateLimitsResponse` 顶层含 `ordinaryUsageAllowed`(描述原文:"clients must not infer recovery from percentages or reset times")、`rateLimitResetCredits`、`accountId`;
- `RateLimitResetCreditStatus` = available/redeeming/redeemed/unknown;`PlanType` 17 值;`RateLimitReachedType` 5 值(rate_limit_reached、workspace_{owner,member}_{credits_depleted,usage_limit_reached});
- 通知 `AccountRateLimitsUpdatedNotification` 存在(服务端可推送额度更新)。

## 3. rollout JSONL 结构扫描(仅键名/枚举值,未读内容)

样本:`~/.codex/sessions/2026/10/21/rollout-2026-10-21T17-25-59-<uuid>.jsonl`(27 行)

- 顶层 type:`session_meta` / `turn_context` / `response_item` / `event_msg` / `token_usage_record` / `world_state`
- `event_msg.payload.type`:`task_started`(含 `turn_attribution.turn_trigger`)、`item_completed`、`token_count`(含 `rate_limits`)、`task_complete`、`thread_settings_applied`
- `token_count.rate_limits` 实测值(**snake_case**,与 App Server camelCase 不同):

```json
{"primary": {"resets_at": 1792583368, "used_percent": 100.0, "window_minutes": 300},
 "secondary": {"resets_at": 1793099965, "used_percent": 47.0, "window_minutes": 10080}}
```

(真实五小时窗口 100% 耗尽样本;`rate_limit_reached_type: null`、`plan_type: "team"`)

- `response_item.payload`:`message`(role ∈ user/assistant/developer,含 `metadata.client_authored`)、`agent_message`、`reasoning` 等
- 会话文件命名:`rollout-<YYYY-MM-DDThh-mm-ss>-<uuid>.jsonl`,目录 `sessions/YYYY/MM/DD/`

## 4. Codex 自带 SQLite 实测(更正:三库存在于 Codex Home 根目录)

初测只看了 `~/.codex/sqlite/codex.db`(仅 automations/inbox/local_thread_catalog),误判"0.162.1 CLI 无 thread_history/queue 库"。**复核更正**(根目录完整列表,配合上游源码核实):

- `~/.codex/thread_history_1.sqlite`(101M):表 `thread_turns`、`thread_items`、`thread_realtime_items`、`thread_history_projection_state`
- `~/.codex/queue_1.sqlite`(283k):表 `queued_items`、`queued_thread_revisions`
- `~/.codex/state_5.sqlite`(13M)、`logs_2.sqlite`、`goals_1.sqlite`、`memories_1.sqlite`
- 其他:`history.jsonl`(TUI 手工输入历史)、`session_index.jsonl`、`auth.json`(未读,永不读)

结论修订:0.162.1 CLI 与 0.164 桌面版同样具备结构化三库(代数后缀 `_N`),检测/确认/队列观察的主源在两个目标版本均可用;JSONL 仍是恒备回退。

## 5. 其他目录事实

`~/.codex/` 下存在:`archived_sessions/`、`thread-writer-locks/`(本机空)、`ipc/ipc.sock`、`sqlite/`、`sessions/`(11 个 jsonl)。
