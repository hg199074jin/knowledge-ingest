"""R1.5-A: production shadow scan 组合根 + 唯一 activation gate。

R1.5 前 `insight scan` 是 no-op（只打印一行），直接上 launchd 会得到
"任务在跑、实际什么都没处理" 的假 canary。本模块补上 activation wiring：

- `assert_activation_ready()`：**任何模型调用之前**要求 doctor 的
  `collect_isolation_readiness() == PASS`（唯一 gate 来源，drift 即 fail-closed）；
- `build_production_insight_service()`：把**现有**组件组合成一个
  `InsightService`（不重写第二套 pipeline）：R1 certified model port factory +
  真实 knowledge port + 既有 candidate/value/retrieval/critic/card 组件；
- `run_shadow_scan()`：增量、bounded、restart-safe 的 shadow scan 驱动。

边界（R1.5 §4）：
- Telegram V2 `state.db` 只读（`TelegramEventStore.open_read_only`），不推进 cursor、
  不改 classifier/handoff/digest/budget；
- V3 自身状态只写 `insight/state.db`；
- 不发通知、不自动 ADOPT cognition、不删除 source。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .candidate_filter import HighRecallCandidateFilter
from .cards import DeepInsightCardWriter
from .content_resolver import PartsContentResolver
from .critic import QualityCritic
from .doctor import collect_isolation_readiness
from .knowledge_port import (
    ApprovedCognitionKnowledgePort,
    CommandPersonalKnowledgePort,
    CompositePersonalKnowledgePort,
    PersonalKnowledgePort,
)
from .models import InsightSourceView
from .retrieval import PersonalReasoningRetriever
from .service import InsightService, StageOutcome
from .store import InsightStore
from .value_gate import DeepValueGate

RETRIEVAL_CMD_ENV = "KI_INSIGHT_RETRIEVAL_CMD"


class InsightActivationError(RuntimeError):
    """production scan 的 activation gate 未 PASS（fail-closed，绝不降级）。"""


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def assert_activation_ready(env: Mapping[str, str] | None = None, *,
                            real_home: Path | None = None,
                            version_probe=None):
    """模型调用前的唯一 activation gate；非 PASS 直接抛错。

    刻意不放宽：readiness != PASS 时**不做**任何模型调用、**不**写任何 V3 状态。
    """
    readiness = collect_isolation_readiness(env, real_home=real_home,
                                            version_probe=version_probe)
    if readiness.status != "PASS":
        raise InsightActivationError(
            f"activation_ready={readiness.status} ({readiness.detail})")
    return readiness


def build_production_knowledge_port(store: InsightStore, *,
                                    env: Mapping[str, str] | None = None
                                    ) -> PersonalKnowledgePort:
    """真实 production knowledge port（非 test fake）。

    - `ApprovedCognitionKnowledgePort(store)`：V3 Insight Store 内已批准认知
      （Human Gate ADOPT 产物）——M5 冻结的 V3.0 首版内部闭环来源；
    - `CommandPersonalKnowledgePort(env)`：**仅当**部署配置了
      `KI_INSIGHT_RETRIEVAL_CMD` 时并入（外部检索入口；未配置时它的
      `_command()` 会抛 `ModelNotConfiguredError`，因此不能无条件组合）。

    注意：approved cognition 为空时返回空 records 是**设计允许**的高召回语义
    （knowledge_port 冻结契约「显式空 records 合法」），不是空 retriever 假装
    接通——production personalization 的数据源在 Human Gate ADOPT 之前本就为空。
    """
    env = dict(os.environ) if env is None else dict(env)
    ports: list[PersonalKnowledgePort] = [ApprovedCognitionKnowledgePort(store)]
    if env.get(RETRIEVAL_CMD_ENV, "").strip():
        ports.append(CommandPersonalKnowledgePort(env))
    if len(ports) == 1:
        return ports[0]
    return CompositePersonalKnowledgePort(ports)


def build_production_insight_service(*, store: InsightStore,
                                     insight_root: Path,
                                     env: Mapping[str, str] | None = None,
                                     version_probe=None) -> InsightService:
    """组合唯一 production `InsightService`（全部为既有组件）。

    model port 走 R1.1 factory `build_insight_model_port_from_env()`，因此
    strict isolation / certified absolute command / isolated HOME+CODEX_HOME /
    neutral cwd / stage override 禁止全部自动继承；不得绕过。
    """
    from .model_port import build_insight_model_port_from_env

    assert_activation_ready(env, version_probe=version_probe)
    env = dict(os.environ) if env is None else dict(env)
    model_port = build_insight_model_port_from_env(env)
    knowledge_port = build_production_knowledge_port(store, env=env)
    return InsightService(
        store,
        resolver=PartsContentResolver(),
        retriever=PersonalReasoningRetriever(model_port, knowledge_port),
        candidate_filter=HighRecallCandidateFilter(model_port),
        value_gate=DeepValueGate(model_port),
        model_port=model_port,
        critic=QualityCritic(model_port),
        card_writer=DeepInsightCardWriter(insight_root),
        now=_now_iso,
    )


def is_incremental_candidate(store: InsightStore,
                             view: InsightSourceView) -> bool:
    """该 view 是否值得处理：从未处理，或 source_revision 会发生变化。

    只读判定（不注册、不写），保证同 revision 不重复消耗模型预算。
    """
    row = store.get_source_by_identity(view.provider, view.source_item_id)
    if row is None:
        return True
    return row["content_fingerprint"] != view.content_fingerprint


def select_incremental_views(store: InsightStore, adapter, *,
                             limit: int | None = None
                             ) -> list[InsightSourceView]:
    """V2 只读投影 + V3 增量过滤 + bounded 选择。"""
    selected: list[InsightSourceView] = []
    for view in adapter.iter_views():
        if not is_incremental_candidate(store, view):
            continue
        selected.append(view)
        if limit is not None and len(selected) >= limit:
            break
    return selected


@dataclass
class ScanReport:
    """sanitized 扫描结果（不含 source 正文、model payload 或任何 secret）。"""

    scanned: int = 0
    outcomes: list[tuple[str, str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def record(self, view: InsightSourceView, outcome: StageOutcome) -> None:
        self.scanned += 1
        self.outcomes.append((view.content_kind, outcome.stage,
                              outcome.status))

    def lines(self) -> list[str]:
        rows = [f"  {kind:16} stage={stage:16} status={status}"
                for kind, stage, status in self.outcomes]
        tail = [f"scanned={self.scanned}",
                f"model-stage-outcomes={len(self.outcomes)}",
                f"errors={len(self.errors)}"]
        return rows + tail


def run_shadow_scan(*, store: InsightStore, adapter, insight_root: Path,
                    env: Mapping[str, str] | None = None,
                    real_home: Path | None = None,
                    limit: int | None = None,
                    version_probe=None) -> ScanReport:
    """真实 production shadow scan（V2 只读、V3 增量、bounded）。

    - activation gate 先行：非 PASS 直接抛 `InsightActivationError`；
    - 单个 item 失败不破坏其它已提交状态（逐项 try/except，错误只记录）；
    - model failure 由 service 落 blocked/retryable，绝不记成 reject。
    """
    service = build_production_insight_service(
        store=store, insight_root=insight_root, env=env,
        version_probe=version_probe)
    views = select_incremental_views(store, adapter, limit=limit)
    report = ScanReport()
    for view in views:
        try:
            outcome = service.process(view)
        except Exception as exc:  # noqa: BLE001 — 单项失败不拖垮整批
            report.errors.append(f"{type(exc).__name__}")
            continue
        report.record(view, outcome)
    return report