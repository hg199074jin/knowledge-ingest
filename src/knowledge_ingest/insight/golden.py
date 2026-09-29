"""V3 M9: Golden Set harness——质量基准集运行器与指标报告。

冻结语义（实施方案 Task 9；用户 M6 复核后追加 M9-CAL-01/02）：
- Golden 材料与历史 ChatGPT 高质量回复 = 私人 QA（不入 Git）；
- 指标只做自动可测部分（recall/precision/leakage/critic pass rate），
  人工质量评判单独标记——禁止混淆自动与人工结论；
- 不使用 BLEU/ROUGE/embedding cosine/字数/标题匹配；
- miss 如实记录，禁止"改标签让指标变绿"。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class GoldenCase:
    case_id: str
    category: str
    source_path: str
    expected_candidate: bool = True
    expected_deep_value: str = "DEEP_READ"
    requires_personal_connection: bool = False
    reference_path: str = ""


@dataclass(frozen=True)
class GoldenMetrics:
    valuable_idea_recall: float
    deep_read_precision: float
    false_personal_link_rate: float
    stale_context_leakage: float
    critic_pass_rate: float
    human_quality_pending: int
    total_cases: int
    misses: list = field(default_factory=list)


def load_manifest(manifest_path: Path) -> list[GoldenCase]:
    data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    cases = []
    for c in data.get("cases", []):
        cases.append(GoldenCase(
            case_id=c["case_id"], category=c["category"],
            source_path=c["source_path"],
            expected_candidate=c.get("expected", {}).get("candidate", True),
            expected_deep_value=c.get("expected", {}).get("deep_value",
                                                           "DEEP_READ"),
            requires_personal_connection=c.get("expected", {}).get(
                "requires_personal_connection", False),
            reference_path=c.get("reference_path", "")))
    return cases


def compute_metrics(results: list[dict]) -> GoldenMetrics:
    """从 Golden 运行结果列表计算自动指标。

    每个 result 含：case_id / expected_candidate / actual_candidate /
    expected_deep_value / actual_deep_value / has_personal_connection /
    requires_personal_connection / stale_context_detected /
    critic_status / human_pending。
    """
    total = len(results)
    if total == 0:
        return GoldenMetrics(1.0, 1.0, 0.0, 0.0, 1.0, 0, 0)
    misses = []
    recall_hits = 0
    precision_hits = 0
    false_links = 0
    stale_leaks = 0
    critic_passes = 0
    pending = 0
    for r in results:
        if r["expected_candidate"] and r["actual_candidate"]:
            recall_hits += 1
        elif r["expected_candidate"] and not r["actual_candidate"]:
            misses.append(f"{r['case_id']}: expected candidate, got skip")
        if (r["expected_deep_value"] == "DEEP_READ"
                and r.get("actual_deep_value") == "DEEP_READ"):
            precision_hits += 1
        if (r.get("requires_personal_connection", False)
                and r.get("has_personal_connection", False)
                and not r.get("is_hard_link", False)):
            pass  # good
        elif r.get("has_personal_connection", False) and r.get(
                "is_hard_link", False):
            false_links += 1
            misses.append(f"{r['case_id']}: hard link detected")
        if r.get("stale_context_detected", False):
            stale_leaks += 1
        if r.get("critic_status") in ("passed", "needs_review"):
            critic_passes += 1
        if r.get("human_pending", False):
            pending += 1
    expected_candidates = sum(1 for r in results if r["expected_candidate"])
    deep_reads = sum(1 for r in results
                     if r["expected_deep_value"] == "DEEP_READ")
    return GoldenMetrics(
        valuable_idea_recall=recall_hits / max(expected_candidates, 1),
        deep_read_precision=precision_hits / max(deep_reads, 1),
        false_personal_link_rate=false_links / max(total, 1),
        stale_context_leakage=stale_leaks / max(total, 1),
        critic_pass_rate=critic_passes / max(total, 1),
        human_quality_pending=pending,
        total_cases=total,
        misses=misses)
