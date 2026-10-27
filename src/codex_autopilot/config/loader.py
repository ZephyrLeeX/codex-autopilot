"""配置加载与合并(P2-T1)。

优先级(高到低):CLI 参数 > 环境变量 `CODEX_AUTOPILOT_<SECTION>__<KEY>`
> 配置文件(TOML)> 内置默认值(cli-design.md §5)。

- 配置文件缺省路径遵循 XDG(deployment.md §2);文件不存在时静默用默认值;
  `--config` 显式指定的文件不存在则报错(用户意图明确,必须早失败);
- 未知段/未知键在 TOML 中一律拒绝(拼写错误不得静默弱化安全参数);
- 路径段(`state_dir`、`codex_home`)展开 `~` 后必须为绝对路径;
- 校验失败抛 ConfigError(CLI 层映射为退出码 20)。
"""

from __future__ import annotations

import dataclasses
import tomllib
import types
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints

from codex_autopilot.config.errors import ConfigError
from codex_autopilot.config.schema import (
    SECTIONS,
    Config,
    PathsSection,
    field_name_of,
    section_fields,
    toml_key_of,
)
from codex_autopilot.util.paths import (
    XdgPathError,
    default_config_file,
    default_state_dir,
    expand_path,
)

ENV_PREFIX = "CODEX_AUTOPILOT_"
_ENV_SEP = "__"

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


@dataclass(frozen=True)
class LoadResult:
    config: Config
    config_file_used: Path | None  # 实际加载的文件;未找到/未指定时为 None


def _unwrap_optional(annotation: Any) -> Any:
    if get_origin(annotation) is types.UnionType:
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


def _coerce_toml_value(section: str, key: str, annotation: Any, value: Any) -> Any:
    """TOML 值适配字段类型(仅 list→tuple);类型不符直接报错。"""

    annotation = _unwrap_optional(annotation)
    origin = get_origin(annotation)
    if origin is tuple:
        if not isinstance(value, list):
            raise ConfigError(f"{section}.{key} must be a list, got {type(value).__name__}")
        return tuple(value)
    if annotation is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{section}.{key} must be an integer, got {value!r}")
    elif annotation is str:
        if not isinstance(value, str):
            raise ConfigError(f"{section}.{key} must be a string, got {value!r}")
    elif annotation is bool and not isinstance(value, bool):
        raise ConfigError(f"{section}.{key} must be a boolean, got {value!r}")
    return value


def _coerce_scalar(section: str, key: str, annotation: Any, raw: str, *, origin: str) -> Any:
    """把环境变量/CLI 覆盖的字符串按字段类型解析。"""

    annotation = _unwrap_optional(annotation)
    origin_kind = get_origin(annotation)
    if origin_kind is tuple:
        (element, *rest) = get_args(annotation) or (str,)
        if rest and rest[0] is not Ellipsis:
            raise ConfigError(f"unsupported tuple annotation for {section}.{key}")
        items: list[Any] = []
        for piece in raw.split(","):
            piece = piece.strip()
            if not piece:
                raise ConfigError(f"{origin} for {section}.{key}: empty list item in {raw!r}")
            items.append(_coerce_scalar(section, key, element, piece, origin=origin))
        return tuple(items)
    if annotation is bool:
        lowered = raw.strip().lower()
        if lowered in _TRUE_VALUES:
            return True
        if lowered in _FALSE_VALUES:
            return False
        raise ConfigError(f"{origin} for {section}.{key}: expected boolean, got {raw!r}")
    if annotation is int:
        try:
            return int(raw.strip())
        except ValueError as exc:
            raise ConfigError(
                f"{origin} for {section}.{key}: expected integer, got {raw!r}"
            ) from exc
    if annotation is str:
        return raw
    raise ConfigError(f"unsupported config type for {section}.{key}: {annotation!r}")


