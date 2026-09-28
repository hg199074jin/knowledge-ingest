"""V3 M8: InsightService——状态机编排 + 增量扫描 + restart-safe。

冻结语义（实施方案 Task 8；用户 M6 复核①）：
- 状态链：WAITING_CONTENT → Candidate →（false 终态）→ DeepValue →
  WATCH/ARCHIVE_ONLY/REJECT 终态 | DEEP_READ → Retrieval（持久化
  ContextPack）→ Evidence → Thinker → Critic → Card → Proposal；
- **Context Pack 绑定**：DEEP_READ 后检索一次并 replace_context_refs
  持久化；Evidence/Thinker/Critic/Card 全部消费 store 里这份 pack
  （get_context_pack），禁止静默重检索；
- 增量约束：已 passed 且 input 未变的阶段绝不重跑模型/不扣预算；
  重算仅由 新 SourceView / WAITING→readable / 指纹变新 revision /
  BLOCKED 重试 触发；
- Proposal 入口：passed 且 delta != NONE（passed+NONE/needs_review
  均不产生——用户 M6 复核后冻结）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .candidate_filter import HighRecallCandidateFilter
from .cards import DeepInsightCardWriter, card_id_for
from .content_resolver import (
    ContentPart,
    ContentResolverPort,  # noqa: F401 (port)
)
from .critic import QualityCritic, ThinkingOrchestrator
from .evidence import EvidenceExtractionError, build_evidence_pack
from .model_port import ModelPortError
from .models import EvidencePack, InsightSourceView, PersonalContextRef
from .proposals import InsightCard, build_cognition_proposal
from .store import InsightStore
from .thinker import PersonalThinkingEngine
from .value_gate import DeepValueGate


@dataclass(frozen=True)
class StageOutcome:
    stage: str
    status: str                       # passed / needs_review / blocked / ...
    card_path: str | None = None
    proposal_id: str | None = None
    detail: str = ""


class InsightService:
    """状态机编排：每阶段独立落库、restart-safe、增量。"""

    def __init__(self, store: InsightStore, *, resolver, retriever,
                 candidate_filter: HighRecallCandidateFilter,
                 value_gate: DeepValueGate, model_port, critic: QualityCritic,
                 card_writer: DeepInsightCardWriter, now=None):
        self.store = store
        self.resolver = resolver
        self.retriever = retriever
        self.candidate_filter = candidate_filter
        self.value_gate = value_gate
        self.model_port = model_port
        self.critic = critic
        self.card_writer = card_writer
        self._now = now or (lambda: "2026-09-28T12:00:00+00:00")
        self.orchestrator = ThinkingOrchestrator(
            PersonalThinkingEngine(model_port), critic)

    def process(self, view: InsightSourceView) -> StageOutcome:
        sid = self.store.register_source_view(view)
        revision = self.store.get_source(sid)["source_revision"]

        # 1) 可读性门（WAITING_CONTENT 可恢复，绝不 REJECT）
        content = self.resolver.resolve(view)
        if content is None:
            return StageOutcome("content", "waiting",
                                detail="full text unavailable")

        # 2) Candidate（增量：本 revision 已判定则复用）
        import json
        row = self.store.latest_candidate(sid)
        if row is None or int(row["source_revision"]) != revision:
            decision = self.candidate_filter.evaluate(view)
            self.store.record_candidate(sid, decision)
        row = self.store.latest_candidate(sid)
        payload = json.loads(row["decision_json"])
        if not payload["candidate"]:
            return StageOutcome("candidate", "rejected",
                                detail="candidate=false")

        # 3) Deep Value（增量：同 revision 已判定则复用）
        gate_row = self.store.latest_value_gate(sid)
        if gate_row is None or int(gate_row["source_revision"]) != revision:
            gate_decision = self.value_gate.evaluate(view, payload)
            self.store.record_value_gate(sid, gate_decision)
        gate_row = self.store.latest_value_gate(sid)
        decision_value = json.loads(gate_row["decision_json"])["decision"]
        if decision_value in ("WATCH", "ARCHIVE_ONLY", "REJECT"):
            return StageOutcome("value_gate", decision_value.lower())

        # 4) DEEP_READ → Retrieval（Context Pack 绑定，只检索一次）
        refs = self.store.get_context_refs(sid)
        if not refs:
            refs = self.retriever.retrieve(view, content)
            self.store.replace_context_refs(sid, refs)

        # 5) Evidence（增量：以 run 记录判重）
        if self._stage_done(sid, revision, "evidence"):
            pack = self._load_pack(sid)
        else:
            run_id = self.store.create_run(sid, "evidence", "running")
            try:
                pack = build_evidence_pack(self.model_port, view, content,
                                           refs)
            except ModelPortError as exc:
                self.store.finish_run(run_id, "blocked",
                                      error_code=str(exc)[:80])
                return StageOutcome("evidence", "blocked", detail=str(exc)[:120])
            self.store.finish_run(run_id, "passed")
            self._save_pack(sid, pack)

        # 6) Thinker → Critic（最多一次修订）
        run_id = self.store.create_run(sid, "thinking", "running")
        try:
            outcome = self.orchestrator.run(pack)
        except ModelPortError as exc:
            self.store.finish_run(run_id, "blocked",
                                  error_code=str(exc)[:80])
            return StageOutcome("thinking", "blocked", detail=str(exc)[:120])
        quality = ("passed" if outcome.status == "passed"
                   else "needs_review")
        self.store.finish_run(run_id, quality)

        # 7) Card（同 revision+指纹幂等）
        card_id = card_id_for(view.source_item_id, revision,
                              view.content_fingerprint)
        card_path = self.card_writer.write(
            view=view, pack=pack, thought=outcome.draft,
            quality_status=quality, now=self._now(),
            context_pack_id=self.store.get_context_pack(sid).pack_id)

        # 8) Proposal 入口（passed 且 delta != NONE）
        proposal_id = None
        card = InsightCard(
            card_id=card_id, insight_source_id=sid, source_revision=revision,
            context_pack_id=self.store.get_context_pack(sid).pack_id,
            card_path=str(card_path), quality_status=quality,
            cognition_delta=outcome.draft["cognition_delta"],
            title=outcome.draft["title"],
            own_version=outcome.draft["own_version"],
            mechanism=outcome.draft["mechanism"],
            challenge=outcome.draft["challenge"],
            human_gate_recommendation=outcome.draft[
                "human_gate_recommendation"],
            evidence_refs=tuple(p.source_ref for p in pack.source_parts),
            related_cognition_refs=tuple(
                c["record_id"] for c in outcome.draft["personal_connections"]),
            related_cognitions=tuple(
                {"record_id": c["record_id"], "text": r.text,
                 "connection": c["connection"]}
                for c, r in ((c, next(
                    (rr for rr in refs if rr.record_id == c["record_id"]),
                    None)) for c in outcome.draft["personal_connections"])
                if r is not None))
        proposal = build_cognition_proposal(card)
        if proposal is not None:
            self.store.save_proposal(proposal)
            proposal_id = proposal.proposal_id

        return StageOutcome("card", quality, card_path=str(card_path),
                            proposal_id=proposal_id)

    # ---------- helpers ----------

    def _stage_done(self, sid: str, revision: int, stage: str) -> bool:
        row = self.store._conn.execute(
            """SELECT status FROM insight_runs
               WHERE insight_source_id = ? AND source_revision = ?
               AND stage = ? ORDER BY run_id DESC LIMIT 1""",
            (sid, revision, stage)).fetchone()
        return row is not None and row["status"] == "passed"

    def _save_pack(self, sid: str, pack) -> None:
        """Evidence Pack 全量 JSON 持久化（restart-safe 权威产物）。"""
        payload = {
            "source_claims": list(pack.source_claims),
            "source_evidence": list(pack.source_evidence),
            "source_inferences": list(pack.source_inferences),
            "unknown_variables": list(pack.unknown_variables),
            "verification_flags": list(pack.verification_flags),
            "personal_context": [
                {"record_id": r.record_id,
                 "relation_reason": r.relation_reason,
                 "state": r.state, "source_ref": r.source_ref,
                 "kind": r.kind, "text": r.text, "relation": r.relation}
                for r in pack.personal_context],
            "source_parts": [
                {"part_id": p.part_id, "text": p.text,
                 "source_ref": p.source_ref} for p in pack.source_parts],
        }
        with self.store._conn:
            self.store._conn.execute(
                """INSERT INTO insight_digest_state (key, value)
                   VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (f"evidence:{sid}",
                 json.dumps(payload, ensure_ascii=False)))

    def _load_pack(self, sid: str):
        """从持久化 JSON 恢复 Evidence Pack（restart-safe）。"""
        row = self.store._conn.execute(
            "SELECT value FROM insight_digest_state WHERE key = ?",
            (f"evidence:{sid}",)).fetchone()
        if row is None:
            raise EvidenceExtractionError(
                "no persisted evidence pack for restart")
        data = json.loads(row["value"])
        return EvidencePack(
            source_claims=tuple(data["source_claims"]),
            source_evidence=tuple(data["source_evidence"]),
            source_inferences=tuple(data["source_inferences"]),
            unknown_variables=tuple(data["unknown_variables"]),
            personal_context=tuple(
                PersonalContextRef(record_id=p["record_id"],
                                   relation_reason=p["relation_reason"],
                                   state=p["state"],
                                   source_ref=p.get("source_ref"),
                                   kind=p.get("kind"), text=p.get("text"),
                                   relation=p.get("relation", "DIRECT"))
                for p in data["personal_context"]),
            verification_flags=tuple(data["verification_flags"]),
            source_parts=tuple(
                ContentPart(part_id=p["part_id"], text=p["text"],
                            source_ref=p["source_ref"])
                for p in data["source_parts"]))
