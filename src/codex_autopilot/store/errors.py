"""Store 层错误类型。"""

from __future__ import annotations


class StoreError(Exception):
    """数据库打开/迁移/事务等基础设施错误(CLI 映射退出码 20)。"""


class StoreNotOpenError(StoreError):
    """连接已关闭后继续使用。"""


class NestedTransactionError(StoreError):
    """尝试在事务内再开事务(单线程模型禁止嵌套,防隐式提交)。"""


class ConstraintViolationError(StoreError):
    """唯一约束/外键冲突;`key` 给出稳定机器码(duplicate_uuid/duplicate_name/fk…)。"""

    def __init__(self, message: str, *, key: str):
        super().__init__(message)
        self.key = key
