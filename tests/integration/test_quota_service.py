"""P3-T4 集成测试:QuotaService 融合(实时/历史/降级/隔离,S-08/S-09/S-20/S-23)。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_autopilot.codexif.appserver import AppServerClient
from codex_autopilot.config import load_config
from codex_autopilot.quota import QuotaService
from codex_autopilot.store.dao import AccountDao, NewSession, SessionDao
from tests.conftest import write_scenario

UUID_A = "01234567-89ab-cdef-0123-456789abcdef"
UUID_B = "fedcba98-7654-3210-fedc-ba9876543210"


@pytest.fixture
def config(tmp_path):
    return load_config(env={}).config.with_overrides(codex_home=tmp_path / "codex-home")


def register_session(store, fake_clock, codex_home, uuid, name) -> int:
    accounts, sessions = AccountDao(store), SessionDao(store)
    with store.transaction():
        account = accounts.get_or_create(str(codex_home), label=None, now=fake_clock.epoch())
        sessions.insert(
            NewSession(
                uuid=uuid,
                name=name,
                project_path="/tmp/project",
                account_id=int(account["id"]),
            ),
            now=fake_clock.epoch(),
        )
    return int(account["id"])


@pytest.fixture
def service_with_fake_appserver(store, config, fake_clock, fake_appserver_env):
    """client_factory 注入 Fake app-server(通过 env 已指向剧本/日志)。"""

    binary, scenario, _ = fake_appserver_env

    def factory(binary_arg: str, codex_home: Path) -> AppServerClient:
        del binary_arg  # 生产 binary 参数被替换为 Fake(测试注入点)
        return AppServerClient(
            binary=str(binary),
            codex_home=codex_home,
            client_version="0.0.0-test",
            call_timeout_sec=2.0,
            shutdown_grace_sec=0.5,
        )

    def make(**scenario_settings):
        write_scenario(scenario, **scenario_settings)
        return QuotaService(store, config, fake_clock, client_factory=factory)

    return make


class TestRefreshAppserver:
    def test_success_persists_snapshot_and_audit(
        self, store, fake_clock, tmp_path, codex_home, service_with_fake_appserver
    ):
        account_id = register_session(store, fake_clock, codex_home, UUID_A, "alpha")
        service = service_with_fake_appserver()
        account = store.query_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
        snapshot = service.refresh_appserver(account, actor="cli:quota")
        assert snapshot.ok and len(snapshot.windows) == 2
        row = store.query_one("SELECT * FROM quota_snapshots WHERE account_id = ?", (account_id,))
        assert row is not None and row["source"] == "appserver" and row["ok"] == 1
        windows = store.query(
            "SELECT * FROM quota_windows WHERE snapshot_id = ? ORDER BY id", (row["id"],)
        )
        assert [w["window_kind"] for w in windows] == ["five_hour", "weekly"]
        audit = store.query("SELECT * FROM audit_log WHERE category = 'quota' AND level = 'info'")
        assert audit and "appserver quota query ok" in audit[-1]["summary"]

    def test_failure_records_error_snapshot(
        self, store, fake_clock, codex_home, service_with_fake_appserver
    ):
        """S-08/S-18:查询失败 → ok=0 错误快照留痕 + warn 审计,不抛异常。"""

        account_id = register_session(store, fake_clock, codex_home, UUID_A, "alpha")
        service = service_with_fake_appserver(hang_rate_limits=True)
        account = store.query_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
        snapshot = service.refresh_appserver(account)
        assert snapshot.ok is False and snapshot.error
        row = store.query_one(
            "SELECT * FROM quota_snapshots WHERE account_id = ? AND ok = 0", (account_id,)
        )
        assert row is not None
        audit = store.query("SELECT * FROM audit_log WHERE category = 'quota' AND level = 'warn'")
        assert audit

    def test_raw_stored_truncated(self, store, fake_clock, codex_home, service_with_fake_appserver):
        account_id = register_session(store, fake_clock, codex_home, UUID_A, "alpha")
        service = service_with_fake_appserver(
            rate_limits_result={
                "rateLimitsByLimitId": {
                    "codex": {"primary": {"usedPercent": 1, "pad": "x" * (80 * 1024)}}
                }
            }
        )
        account = store.query_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
        service.refresh_appserver(account)
        raw = store.query_one("SELECT raw FROM quota_snapshots")["raw"]
        assert raw is not None and len(raw.encode()) <= 64 * 1024


class TestIngestJsonl:
    def test_ingests_latest_token_count(
        self, store, fake_clock, codex_home, write_rollout, fixture_path, config
    ):
        register_session(store, fake_clock, codex_home, UUID_A, "alpha")
        write_rollout(
            codex_home,
            UUID_A,
            fixture_path("jsonl", "token_count_5h_100.jsonl").read_text(),
        )
        service = QuotaService(store, config, fake_clock)
        account = store.query_one("SELECT * FROM accounts")
        snapshot = service.ingest_jsonl(account)
        assert snapshot is not None and snapshot.source == "jsonl"
        assert snapshot.captured_at == 1767225660  # 事件时间 2026-01-01T00:01:00Z
        row = store.query_one("SELECT * FROM quota_snapshots")
        assert row["source"] == "jsonl" and row["ok"] == 1

    def test_dedup_on_reingest(
        self, store, fake_clock, codex_home, write_rollout, fixture_path, config
    ):
        register_session(store, fake_clock, codex_home, UUID_A, "alpha")
        write_rollout(
            codex_home,
            UUID_A,
            fixture_path("jsonl", "token_count_normal.jsonl").read_text(),
        )
        service = QuotaService(store, config, fake_clock)
        account = store.query_one("SELECT * FROM accounts")
        assert service.ingest_jsonl(account) is not None
        assert service.ingest_jsonl(account) is not None  # 同 captured_at,仍返回视图数据
        count = store.query_one("SELECT COUNT(*) AS n FROM quota_snapshots")["n"]
        assert count == 1  # 但库里只有一条(persistence §8 去重)

    def test_torn_tail_not_ingested(self, store, fake_clock, codex_home, write_rollout, config):
        """S-23:撕裂尾行(写入方未完成)不产出快照;不基于不完整数据判定。"""

        register_session(store, fake_clock, codex_home, UUID_A, "alpha")
        full_line = json.dumps(
            {
                "timestamp": "2026-01-01T00:01:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "rate_limits": {
                        "primary": {"resets_at": 1, "used_percent": 100.0, "window_minutes": 300}
                    },
                },
            }
        )
        write_rollout(codex_home, UUID_A, full_line[: len(full_line) // 2])  # 半行,无换行
        service = QuotaService(store, config, fake_clock)
        account = store.query_one("SELECT * FROM accounts")
        assert service.ingest_jsonl(account) is None
        assert store.query_one("SELECT COUNT(*) AS n FROM quota_snapshots")["n"] == 0

    def test_split_chain_latest_wins(
        self, store, fake_clock, codex_home, write_rollout, fixture_path, config
    ):
        """分裂会话:原文件 47% 旧样本,续链文件(事件时间更晚)100% 样本胜出。"""

        register_session(store, fake_clock, codex_home, UUID_A, "alpha")
        write_rollout(
            codex_home,
            UUID_A,
            fixture_path("jsonl", "token_count_normal.jsonl").read_text(),
            ts="2026-01-01T00-00-00",
        )
        continuation = (
            json.dumps(
                {
                    "timestamp": "2026-01-02T00-05:00Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "rate_limits": {
                            "primary": {
                                "resets_at": 1767312000,
                                "used_percent": 100.0,
                                "window_minutes": 300,
                            },
                            "secondary": {
                                "resets_at": 1767657600,
                                "used_percent": 47.0,
                                "window_minutes": 10080,
                            },
                        },
                    },
                }
            )
            + "\n"
        )
        write_rollout(
            codex_home,
            UUID_A,
            continuation,
            ts="2026-01-02T00-00-00",
            suffix_uuid=UUID_B,
        )
        service = QuotaService(store, config, fake_clock)
        account = store.query_one("SELECT * FROM accounts")
        snapshot = service.ingest_jsonl(account)
        assert snapshot is not None
        five = [w for w in snapshot.windows if w.window_kind == "five_hour"]
        assert five[0].used_percent == 100.0

    def test_missing_rollout_returns_none(self, store, fake_clock, codex_home, config):
        register_session(store, fake_clock, codex_home, UUID_A, "alpha")  # 无 rollout 文件
        service = QuotaService(store, config, fake_clock)
        account = store.query_one("SELECT * FROM accounts")
        assert service.ingest_jsonl(account) is None

    def test_no_sessions_returns_none(self, store, fake_clock, codex_home, config):
        accounts = AccountDao(store)
        with store.transaction():
            accounts.get_or_create(str(codex_home), label=None, now=fake_clock.epoch())
        service = QuotaService(store, config, fake_clock)
        account = store.query_one("SELECT * FROM accounts")
        assert service.ingest_jsonl(account) is None


class TestLatestView:
    def test_view_prefers_newest_any_source(
        self, store, fake_clock, codex_home, config, service_with_fake_appserver
    ):
        account_id = register_session(store, fake_clock, codex_home, UUID_A, "alpha")
        service = service_with_fake_appserver()
        account = store.query_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
        service.refresh_appserver(account)  # appserver,captured_at = now
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO quota_snapshots(account_id, source, captured_at, ingested_at, ok,"
                " parser_version) VALUES (?, 'jsonl', ?, ?, 1, 't')",
                (account_id, fake_clock.epoch() + 10, fake_clock.epoch()),
            )
        view = service.latest_view(account)
        assert view.snapshot["source"] == "jsonl"  # 更新的 captured_at 胜出

    def test_stale_after_ttl(
        self, store, fake_clock, codex_home, config, service_with_fake_appserver
    ):
        account_id = register_session(store, fake_clock, codex_home, UUID_A, "alpha")
        service = service_with_fake_appserver()
        account = store.query_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
        service.refresh_appserver(account)
        view = service.latest_view(account)
        assert not view.stale  # 刚采集,fresh_ttl 默认 600s
        fake_clock.advance(601)
        assert service.latest_view(account).stale

    def test_no_snapshot_blocked_unknown(self, store, fake_clock, codex_home, config):
        account_id = register_session(store, fake_clock, codex_home, UUID_A, "alpha")
        service = QuotaService(store, config, fake_clock)
        account = store.query_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
        view = service.latest_view(account)
        assert view.snapshot is None
        assert view.assessment.blocked and view.assessment.unknown_reason == "no_snapshot"

    def test_assessment_blocked_on_exhausted(
        self, store, fake_clock, codex_home, config, service_with_fake_appserver
    ):
        account_id = register_session(store, fake_clock, codex_home, UUID_A, "alpha")
        service = service_with_fake_appserver(
            rate_limits_result={
                "rateLimitsByLimitId": {
                    "codex": {
                        "primary": {
                            "usedPercent": 99,
                            "resetsAt": fake_clock.epoch() + 100,
                            "windowDurationMins": 300,
                        },
                        "secondary": {
                            "usedPercent": 41,
                            "resetsAt": fake_clock.epoch() + 999,
                            "windowDurationMins": 10080,
                        },
                    }
                },
                "ordinaryUsageAllowed": False,
            }
        )
        account = store.query_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
        service.refresh_appserver(account)
        view = service.latest_view(account)
        assert view.assessment.blocked and view.assessment.blocked_kind == "windows"
        assert view.assessment.blocking_keys == ("300",)


class TestAccountIsolation:
    def test_accounts_isolated(
        self, store, fake_clock, tmp_path, write_rollout, fixture_path, config
    ):
        """S-09/T-QUOTA-08:不同 Codex Home 的快照互不可见。"""

        home1 = tmp_path / "home1"
        home2 = tmp_path / "home2"
        home1.mkdir()
        home2.mkdir()
        id1 = register_session(store, fake_clock, home1, UUID_A, "alpha")
        id2 = register_session(store, fake_clock, home2, UUID_B, "beta")
        write_rollout(home1, UUID_A, fixture_path("jsonl", "token_count_5h_100.jsonl").read_text())
        write_rollout(home2, UUID_B, fixture_path("jsonl", "token_count_normal.jsonl").read_text())
        service = QuotaService(store, config, fake_clock)
        account1 = store.query_one("SELECT * FROM accounts WHERE id = ?", (id1,))
        account2 = store.query_one("SELECT * FROM accounts WHERE id = ?", (id2,))
        service.ingest_jsonl(account1)
        service.ingest_jsonl(account2)
        view1 = service.latest_view(account1)
        view2 = service.latest_view(account2)
        assert view1.snapshot["captured_at"] == 1767225660
        five1 = [w for w in view1.snapshot["windows"] if w["window_kind"] == "five_hour"]
        five2 = [w for w in view2.snapshot["windows"] if w["window_kind"] == "five_hour"]
        assert five1[0]["used_percent"] == 100.0
        assert five2[0]["used_percent"] == 47.0
        assert view1.assessment.blocked and not view2.assessment.blocked

    def test_monitored_accounts_and_resolve(self, store, fake_clock, tmp_path, config):
        home1 = tmp_path / "home1"
        home1.mkdir()
        register_session(store, fake_clock, home1, UUID_A, "alpha")
        service = QuotaService(store, config, fake_clock)
        accounts = service.monitored_accounts()
        assert [a["codex_home"] for a in accounts] == [str(home1)]
        assert service.resolve_account(str(home1))["codex_home"] == str(home1)
        assert service.resolve_account("nope") is None


class TestDegradedFallback:
    def test_refresh_failure_keeps_jsonl_view(
        self,
        store,
        fake_clock,
        codex_home,
        write_rollout,
        fixture_path,
        config,
        service_with_fake_appserver,
    ):
        """S-08:实时失败 → 错误快照留痕;视图仍可展示 jsonl 历史(标非实时)。"""

        account_id = register_session(store, fake_clock, codex_home, UUID_A, "alpha")
        write_rollout(
            codex_home,
            UUID_A,
            fixture_path("jsonl", "token_count_normal.jsonl").read_text(),
        )
        service = service_with_fake_appserver()
        account = store.query_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
        service.ingest_jsonl(account)  # 先有历史
        failing = service_with_fake_appserver(hang_rate_limits=True)
        snapshot = failing.refresh_appserver(account)  # 实时查询失败
        assert snapshot.ok is False
        view = failing.latest_view(account)  # 降级:展示 jsonl
        assert view.snapshot["source"] == "jsonl"
        error = failing.latest_appserver_error(account_id)
        assert error is not None and error["ok"] == 0
