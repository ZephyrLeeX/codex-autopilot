"""配置错误:非法值、未知键、坏 TOML、非法 XDG 变量等(启动即拒,exit 20)。"""

from __future__ import annotations


class ConfigError(Exception):
    """配置无效。message 面向用户,包含定位信息(键路径/文件/环境变量名)。"""
