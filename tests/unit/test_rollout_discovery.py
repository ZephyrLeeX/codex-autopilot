"""P3-T1 测试:rollout 文件发现(T-PARSE:命名/分裂链/archived)。"""

from __future__ import annotations

from pathlib import Path

import pytest

from codex_autopilot.codexif.discovery import (
    SessionFile,
    discover_session_files,
    find_rollout_chain,
    find_rollout_for_uuid,
)

UUID_A = "01234567-89ab-cdef-0123-456789abcdef"
UUID_B = "fedcba98-7654-3210-fedc-ba9876543210"


def _make(codex_home: Path, name: str, *, archived: bool = False) -> Path:
    root = codex_home / ("archived_sessions" if archived else "sessions") / "2026/01/01"
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_text('{"type":"session_meta","payload":{}}\n', encoding="utf-8")
    return path


class TestDiscovery:
    def test_finds_sessions_tree_files(self, tmp_path):
        home = tmp_path / "codex"
        _make(home, f"rollout-2026-01-01T00-00-00-{UUID_A}.jsonl")
        _make(home, f"rollout-2026-01-02T10-20-30-{UUID_B}.jsonl", archived=False)
        files = discover_session_files(home)
        assert [f.uuid for f in files] == [UUID_A, UUID_B]  # 按时间升序
        assert all(not f.archived for f in files)

    def test_ignores_non_rollout_jsonl(self, tmp_path):
        home = tmp_path / "codex"
        (home / "sessions" / "2026/01/01").mkdir(parents=True)
        (home / "sessions" / "2026/01/01" / "session_index.jsonl").write_text("{}\n")
        (home / "random.jsonl").write_text("{}\n")
        assert discover_session_files(home) == []

    def test_archived_files_marked(self, tmp_path):
        home = tmp_path / "codex"
        _make(home, f"rollout-2026-01-01T00-00-00-{UUID_A}.jsonl", archived=True)
        files = discover_session_files(home)
        assert len(files) == 1 and files[0].archived
        assert discover_session_files(home, include_archived=False) == []

    def test_missing_directories_return_empty(self, tmp_path):
        assert discover_session_files(tmp_path / "empty-home") == []

    def test_started_at_parsed_from_filename(self, tmp_path):
        home = tmp_path / "codex"
        _make(home, f"rollout-2026-03-04T05-06-07-{UUID_A}.jsonl")
        (file,) = discover_session_files(home)
        assert file.started_at is not None
        assert (file.started_at.year, file.started_at.month, file.started_at.day) == (2026, 3, 4)

    def test_invalid_date_in_name_tolerated(self, tmp_path):
        home = tmp_path / "codex"
        _make(home, f"rollout-2026-13-40T25-99-99-{UUID_A}.jsonl")
        (file,) = discover_session_files(home)
        assert file.started_at is None  # 不因命名异常崩溃
        assert file.uuid == UUID_A

    def test_uppercase_uuid_normalized(self, tmp_path):
        home = tmp_path / "codex"
        _make(home, f"rollout-2026-01-01T00-00-00-{UUID_A.upper()}.jsonl")
        (file,) = discover_session_files(home)
        assert file.uuid == UUID_A


class TestSplitChain:
    def test_split_chain_uuids(self, tmp_path):
        home = tmp_path / "codex"
        _make(home, f"rollout-2026-01-01T00-00-00-{UUID_A}_{UUID_B}.jsonl")
        (file,) = discover_session_files(home)
        assert file.uuid == UUID_B  # 链尾是文件自身会话
        assert file.chain == (UUID_A, UUID_B)

    def test_find_for_uuid_matches_own_only(self, tmp_path):
        home = tmp_path / "codex"
        _make(home, f"rollout-2026-01-01T00-00-00-{UUID_A}.jsonl")
        _make(home, f"rollout-2026-01-02T00-00-00-{UUID_A}_{UUID_B}.jsonl")
        found = find_rollout_for_uuid(home, UUID_B)
        assert found is not None and found.uuid == UUID_B
        # A 的自身文件仍指向 A(不是续链文件)
        found_a = find_rollout_for_uuid(home, UUID_A)
        assert found_a is not None and found_a.uuid == UUID_A

    def test_find_chain_includes_continuation(self, tmp_path):
        """分裂会话:A 的完整链 = 原文件 + 续链文件(时间序)。"""

        home = tmp_path / "codex"
        _make(home, f"rollout-2026-01-01T00-00-00-{UUID_A}.jsonl")
        _make(home, f"rollout-2026-01-02T00-00-00-{UUID_A}_{UUID_B}.jsonl")
        chain = find_rollout_chain(home, UUID_A)
        assert [f.uuid for f in chain] == [UUID_A, UUID_B]
        chain_b = find_rollout_chain(home, UUID_B)
        assert [f.uuid for f in chain_b] == [UUID_B]

    def test_find_chain_archived_after_live(self, tmp_path):
        """归档 + 新文件:链包含两者(时间序);取"自身文件"时选最新。"""

        home = tmp_path / "codex"
        archived = _make(home, f"rollout-2026-01-01T00-00-00-{UUID_A}.jsonl", archived=True)
        live = _make(home, f"rollout-2026-01-02T00-00-00-{UUID_A}.jsonl")
        chain = find_rollout_chain(home, UUID_A)
        assert [f.path for f in chain] == [archived, live]  # 按时间升序,归档在前
        found = find_rollout_for_uuid(home, UUID_A)
        assert found is not None and found.path == live  # 最新文件代表该会话


class TestSessionFileModel:
    def test_frozen_and_sortable(self, tmp_path):
        path = _make(tmp_path, f"rollout-2026-01-01T00-00-00-{UUID_A}.jsonl")
        file = SessionFile(path=path, uuid=UUID_A, chain=(UUID_A,), started_at=None, archived=False)
        assert file.uuid == UUID_A
        import dataclasses

        with pytest.raises(dataclasses.FrozenInstanceError):  # frozen 不可变
            file.uuid = "x"  # type: ignore[misc]
