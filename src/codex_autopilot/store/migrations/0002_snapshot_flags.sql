-- 0002: 快照判定字段结构化(P4;persistence.md §3 只增不改)。
-- 背景:G6 恢复三证据与 E1/E3 提前恢复信号需要跨快照比较
-- ordinaryUsageAllowed / spendControlReached / rateLimitReachedType /
-- rateLimitResetCredits 摘要;此前这些字段只存在于 raw(会截断),
-- 库内无结构化形态,历史快照无法参与判定(quota-monitoring §5.2)。

ALTER TABLE quota_snapshots ADD COLUMN ordinary_usage_allowed INTEGER;
ALTER TABLE quota_snapshots ADD COLUMN spend_control_reached INTEGER NOT NULL DEFAULT 0;
ALTER TABLE quota_snapshots ADD COLUMN rate_limit_reached_type TEXT;
ALTER TABLE quota_snapshots ADD COLUMN reset_credits TEXT;
