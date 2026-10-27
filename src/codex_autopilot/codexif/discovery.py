"""rollout 文件发现(P3-T1;session-management §6、phase0-research §2.3)。

目录事实(实证):
- `<codex_home>/sessions/YYYY/MM/DD/rollout-<YYYY-MM-DDThh-mm-ss>-<uuid>.jsonl`
- `<codex_home>/archived_sessions/…`(结构递归扫描)
- 分裂链命名:`rollout-<ts>-<A>_<B>.jsonl`——尾段含多个 UUID 时,链尾(B)是
  该文件自身会话,前面的 UUID 是祖先(压缩/续链产生的引用)。

全部操作只读;文件名中的 UUID 用于定位,内容解析在 rollout.py。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from codex_autopilot.util.identifiers import canonical_uuid

# rollout-<ts>-<uuid>[_<uuid>…].jsonl;ts 的冒号以连字符表示(实测命名)。
_ROLLOUT_NAME_RE = re.compile(
    r"^rollout-(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-"
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"(?:_[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})*)"
    r"\.jsonl$"
)
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_FILENAME_TS_FORMAT = "%Y-%m-%dT%H-%M-%S"


@dataclass(frozen=True)
class SessionFile:
    """一个 rollout 文件的身份信息(全部来自文件名,不读内容)。"""

    path: Path
    uuid: str  # 本文件自身会话(链尾)的 canonical 小写 UUID
    chain: tuple[str, ...]  # 分裂链全部 UUID(含祖先);单文件时 == (uuid,)
    started_at: datetime | None  # 文件名时间戳(本地时区语义,仅用于排序/展示)
    archived: bool  # True = archived_sessions/ 下


def _parse_rollout_name(path: Path) -> tuple[datetime | None, tuple[str, ...]] | None:
    match = _ROLLOUT_NAME_RE.fullmatch(path.name)
    if match is None:
        return None
    started: datetime | None
    try:
        started = datetime.strptime(match.group(1), _FILENAME_TS_FORMAT)
    except ValueError:  # 正则已保证形状,防御非法日期(如 02-31)
        started = None
    chain = tuple(part.lower() for part in match.group(2).split("_"))
    return started, chain


def _scan_dir(root: Path, *, archived: bool) -> list[SessionFile]:
    if not root.is_dir():
        return []
    found: list[SessionFile] = []
    for path in sorted(root.rglob("*.jsonl")):
        if not path.is_file():
            continue
        parsed = _parse_rollout_name(path)
        if parsed is None:
            continue  # session_index.jsonl 等非 rollout 文件
        started, chain = parsed
        found.append(
            SessionFile(
                path=path,
                uuid=chain[-1],
                chain=chain,
                started_at=started,
                archived=archived,
            )
        )
    return found


def discover_session_files(codex_home: Path, *, include_archived: bool = True) -> list[SessionFile]:
    """发现 Codex Home 下全部 rollout 文件(只读)。

    结果按 (started_at, path) 升序;started_at 缺失(命名异常防御分支)排最后。
    目录缺失/不可读不报错——发现层只回答"有什么",语义判断交给上层。
    """

    home = Path(codex_home)
    files = _scan_dir(home / "sessions", archived=False)
    if include_archived:
        files.extend(_scan_dir(home / "archived_sessions", archived=True))
    return sorted(
        files,
        key=lambda f: (f.started_at is None, f.started_at or datetime.min, str(f.path)),
    )


def find_rollout_chain(codex_home: Path, session_uuid: str) -> list[SessionFile]:
    """某会话 UUID 的完整文件链(分裂兼容):文件 chain 中含该 UUID 的全部文件。

    会话 A 分裂出 A_B 后:查询 A 命中 `…-A.jsonl` 与 `…-A_B.jsonl`(续链文件
    的内容延续 A 的历史);查询 B 只命中 `…-A_B.jsonl`。顺序 = discover 的
    时间序,直接顺序读即为事件时间序。未找到返回 []。
    """

    target = canonical_uuid(session_uuid)
    return [f for f in discover_session_files(Path(codex_home)) if target in f.chain]


def find_rollout_for_uuid(codex_home: Path, session_uuid: str) -> SessionFile | None:
    """该 UUID 自身(链尾)的文件;多个时取最新;兼容 chain 尾段校验。"""

    target = canonical_uuid(session_uuid)
    candidates = [f for f in discover_session_files(Path(codex_home)) if f.uuid == target]
    if not candidates:
        return None
    return max(candidates, key=lambda f: (f.started_at or datetime.min, str(f.path)))


def looks_like_session_uuid(value: str) -> bool:
    return bool(_UUID_RE.fullmatch(value)) if isinstance(value, str) else False
