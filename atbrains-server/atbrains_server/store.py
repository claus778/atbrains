"""atbrains 服务端存储引擎组合入口（MariaDB 长期 + Redis 短期 + Qdrant 向量 + 知识图谱）。"""
from __future__ import annotations

from .engine.base import BaseMixin
from .engine.curation import CurationMixin
from .engine.distill import DistillMixin
from .engine.graph import GraphMixin
from .engine.knowledge import KnowledgeMixin
from .engine.maintenance import MaintenanceMixin
from .engine.memories import MemoriesMixin
from .engine.net import NetMixin
from .engine.recall import RecallMixin
from .engine.transcript import TranscriptMixin
from .engine.vectors import VectorsMixin


class AtbrainsStore(
    BaseMixin,
    NetMixin,
    MemoriesMixin,
    KnowledgeMixin,
    TranscriptMixin,
    RecallMixin,
    VectorsMixin,
    CurationMixin,
    GraphMixin,
    MaintenanceMixin,
    DistillMixin,
):
    """Redis + MariaDB + Qdrant 三层记忆存储引擎（单引擎共享连接与缓存，按 RequestContext 隔离调用方身份）。"""


__all__ = ["AtbrainsStore"]