def _merge_toml_section(section_name: str, cls: type, current: Any, data: Any) -> Any:
    if not isinstance(data, dict):
        raise ConfigError(f"[{section_name}] must be a TOML table, got {type(data).__name__}")
    known = {toml_key_of(cls, fname) for fname, _ in section_fields(cls)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(
            f"unknown key(s) in [{section_name}]: {', '.join(unknown)}; "
            f"valid keys: {', '.join(sorted(known))}"
        )
    changes = {}
    hints = get_type_hints(cls)
    for toml_k, value in data.items():
        fname = field_name_of(cls, toml_k)
        annotation = _unwrap_optional(hints[fname])
        if dataclasses.is_dataclass(annotation):
            nested_current = getattr(current, fname)
            changes[fname] = _merge_toml_section(
                f"{section_name}.{toml_k}", annotation, nested_current, value
            )
        else:
            changes[fname] = _coerce_toml_value(section_name, toml_k, annotation, value)
    return dataclasses.replace(current, **changes) if changes else current


def _apply_env(section_name: str, cls: type, current: Any, env: Mapping[str, str]) -> Any:
    prefix = ENV_PREFIX + section_name.upper().replace(".", _ENV_SEP) + _ENV_SEP
    changes = {}
    for fname, annotation in section_fields(cls):
        annotation = _unwrap_optional(annotation)
        toml_k = toml_key_of(cls, fname)
        env_name = prefix + toml_k.upper()
        if dataclasses.is_dataclass(annotation):
            nested = getattr(current, fname)
            updated = _apply_env(f"{section_name}.{toml_k}", annotation, nested, env)
            if updated is not nested:
                changes[fname] = updated
            continue
        raw = env.get(env_name)
        if raw is None or raw == "":
            continue
        changes[fname] = _coerce_scalar(
            section_name, toml_k, annotation, raw, origin=f"environment variable {env_name}"
        )
    return dataclasses.replace(current, **changes) if changes else current


def _apply_cli_overrides(
    section_instances: dict[str, Any], cli_overrides: Mapping[str, Any]
) -> dict[str, Any]:
    """CLI 覆盖以「TOML 键路径」为键,如 {"paths.state_dir": "/tmp/x"}。"""

    result = dict(section_instances)
    for key_path, raw in cli_overrides.items():
        if raw is None:
            continue
        parts = key_path.split(".")
        if len(parts) == 2 and parts[0] in SECTIONS:
            section_name, toml_k = parts
            cls = SECTIONS[section_name]
            hints = get_type_hints(cls)
            fname = field_name_of(cls, toml_k)
            annotation = _unwrap_optional(hints[fname])
            value = _coerce_scalar(
                section_name, toml_k, annotation, str(raw), origin=f"CLI option --{toml_k}"
            )
            result[section_name] = dataclasses.replace(result[section_name], **{fname: value})
        else:
            raise ConfigError(f"unsupported CLI config override key: {key_path!r}")
    return result


def load_config(
    *,
    env: Mapping[str, str],
    config_file: Path | None = None,
    cli_overrides: Mapping[str, Any] | None = None,
) -> LoadResult:
    """加载并合并配置。

    `config_file`:显式指定的配置文件(`--config`);None 时使用 XDG 默认路径,
    且默认路径不存在不视为错误。
    """

    explicit = config_file is not None
    file = config_file if explicit else default_config_file(env)
    file_used: Path | None = None
    file_data: dict[str, Any] = {}
    if file is not None and file.is_file():
        try:
            with open(file, "rb") as fh:
                file_data = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"cannot parse config file {file}: {exc}") from exc
        file_used = file
    elif explicit:
        raise ConfigError(f"config file not found: {file}")

    unknown_sections = sorted(set(file_data) - set(SECTIONS))
    if unknown_sections:
        raise ConfigError(
            f"unknown config section(s) {', '.join(unknown_sections)}; "
            f"valid sections: {', '.join(SECTIONS)}"
        )

    instances: dict[str, Any] = {name: cls() for name, cls in SECTIONS.items()}
    for name in SECTIONS:
        if name in file_data:
            instances[name] = _merge_toml_section(
                name, SECTIONS[name], instances[name], file_data[name]
            )
    for name in SECTIONS:
        instances[name] = _apply_env(name, SECTIONS[name], instances[name], env)
    instances = _apply_cli_overrides(instances, cli_overrides or {})

    for instance in instances.values():
        instance.validate()

    paths: PathsSection = instances["paths"]
    raw_state_dir = paths.state_dir if paths.state_dir else None
    try:
        state_dir = expand_path(raw_state_dir) if raw_state_dir else default_state_dir(env)
        codex_home = expand_path(instances["codex"].codex_home)
    except XdgPathError as exc:
        raise ConfigError(str(exc)) from exc
    if not state_dir.is_absolute():  # pragma: no cover - expand_path 已保证
        raise ConfigError(
            f"paths.state_dir must resolve to an absolute path, got {raw_state_dir!r}"
        )
    if not codex_home.is_absolute():  # pragma: no cover - expand_path 已保证
        raise ConfigError(
            f"codex.codex_home must resolve to an absolute path, got "
            f"{instances['codex'].codex_home!r}"
        )

    config = Config(
        state_dir=state_dir,
        codex_binary=instances["codex"].binary,
        codex_home=codex_home,
        scheduler=instances["scheduler"],
        quota=instances["quota"],
        recovery=instances["recovery"],
        notify=instances["notify"],
    )
    return LoadResult(config=config, config_file_used=file_used)
