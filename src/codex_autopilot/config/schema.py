"""配置模式:各段 dataclass、默认值与校验规则(P2-T1)。

约定:
- 每个段是一个 frozen dataclass,字段名即 TOML 键(`from` 例外,映射 `from_addr`);
- 未知段/未知键一律拒绝(防拼写错误静默弱化安全参数);
- 校验失败抛 ConfigError,带完整键路径(quota.exhausted_threshold_pct 等);
- 环境变量解析的类型信息来自字段注解(loader.py 使用)。

默认值与 examples/config.toml 注释中的取值一致(T-CLI-01 锚点)。
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, get_type_hints

from codex_autopilot.config.errors import ConfigError

# TOML 键名与 Python 字段名不一致的别名(目前仅 `from` 为关键字)。
_FIELD_ALIASES: dict[type, dict[str, str]] = {
    # {dataclass: {toml_key: field_name}}
}
_TOML_KEY_ALIASES: dict[type, dict[str, str]] = {
    # {dataclass: {field_name: toml_key}}
}


def _register_alias(cls: type, field_name: str, toml_key: str) -> None:
    _FIELD_ALIASES.setdefault(cls, {})[toml_key] = field_name
    _TOML_KEY_ALIASES.setdefault(cls, {})[field_name] = toml_key


def toml_key_of(cls: type, field_name: str) -> str:
    return _TOML_KEY_ALIASES.get(cls, {}).get(field_name, field_name)


def field_name_of(cls: type, toml_key: str) -> str:
    return _FIELD_ALIASES.get(cls, {}).get(toml_key, toml_key)


def section_fields(cls: type) -> list[tuple[str, Any]]:
    """[(field_name, annotation)],annotation 已解析(含 `from __future__` 字符串形式)。"""

    hints = get_type_hints(cls)
    return [(f.name, hints[f.name]) for f in fields(cls)]


def _check_int(section: str, key: str, value: int, *, lo: int, hi: int) -> None:
    if not (lo <= value <= hi):
        raise ConfigError(f"{section}.{key} must be an integer in [{lo}, {hi}], got {value!r}")


def _check_min(section: str, key: str, value: float, minimum: float) -> None:
    if value < minimum:
        raise ConfigError(f"{section}.{key} must be >= {minimum}, got {value!r}")


@dataclass(frozen=True)
class PathsSection:
    state_dir: str | None = None  # None → XDG 默认($XDG_STATE_HOME/codex-autopilot)

    def validate(self) -> None:
        if self.state_dir is not None and (
            not isinstance(self.state_dir, str) or not self.state_dir.strip()
        ):
            raise ConfigError("paths.state_dir must be a non-empty path string")


@dataclass(frozen=True)
class CodexSection:
    binary: str = "codex"
    codex_home: str = "~/.codex"

    def validate(self) -> None:
        if not isinstance(self.binary, str) or not self.binary.strip():
            raise ConfigError("codex.binary must be a non-empty string (name or absolute path)")
        if not isinstance(self.codex_home, str) or not self.codex_home.strip():
            raise ConfigError("codex.codex_home must be a non-empty directory path")


@dataclass(frozen=True)
class SchedulerSection:
    tick_interval: int = 60
    intensive_interval: int = 60

    def validate(self) -> None:
        _check_min("scheduler", "tick_interval", self.tick_interval, 1)
        _check_min("scheduler", "intensive_interval", self.intensive_interval, 1)


@dataclass(frozen=True)
class QuotaSection:
    exhausted_threshold_pct: int = 99
    resume_max_pct: int = 90
    fresh_ttl_sec: int = 600
    regular_probe_interval: int = 300
    hint_min_pct: int = 90

    def validate(self) -> None:
        _check_int("quota", "exhausted_threshold_pct", self.exhausted_threshold_pct, lo=95, hi=100)
        _check_int("quota", "resume_max_pct", self.resume_max_pct, lo=1, hi=100)
        if self.resume_max_pct >= self.exhausted_threshold_pct:
            raise ConfigError(
                f"quota.resume_max_pct ({self.resume_max_pct}) must be lower than "
                f"quota.exhausted_threshold_pct ({self.exhausted_threshold_pct}); "
                "otherwise a window could count as both exhausted and recovered"
            )
        _check_min("quota", "fresh_ttl_sec", self.fresh_ttl_sec, 1)
        _check_min("quota", "regular_probe_interval", self.regular_probe_interval, 1)
        _check_int("quota", "hint_min_pct", self.hint_min_pct, lo=90, hi=98)


@dataclass(frozen=True)
class RecoverySection:
    confirm_timeout_sec: int = 1800
    queue_timeout_sec: int = 45
    missing_grace_sec: int = 86400
    min_delivery_gap_sec: int = 600
    template: str = (
        "Continue the previous task automatically. (codex-autopilot resume #{failure_id})"
    )

    def validate(self) -> None:
        for key in (
            "confirm_timeout_sec",
            "queue_timeout_sec",
            "missing_grace_sec",
            "min_delivery_gap_sec",
        ):
            _check_min("recovery", key, getattr(self, key), 1)
        if not isinstance(self.template, str) or not self.template.strip():
            raise ConfigError("recovery.template must be a non-empty string")


@dataclass(frozen=True)
class SmtpSection:
    enabled: bool = False
    host: str = ""
    port: int = 465
    encryption: str = "ssl"  # ssl | starttls | none
    username: str = ""
    password_env: str = ""  # 与 password_cmd 二选一(examples/config.toml)
    password_cmd: tuple[str, ...] = ()
    from_addr: str = ""
    to: tuple[str, ...] = ()
    retry: int = 3
    retry_backoff_sec: tuple[int, ...] = (30, 120, 480)

    def validate(self) -> None:
        if self.encryption not in ("ssl", "starttls", "none"):
            raise ConfigError(
                f"notify.smtp.encryption must be one of ssl|starttls|none, got {self.encryption!r}"
            )
        _check_int("notify.smtp", "port", self.port, lo=1, hi=65535)
        _check_min("notify.smtp", "retry", self.retry, 0)
        if self.retry > 0:
            if len(self.retry_backoff_sec) != self.retry:
                raise ConfigError(
                    f"notify.smtp.retry_backoff_sec must have exactly retry={self.retry} "
                    f"entries, got {len(self.retry_backoff_sec)}"
                )
            for value in self.retry_backoff_sec:
                _check_min("notify.smtp", "retry_backoff_sec", value, 1)
        if not self.enabled:
            return
        missing = [
            key
            for key, value in (
                ("host", self.host),
                ("username", self.username),
                ("from", self.from_addr),
            )
            if not value.strip()
        ]
        if not self.to:
            missing.append("to")
        if missing:
            raise ConfigError(
                f"notify.smtp is enabled but missing required fields: {', '.join(missing)}"
            )
        have_env, have_cmd = bool(self.password_env.strip()), bool(self.password_cmd)
        if have_env == have_cmd:  # 都无或都有
            raise ConfigError(
                "notify.smtp requires exactly one of password_env / password_cmd "
                "(二选一;凭据绝不写入配置文件)"
            )


_register_alias(SmtpSection, "from_addr", "from")


@dataclass(frozen=True)
class NotifySection:
    console: bool = True
    safety_events_always_on: bool = True
    smtp: SmtpSection = field(default_factory=SmtpSection)

    def validate(self) -> None:
        if not isinstance(self.console, bool):
            raise ConfigError("notify.console must be a boolean")
        if not isinstance(self.safety_events_always_on, bool):
            raise ConfigError("notify.safety_events_always_on must be a boolean")
        self.smtp.validate()


SECTIONS: dict[str, type] = {
    "paths": PathsSection,
    "codex": CodexSection,
    "scheduler": SchedulerSection,
    "quota": QuotaSection,
    "recovery": RecoverySection,
    "notify": NotifySection,
}


@dataclass(frozen=True)
class Config:
    """加载完成、路径已解析的最终配置(immutable)。

    `state_dir` / `codex_home` 为绝对路径;`state_db`、`lock_file` 派生自 state_dir
    (persistence.md §1/§2)。列类型仅在模块导入期使用,子段保持 dataclass 形态。
    """

    state_dir: Path
    codex_binary: str
    codex_home: Path
    scheduler: SchedulerSection
    quota: QuotaSection
    recovery: RecoverySection
    notify: NotifySection

    @property
    def state_db(self) -> Path:
        return self.state_dir / "state.db"

    @property
    def lock_file(self) -> Path:
        return self.state_dir / "daemon.lock"

    def with_overrides(self, **changes: Any) -> Config:
        return replace(self, **changes)
