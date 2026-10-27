"""额度服务:App Server 实时与 JSONL 历史的采集、入库与融合视图(P3-T4)。

职责(quota-monitoring §4):
- 每账户至多一次实时查询(失败落 ok=0 错误快照,S-08/S-18);
- 会话 rollout 的 token_count 快照入库(source=jsonl,captured_at=事件时间);
- 账户"当前视图" = 最新 ok=1 快照(按 captured_at,不分来源)——展示用;
  G6 恢复证据只接受新鲜 appserver 快照(P5 守门实现,本层提供数据);
- 来源标注硬要求(FR-04-A2):视图携带 source/captured_at/stale。

本阶段只读监控:不触发任何恢复动作,不写 Codex 任何状态。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codex_autopilot import __version__
from codex_autopilot.codexif.appserver import AppServerClient, AppServerError
from codex_autopilot.codexif.discovery import find_rollout_chain
from codex_autopilot.codexif.rollout import (
    RolloutError,
    latest_rate_limits_sample,
    read_file_events,
)
from codex_autopilot.config import Config
from codex_autopilot.quota.model import (
    AccountQuotaAssessment,
    QuotaSnapshot,
    QuotaThresholds,
    QuotaWindow,
    assess_snapshot,
)
from codex_autopilot.quota.parser import parse_appserver_response, parse_jsonl_rate_limits
from codex_autopilot.store import Store
from codex_autopilot.store.dao import NewQuotaSnapshot, NewQuotaWindow, QuotaSnapshotDao
from codex_autopilot.util.clock import TimeSource

logger = logging.getLogger(__name__)

# AppServerClient 工厂签名(测试注入 Fake;生产用真实子进程客户端)
ClientFactory = Callable[[str, Path], "AppServerClient"]


def _default_client_factory(binary: str, codex_home: Path) -> AppServerClient:
    return AppServerClient(
        binary=binary,
        codex_home=codex_home,
        client_version=__version__,
    )


@dataclass(frozen=True)
class AccountQuotaView:
    """账户当前额度视图(展示/评估共用;FR-04-A2 三元组在 row 内)。"""

    account_id: int
    codex_home: str
    label: str | None
    snapshot: dict[str, Any] | None  # QuotaSnapshotDao.latest_ok 的行(含 windows)
    stale: bool
    assessment: AccountQuotaAssessment


class QuotaService:
    """额度域服务;每实例绑定一个 Store(单线程使用,architecture §4)。"""

    def __init__(
        self,
        store: Store,
        config: Config,
        clock: TimeSource,
        *,
        client_factory: ClientFactory | None = None,
    ):
        self._store = store
        self._config = config
        self._clock = clock
        self._dao = QuotaSnapshotDao(store)
        self._thresholds = QuotaThresholds(
            exhausted_threshold_pct=config.quota.exhausted_threshold_pct,
            hint_min_pct=config.quota.hint_min_pct,
        )
        self._client_factory = client_factory or _default_client_factory

    # -- 账户 -------------------------------------------------------------

    def monitored_accounts(self) -> list[sqlite3.Row]:
        """有未删除会话的账户(去重,按 codex_home 排序)。"""

        return self._store.query(
            "SELECT DISTINCT a.* FROM accounts a"
            " JOIN sessions s ON s.account_id = a.id"
            " WHERE s.deleted_at IS NULL"
            " ORDER BY a.codex_home"
        )

    def all_accounts(self) -> list[sqlite3.Row]:
        return self._store.query("SELECT * FROM accounts ORDER BY codex_home")

    def resolve_account(self, name_or_home: str) -> sqlite3.Row | None:
        """按 label 或 codex_home 精确解析(不做模糊匹配,同 D-CLI-2)。"""

        row = self._store.query_one(
            "SELECT * FROM accounts WHERE label = ? OR codex_home = ? ORDER BY id LIMIT 1",
            (name_or_home, name_or_home),
        )
        return row

    # -- 采集:App Server(实时) -----------------------------------------

    def refresh_appserver(self, account: sqlite3.Row, *, actor: str = "daemon") -> QuotaSnapshot:
        """对该账户执行一次实时查询;结果(含失败)落库并审计,返回快照。"""

        now = self._clock.epoch()
        codex_home = Path(account["codex_home"])
        client = self._client_factory(self._config.codex_binary, codex_home)
        raw_result: dict[str, Any] | None = None
        try:
            with client:
                raw_result = client.read_rate_limits()
            snapshot = parse_appserver_response(
                raw_result, thresholds=self._thresholds, captured_at=now
            )
        except AppServerError as exc:
            snapshot = QuotaSnapshot(source="appserver", captured_at=now, ok=False, error=str(exc))
        self._persist(
            int(account["id"]),
            snapshot,
            raw=json.dumps(raw_result, ensure_ascii=False) if raw_result is not None else None,
        )
        self._store.audit(
            actor=actor,
            category="quota",
            level="info" if snapshot.ok else "warn",
            summary=(
                f"appserver quota query ok ({len(snapshot.windows)} windows)"
                if snapshot.ok
                else f"appserver quota query failed: {snapshot.error}"
            ),
            data={
                "codex_home": account["codex_home"],
                "captured_at": snapshot.captured_at,
                "ok": snapshot.ok,
                "windows": len(snapshot.windows),
            },
        )
        return snapshot

    # -- 采集:rollout JSONL(历史) --------------------------------------

    def ingest_jsonl(self, account: sqlite3.Row) -> QuotaSnapshot | None:
        """账户下全部注册会话的 rollout 链增量解析,每会话最新样本入库。

        返回其中 captured_at 最新的快照;无会话/无文件/无 token_count → None。
        单会话解析异常隔离(R-06 纪律:跳过并告警,不影响其他会话)。
        """

        sessions = self._store.query(
            "SELECT uuid FROM sessions WHERE account_id = ? AND deleted_at IS NULL",
            (account["id"],),
        )
        codex_home = Path(account["codex_home"])
        latest: tuple[int, QuotaSnapshot] | None = None
        for session in sessions:
            sample = self._latest_session_sample(codex_home, session["uuid"])
            if sample is None:
                continue
            payload, captured_at = sample
            snapshot = parse_jsonl_rate_limits(
                payload, thresholds=self._thresholds, captured_at=captured_at
            )
            self._persist(
                int(account["id"]),
                snapshot,
                raw=json.dumps(payload, ensure_ascii=False),
            )
            if latest is None or captured_at > latest[0]:
                latest = (captured_at, snapshot)
        return latest[1] if latest else None

    def _latest_session_sample(
        self, codex_home: Path, session_uuid: str
    ) -> tuple[dict[str, Any], int] | None:
        """会话完整文件链上最新的 token_count 样本(分裂链兼容)。"""

        best: tuple[dict[str, Any], int] | None = None
        for session_file in find_rollout_chain(codex_home, session_uuid):
            try:
                events = read_file_events(session_file.path)
            except RolloutError as exc:
                logger.warning("rollout read failed for %s: %s", session_file.path, exc)
                continue
            sample = latest_rate_limits_sample(events)
            if sample is None:
                continue
            payload, event_ts, _line_no = sample
            try:
                mtime = int(session_file.path.stat().st_mtime)
            except OSError:  # pragma: no cover - 竞态删除
                mtime = 0
            captured_at = event_ts if event_ts is not None else mtime
            if best is None or captured_at > best[1]:
                best = (payload, captured_at)
        return best

    # -- 视图 -------------------------------------------------------------

    def latest_view(self, account: sqlite3.Row, *, source: str | None = None) -> AccountQuotaView:
        """账户当前视图:最新 ok=1 快照 + stale 判定 + 阻塞评估。"""

        snapshot = self._dao.latest_ok(int(account["id"]), source=source)
        if snapshot is None:
            return AccountQuotaView(
                account_id=int(account["id"]),
                codex_home=account["codex_home"],
                label=account["label"],
                snapshot=None,
                stale=True,
                assessment=AccountQuotaAssessment(
                    blocked=True,
                    blocked_kind="unknown",
                    blocking_keys=(),
                    reference_resets_at=None,
                    unknown_reason="no_snapshot",
                ),
            )
        now = self._clock.epoch()
        stale = now - snapshot["captured_at"] > self._config.quota.fresh_ttl_sec
        domain_snapshot = self._to_domain_snapshot(snapshot)
        return AccountQuotaView(
            account_id=int(account["id"]),
            codex_home=account["codex_home"],
            label=account["label"],
            snapshot=snapshot,
            stale=stale,
            assessment=assess_snapshot(domain_snapshot, thresholds=self._thresholds),
        )

    def latest_appserver_error(self, account_id: int) -> dict[str, Any] | None:
        return self._dao.latest_error(account_id, source="appserver")

    # -- 内部 -------------------------------------------------------------

    def _persist(self, account_id: int, snapshot: QuotaSnapshot, *, raw: str | None) -> None:
        record = NewQuotaSnapshot(
            source=snapshot.source,
            captured_at=snapshot.captured_at,
            ok=snapshot.ok,
            error=snapshot.error,
            raw=raw,
            parser_version=snapshot.parser_version,
            windows=tuple(
                NewQuotaWindow(
                    bucket_key=w.bucket_key,
                    window_kind=w.window_kind,
                    kind_inferred=w.kind_inferred,
                    used_percent=w.used_percent,
                    resets_at=w.resets_at,
                    window_minutes=w.window_minutes,
                    is_exhausted=w.is_exhausted,
                )
                for w in snapshot.windows
            ),
        )
        self._dao.insert(account_id, record, ingested_at=self._clock.epoch())

    def _to_domain_snapshot(self, row: dict[str, Any]) -> QuotaSnapshot:
        """DAO 行 → 领域快照(评估复用;limit_id 不入库存,评估不需要)。"""

        return QuotaSnapshot(
            source=row["source"],
            captured_at=row["captured_at"],
            ok=row["ok"],
            error=row["error"],
            windows=tuple(
                QuotaWindow(
                    bucket_key=w["bucket_key"],
                    window_kind=w["window_kind"],
                    kind_inferred=w["kind_inferred"],
                    used_percent=w["used_percent"],
                    resets_at=w["resets_at"],
                    window_minutes=w["window_minutes"],
                    is_exhausted=w["is_exhausted"],
                )
                for w in row["windows"]
            ),
        )
