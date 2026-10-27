"""P2-T4 CLI 测试:add / remove / list、--json 契约、退出码(T-CLI-ADD-*)。"""

from __future__ import annotations

import json

from codex_autopilot.store import Store

UUID_A = "01234567-89ab-cdef-0123-456789abcdef"
UUID_B = "fedcba98-7654-3210-fedc-ba9876543210"

ADD_KEYS = {
    "name",
    "uuid",
    "description",
    "project_path",
    "codex_home",
    "account_label",
    "enabled",
    "auto_resume",
    "tmux_session",
    "check_interval",
    "created_at",
}
LIST_KEYS = {
    "name",
    "uuid",
    "account",
    "enabled",
    "auto_resume",
    "state",
    "state_since",
    "last_event_at",
    "created_at",
}


def _add(cli, codex_home, *extra, uuid=UUID_A, name="alpha"):
    return cli(
        "--json",
        "add",
        "--session",
        uuid,
        "--name",
        name,
        "--codex-home",
        str(codex_home),
        *extra,
    )


class TestAdd:
    def test_success_json_schema(self, cli, codex_home, tmp_path):
        result = _add(cli, codex_home, "--project", str(tmp_path))
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert set(payload) == ADD_KEYS
        assert payload["uuid"] == UUID_A
        assert payload["name"] == "alpha"
        assert payload["auto_resume"] is False  # 安全默认关(D-SES-3)
        assert payload["enabled"] is True
        assert isinstance(payload["created_at"], int)
        assert payload["codex_home"] == str(codex_home.resolve())

    def test_human_output_contains_reminder(self, cli, codex_home, tmp_path):
        result = cli(
            "add",
            "--session",
            UUID_A,
            "--name",
            "alpha",
            "--codex-home",
            str(codex_home),
            "--project",
            str(tmp_path),
        )
        assert result.exit_code == 0
        assert "alpha" in result.stdout
        assert "auto_resume" in result.stdout and "off" in result.stdout
        assert "never deliver" in result.stdout  # 提醒不投递

    def test_invalid_uuid_rejected(self, cli, codex_home):
        result = _add(cli, codex_home, uuid="not-a-uuid")
        assert result.exit_code == 2
        assert json.loads(result.stdout)["error"]["code"] == "invalid_uuid"

    def test_uppercase_uuid_normalized(self, cli, codex_home, tmp_path):
        result = _add(cli, codex_home, "--project", str(tmp_path), uuid=UUID_A.upper())
        assert result.exit_code == 0
        assert json.loads(result.stdout)["uuid"] == UUID_A

    def test_invalid_name_rejected(self, cli, codex_home, tmp_path):
        result = _add(cli, codex_home, "--project", str(tmp_path), name="BadName")
        assert result.exit_code == 2
        assert json.loads(result.stdout)["error"]["code"] == "invalid_name"

    def test_duplicate_uuid(self, cli, codex_home, tmp_path):
        _add(cli, codex_home, "--project", str(tmp_path))
        result = _add(cli, codex_home, "--project", str(tmp_path), name="beta")
        assert result.exit_code == 1
        assert json.loads(result.stdout)["error"]["code"] == "duplicate_uuid"

    def test_duplicate_name(self, cli, codex_home, tmp_path):
        _add(cli, codex_home, "--project", str(tmp_path))
        result = _add(cli, codex_home, "--project", str(tmp_path), uuid=UUID_B)
        assert result.exit_code == 1
        assert json.loads(result.stdout)["error"]["code"] == "duplicate_name"

    def test_missing_codex_home_rejected(self, cli, tmp_path):
        result = cli(
            "--json",
            "add",
            "--session",
            UUID_A,
            "--name",
            "alpha",
            "--codex-home",
            str(tmp_path / "nope"),
        )
        assert result.exit_code == 2
        assert json.loads(result.stdout)["error"]["code"] == "codex_home_missing"

    def test_missing_project_rejected(self, cli, codex_home, tmp_path):
        result = _add(cli, codex_home, "--project", str(tmp_path / "gone"))
        assert result.exit_code == 2
        assert json.loads(result.stdout)["error"]["code"] == "project_missing"

    def test_allow_missing_project_warns_but_registers(self, cli, codex_home, tmp_path):
        result = _add(
            cli, codex_home, "--project", str(tmp_path / "gone"), "--allow-missing-project"
        )
        assert result.exit_code == 0
        assert "warning" in result.stderr.lower()

    def test_check_interval_must_be_positive(self, cli, codex_home, tmp_path):
        result = _add(cli, codex_home, "--project", str(tmp_path), "--check-interval", "0")
        assert result.exit_code == 2
        assert json.loads(result.stdout)["error"]["code"] == "invalid_check_interval"

    def test_auto_resume_flag(self, cli, codex_home, tmp_path):
        result = _add(cli, codex_home, "--project", str(tmp_path), "--auto-resume")
        assert json.loads(result.stdout)["auto_resume"] is True

    def test_notify_policy_inline_and_file(self, cli, codex_home, tmp_path):
        result = _add(
            cli,
            codex_home,
            "--project",
            str(tmp_path),
            "--notify-policy",
            '{"quota_exhausted": ["console"]}',
        )
        assert result.exit_code == 0
        policy_file = tmp_path / "policy.json"
        policy_file.write_text('{"recovery_failed": ["smtp"]}', encoding="utf-8")
        result = _add(
            cli,
            codex_home,
            "--project",
            str(tmp_path),
            "--notify-policy",
            f"@{policy_file}",
            uuid=UUID_B,
            name="beta",
        )
        assert result.exit_code == 0

    def test_notify_policy_invalid_json(self, cli, codex_home, tmp_path):
        result = _add(cli, codex_home, "--project", str(tmp_path), "--notify-policy", "{oops")
        assert result.exit_code == 2
        assert json.loads(result.stdout)["error"]["code"] == "notify_policy"

    def test_symlinked_codex_home_normalized(self, cli, tmp_path, isolated_home):
        real = tmp_path / "real-home"
        real.mkdir()
        link = tmp_path / "link-home"
        link.symlink_to(real)
        result = _add(cli, link, "--project", str(tmp_path))
        assert result.exit_code == 0
        assert json.loads(result.stdout)["codex_home"] == str(real)

    def test_audit_written(self, cli, codex_home, tmp_path):
        _add(cli, codex_home, "--project", str(tmp_path))
        with _open_state_db() as store:
            rows = store.query("SELECT * FROM audit_log")
        assert len(rows) == 1
        assert rows[0]["actor"] == "cli:add"
        assert rows[0]["category"] == "admin"
        assert rows[0]["session_uuid"] == UUID_A


