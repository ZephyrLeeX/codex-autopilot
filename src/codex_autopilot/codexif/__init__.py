"""Codex 集成端口实现(codex interface,architecture.md §2)。

本包内的模块是仅有的允许触碰 Codex 外部界面的位置:
- discovery/rollout:会话 JSONL 文件(只读,永不写入);
- appserver:App Server 只读 JSON-RPC 客户端(方法白名单,queue-safety §7)。
"""
