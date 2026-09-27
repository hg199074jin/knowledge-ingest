"""V3 M1: insight data-root path convention.

Insight root follows the pipeline root（本机审阅意见 A7）：不引入新的
环境变量、不复用 KI_TELEGRAM_ORICO_ROOT；生产 ORICO 可用性由
`insight doctor` 检查 root 可写（M8）。测试以 tmp config 天然 hermetic。
"""

from __future__ import annotations

from pathlib import Path

from knowledge_ingest.config import AppConfig


def insight_root(config: AppConfig) -> Path:
    """`<pipeline_root>/insight/`——cards/evidence/proposals/knowledge 等均在其下。"""
    return Path(config.pipeline_root) / "insight"
