"""P2-T2 DAO 测试:账户、会话、运行期标志、约束映射。"""

from __future__ import annotations

import pytest

from codex_autopilot.store import Store
from codex_autopilot.store.dao import AccountDao, NewSession, RuntimeFlagsDao, SessionDao
from codex_autopilot.store.errors import ConstraintViolationError

UUID_A = "01234567-89ab-cdef-0123-456789abcdef"
UUID_B = "fedcba98-7654-3210-fedc-ba9876543210"


@pytest.fixture
def daos(store: Store):
    return AccountDao(store), SessionDao(store), RuntimeFlagsDao(store)


def _add_session(
    sessions: SessionDao, account_id: int, *, uuid=UUID_A, name="alpha", auto_resume=False, **kwargs
):
    return sessions.insert(
        NewSession(
            uuid=uuid,
            name=name,
            project_path="/tmp/project",
            account_id=account_id,
            auto_resume=auto_resume,
            **kwargs,
        ),
        now=1000,
    )


class TestAccountDao:
    def test_get_or_create_idempotent(self, daos):
        accounts, _, _ = daos
        first = accounts.get_or_create("/home/u/.codex", label=None, now=1000)
        second = accounts.get_or_create("/home/u/.codex", label=None, now=2000)
        assert first["id"] == second["id"]
        assert second["created_at"] == 1000  # 不重复创建

    def test_distinct_homes_distinct_accounts(self, daos):
        accounts, _, _ = daos
        a = accounts.get_or_create("/h1", label=None, now=1000)
        b = accounts.get_or_create("/h2", label=None, now=1000)
        assert a["id"] != b["id"]


class TestSessionDao:
    def test_insert_and_resolve(self, daos):
        accounts, sessions, _ = daos
        account = accounts.get_or_create("/h", label="main", now=1000)
        _add_session(sessions, account["id"])
        assert sessions.get_by_uuid(UUID_A)["name"] == "alpha"
        assert sessions.get_by_name("alpha")["uuid"] == UUID_A
        assert sessions.resolve("alpha")["uuid"] == UUID_A
        assert sessions.resolve(UUID_A)["name"] == "alpha"
        assert sessions.resolve(UUID_A.upper())["uuid"] == UUID_A  # 大写规范化

    def test_resolve_never_prefix_matches(self, daos):
        """D-CLI-2/G5:绝不前缀模糊匹配(错投风险)。"""

        accounts, sessions, _ = daos
        account = accounts.get_or_create("/h", label=None, now=1000)
        _add_session(sessions, account["id"])
        assert sessions.resolve("0123") is None
        assert sessions.resolve("alph") is None
        assert sessions.resolve("") is None

    def test_duplicate_uuid_rejected(self, daos):
        accounts, sessions, _ = daos
        account = accounts.get_or_create("/h", label=None, now=1000)
        _add_session(sessions, account["id"], uuid=UUID_A, name="one")
        with pytest.raises(ConstraintViolationError) as excinfo:
            _add_session(sessions, account["id"], uuid=UUID_A, name="two")
        assert excinfo.value.key == "duplicate_uuid"

    def test_duplicate_name_rejected(self, daos):
        accounts, sessions, _ = daos
        account = accounts.get_or_create("/h", label=None, now=1000)
        _add_session(sessions, account["id"], uuid=UUID_A, name="dup")
        with pytest.raises(ConstraintViolationError) as excinfo:
            _add_session(sessions, account["id"], uuid=UUID_B, name="dup")
        assert excinfo.value.key == "duplicate_name"

    def test_fk_violation_rejected(self, daos):
        _, sessions, _ = daos
        with pytest.raises(ConstraintViolationError) as excinfo:
            _add_session(sessions, account_id=9999)
        assert excinfo.value.key == "fk_violation"

    def test_list_active_joins_account_and_state(self, daos, store):
        accounts, sessions, _ = daos
        account = accounts.get_or_create("/h", label="lab", now=1000)
        _add_session(sessions, account["id"], name="a")
        _add_session(sessions, account["id"], uuid=UUID_B, name="b")
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO session_state(session_uuid, state, since) VALUES (?, 'ACTIVE', 5)",
                (UUID_B,),
            )
            conn.execute(
                "INSERT INTO state_history(session_uuid, from_state, to_state, reason,"
                " evidence, ts) VALUES (?, NULL, 'ACTIVE', 'registered', '{}', 42)",
                (UUID_B,),
            )
        rows = sessions.list_active()
        assert [row["name"] for row in rows] == ["a", "b"]  # 按名称排序
        by_name = {row["name"]: row for row in rows}
        assert by_name["a"]["state"] is None
        assert by_name["a"]["last_event_at"] is None
        assert by_name["b"]["state"] == "ACTIVE"
        assert by_name["b"]["state_since"] == 5
        assert by_name["b"]["last_event_at"] == 42
        assert by_name["a"]["account_codex_home"] == "/h"
        assert by_name["a"]["account_label"] == "lab"

    def test_soft_delete_hides_from_list(self, daos, store):
        accounts, sessions, _ = daos
        account = accounts.get_or_create("/h", label=None, now=1000)
        _add_session(sessions, account["id"])
        assert sessions.soft_delete(UUID_A, now=2000) == 1
        assert sessions.list_active() == []
        row = sessions.get_by_uuid(UUID_A)  # 行保留,历史可溯
        assert row["deleted_at"] == 2000
        assert sessions.soft_delete(UUID_A, now=3000) == 0  # 重复软删除无效

    def test_purge_removes_dependents_keeps_audit(self, daos, store):
        accounts, sessions, _ = daos
        account = accounts.get_or_create("/h", label=None, now=1000)
        _add_session(sessions, account["id"])
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO failures(session_uuid, turn_id, kind, detected_at, evidence,"
                " blocking_window_keys) VALUES (?, 't1', 'quota_five_hour', 1, '{}', '[]')",
                (UUID_A,),
            )
            conn.execute(
                "INSERT INTO deliveries(failure_id, attempt_no, argv, started_at)"
                " VALUES (1, 1, '[]', 1)",
            )
            conn.execute(
                "INSERT INTO confirmations(delivery_id, method, evidence, created_at)"
                " VALUES (1, 'manual_ack', '{}', 1)",
            )
            from codex_autopilot.util.audit import write_audit

            write_audit(
                conn,
                ts=1,
                actor="cli:add",
                category="admin",
                level="info",
                summary="keep me",
                session_uuid=UUID_A,
            )
        sessions.purge(UUID_A)
        assert sessions.get_by_uuid(UUID_A) is None
        assert store.query("SELECT * FROM failures") == []
        assert store.query("SELECT * FROM deliveries") == []
        assert store.query("SELECT * FROM confirmations") == []
        assert store.query_one("SELECT COUNT(*) AS c FROM audit_log")["c"] == 1  # 审计保留


class TestRuntimeFlagsDao:
    def test_set_get_update(self, daos):
        _, _, flags = daos
        assert flags.get("global_pause") is None
        flags.set("global_pause", True, now=1)
        assert flags.get("global_pause") is True
        flags.set("global_pause", False, now=2)
        assert flags.get("global_pause") is False
        flags.set("complex", {"nested": [1, 2]}, now=3)
        assert flags.get("complex") == {"nested": [1, 2]}