def _open_state_db() -> Store:
    """按当前测试环境(XDG env)推导 state.db 并打开。"""

    import os

    from codex_autopilot.config import load_config

    config = load_config(env=dict(os.environ)).config
    return Store.open(config.state_db)


class TestList:
    def test_empty(self, cli):
        result = cli("--json", "list")
        assert result.exit_code == 0
        assert json.loads(result.stdout) == []

    def test_json_schema(self, cli, codex_home, tmp_path):
        _add(cli, codex_home, "--project", str(tmp_path))
        result = cli("--json", "list")
        items = json.loads(result.stdout)
        assert len(items) == 1
        assert set(items[0]) == LIST_KEYS
        assert set(items[0]["account"]) == {"codex_home", "label"}

    def test_human_table_columns(self, cli, codex_home, tmp_path):
        _add(cli, codex_home, "--project", str(tmp_path))
        result = cli("list")
        assert result.exit_code == 0
        for column in ("NAME", "UUID", "ACCOUNT", "ENABLED", "AUTO_RESUME", "STATE", "LAST_EVENT"):
            assert column in result.stdout

    def test_global_options_precede_subcommand(self, cli):
        """cli-design §1:全局选项(如 --json)在子命令之前;之后则视为用法错误。"""

        result = cli("list", "--json")
        assert result.exit_code == 2


class TestRemove:
    def test_soft_delete_default(self, cli, codex_home, tmp_path):
        _add(cli, codex_home, "--project", str(tmp_path))
        result = cli("--json", "remove", "alpha")
        assert result.exit_code == 0
        assert json.loads(result.stdout) == {"name": "alpha", "uuid": UUID_A, "purged": False}
        assert json.loads(cli("--json", "list").stdout) == []
        # 行仍在(历史保留)

        with _open_state_db() as store:
            row = store.query_one("SELECT * FROM sessions WHERE uuid = ?", (UUID_A,))
        assert row is not None and row["deleted_at"] is not None

    def test_remove_by_uuid(self, cli, codex_home, tmp_path):
        _add(cli, codex_home, "--project", str(tmp_path))
        assert cli("--json", "remove", UUID_A).exit_code == 0

    def test_remove_not_found(self, cli):
        result = cli("--json", "remove", "ghost")
        assert result.exit_code == 1
        assert json.loads(result.stdout)["error"]["code"] == "not_found"

    def test_purge_requires_yes(self, cli, codex_home, tmp_path):
        _add(cli, codex_home, "--project", str(tmp_path))
        result = cli("--json", "remove", "alpha", "--purge")
        assert result.exit_code == 2
        assert json.loads(result.stdout)["error"]["code"] == "purge_confirmation_required"

    def test_purge_deletes_rows_keeps_audit(self, cli, codex_home, tmp_path):
        _add(cli, codex_home, "--project", str(tmp_path))
        result = cli("--json", "remove", "alpha", "--purge", "--yes")
        assert result.exit_code == 0
        assert json.loads(result.stdout)["purged"] is True

        with _open_state_db() as store:
            assert store.query_one("SELECT * FROM sessions WHERE uuid = ?", (UUID_A,)) is None
            assert store.query_one("SELECT COUNT(*) AS c FROM audit_log")["c"] == 2  # add + remove

    def test_remove_twice_rejected(self, cli, codex_home, tmp_path):
        _add(cli, codex_home, "--project", str(tmp_path))
        cli("--json", "remove", "alpha")
        result = cli("--json", "remove", "alpha")
        assert result.exit_code == 1
        assert json.loads(result.stdout)["error"]["code"] == "already_removed"
