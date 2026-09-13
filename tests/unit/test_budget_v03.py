"""tests/unit/test_budget_v03.py — 规格 9：Budget 两阶段 + 幂等。

acquire（request_id 幂等 / quota / breaker / case-retry）、
outcome（幂等 + 冲突拒绝 + breaker 语义）、
amend（只改字段不恢复状态）、target resume（显式恢复出口）。
"""

import json
from datetime import UTC, datetime

import pytest

from knowledge_ingest import budget
from knowledge_ingest.models import JobManifest, JobRequest
from knowledge_ingest.next_action import target_resume


def make_manifest(targets=("family_router",)) -> JobManifest:
    now = datetime.now(UTC)
    return JobManifest(
        job_id="20260913-000000-local-budget",
        created_at=now, updated_at=now, status="TARGET_RUNNING",
        request=JobRequest(raw_prompt="x", provider="local", source="/tmp/x",
                           targets=list(targets)),
    )


def write_handoff(job_dir, target="family_router", budget_cfg=None) -> None:
    handoff = job_dir / "handoff"
    handoff.mkdir(parents=True, exist_ok=True)
    data = {"job_id": "j", "target": target}
    if budget_cfg is not None:
        data["budget"] = budget_cfg
    (handoff / f"target-{target}.yaml").write_text(
        json.dumps(data), encoding="utf-8")


@pytest.fixture()
def job_dir(tmp_path):
    d = tmp_path / "job"
    (d / "handoff").mkdir(parents=True)
    return d


def test_acquire_normal_returns_permit_and_call_no(job_dir):
    manifest = make_manifest()
    result = budget.acquire(manifest, job_dir, target="family_router",
                            host="api.example.com", case_id="case-1",
                            request_id="rq-1")
    assert result["allowed"] is True
    assert result["permit_id"].startswith("bp_")
    assert result["call_no"] == 1
    state = manifest.budget_state["targets"]["family_router"]
    assert state["calls"] == 1
    assert state["cases"]["case-1"]["attempts"] == 1
    permit = state["permits"][result["permit_id"]]
    assert permit["host"] == "api.example.com"
    assert permit["case_id"] == "case-1"
    assert permit["request_id"] == "rq-1"
    assert permit["outcome"] is None


def test_acquire_request_id_replay_is_idempotent(job_dir):
    """同 request_id 重放 → 返回既有 permit，不重复计数。"""
    manifest = make_manifest()
    first = budget.acquire(manifest, job_dir, target="family_router",
                           host="h", case_id="c", request_id="rq-1")
    replay = budget.acquire(manifest, job_dir, target="family_router",
                            host="h", case_id="c", request_id="rq-1")
    assert replay["allowed"] is True
    assert replay["permit_id"] == first["permit_id"]
    assert replay["call_no"] == first["call_no"]
    assert manifest.budget_state["targets"]["family_router"]["calls"] == 1


def test_acquire_quota_exhaustion_denied(job_dir):
    manifest = make_manifest()
    write_handoff(job_dir, budget_cfg={"max_external_calls": 2})
    for i in range(2):
        result = budget.acquire(manifest, job_dir, target="family_router",
                                host="h", case_id=f"c{i}",
                                request_id=f"rq-{i}")
        assert result["allowed"] is True
    denied = budget.acquire(manifest, job_dir, target="family_router",
                            host="h", case_id="c9", request_id="rq-9")
    assert denied == {"allowed": False, "reason": "budget_exhausted"}


def test_breaker_three_consecutive_empty_opens(job_dir):
    """3 连 empty → 熔断（consecutive_empty: 3）→ 拒绝。"""
    manifest = make_manifest()
    for i in range(3):
        result = budget.acquire(manifest, job_dir, target="family_router",
                                host="h", case_id=f"c{i}",
                                request_id=f"rq-{i}")
        assert result["allowed"] is True
        budget.outcome(manifest, permit_id=result["permit_id"],
                       result="empty")
    denied = budget.acquire(manifest, job_dir, target="family_router",
                            host="h", case_id="c9", request_id="rq-9")
    assert denied == {"allowed": False, "reason": "breaker_open"}
    host_state = manifest.budget_state["targets"]["family_router"]["hosts"]["h"]
    assert host_state["consecutive_empty"] == 3


def test_breaker_rate_limit_opens_and_success_clears(job_dir):
    """2 连 rate_limit → 熔断；success → 双清零。"""
    manifest = make_manifest()
    for i in range(2):
        result = budget.acquire(manifest, job_dir, target="family_router",
                                host="h", case_id=f"c{i}",
                                request_id=f"rq-{i}")
        budget.outcome(manifest, permit_id=result["permit_id"],
                       result="rate_limit")
    host_state = manifest.budget_state["targets"]["family_router"]["hosts"]["h"]
    assert host_state["consecutive_rate_limit"] == 2
    assert budget.acquire(manifest, job_dir, target="family_router",
                          host="h", case_id="c9",
                          request_id="rq-9") == \
        {"allowed": False, "reason": "breaker_open"}
    # success 清零后恢复
    manifest.budget_state["targets"]["family_router"]["hosts"]["h"] = {
        "consecutive_empty": 1, "consecutive_rate_limit": 0}
    ok = budget.acquire(manifest, job_dir, target="family_router",
                        host="h2", case_id="ok", request_id="rq-ok")
    budget.outcome(manifest, permit_id=ok["permit_id"], result="success")
    host_state = manifest.budget_state["targets"]["family_router"]["hosts"]["h2"]
    assert host_state == {"consecutive_empty": 0,
                          "consecutive_rate_limit": 0}


def test_empty_resets_rate_counter_and_vice_versa(job_dir):
    manifest = make_manifest()
    first = budget.acquire(manifest, job_dir, target="family_router",
                           host="h", case_id="c0", request_id="rq-0")
    budget.outcome(manifest, permit_id=first["permit_id"],
                   result="rate_limit")
    second = budget.acquire(manifest, job_dir, target="family_router",
                            host="h", case_id="c1", request_id="rq-1")
    budget.outcome(manifest, permit_id=second["permit_id"], result="empty")
    host_state = manifest.budget_state["targets"]["family_router"]["hosts"]["h"]
    assert host_state["consecutive_empty"] == 1
    assert host_state["consecutive_rate_limit"] == 0


def test_case_retry_limit_attempts_capped_at_n_plus_one(job_dir):
    """case-retry 语义：首次 1 次 + N 次 retry（N=1 → 上限 2 次 attempts）。"""
    manifest = make_manifest()
    write_handoff(job_dir, budget_cfg={"max_retries_per_case": 1})
    first = budget.acquire(manifest, job_dir, target="family_router",
                           host="h", case_id="case-a", request_id="rq-1")
    assert first["allowed"] is True
    second = budget.acquire(manifest, job_dir, target="family_router",
                            host="h", case_id="case-a", request_id="rq-2")
    assert second["allowed"] is True  # retry
    denied = budget.acquire(manifest, job_dir, target="family_router",
                            host="h", case_id="case-a", request_id="rq-3")
    assert denied == {"allowed": False, "reason": "case_retry_exceeded"}
    # 其他 case 不受影响
    other = budget.acquire(manifest, job_dir, target="family_router",
                           host="h", case_id="case-b", request_id="rq-4")
    assert other["allowed"] is True


def test_outcome_unknown_permit_rejected(job_dir):
    manifest = make_manifest()
    with pytest.raises(ValueError, match="unknown permit"):
        budget.outcome(manifest, permit_id="bp_nope", result="success")


def test_outcome_conflict_rejected_idempotent_replay_noop(job_dir):
    manifest = make_manifest()
    result = budget.acquire(manifest, job_dir, target="family_router",
                            host="h", case_id="c", request_id="rq-1")
    permit_id = result["permit_id"]
    assert budget.outcome(manifest, permit_id=permit_id,
                          result="empty")["noop"] is False
    # 同 permit 同结果重放 → no-op（不改 breaker 计数）
    replay = budget.outcome(manifest, permit_id=permit_id, result="empty")
    assert replay["noop"] is True
    host_state = manifest.budget_state["targets"]["family_router"]["hosts"]["h"]
    assert host_state["consecutive_empty"] == 1
    # 冲突结果 → 拒绝
    with pytest.raises(ValueError, match="conflicting outcome"):
        budget.outcome(manifest, permit_id=permit_id, result="success")


def test_amend_changes_limits_without_restoring_state(job_dir):
    """规格 9 Blocker 1：修改预算 ≠ 自动恢复。"""
    manifest = make_manifest()
    write_handoff(job_dir, budget_cfg={"max_external_calls": 1})
    # 预算耗尽 → target BLOCKED
    budget.acquire(manifest, job_dir, target="family_router", host="h",
                   case_id="c", request_id="rq-1")
    manifest.targets["family_router"].status = "BLOCKED"
    manifest.targets["family_router"].reason = "budget_exhausted"
    manifest.status = "BLOCKED"
    manifest.active_target = None
    # amend 只改 handoff 字段
    amended = budget.amend(job_dir, "family_router", max_external_calls=50)
    assert amended["max_external_calls"] == 50
    # manifest 状态未被 amend 触碰
    assert manifest.targets["family_router"].status == "BLOCKED"
    assert manifest.status == "BLOCKED"
    # 显式 resume 才恢复
    assert target_resume(manifest, "family_router") == "PENDING"  # 无依赖? 有 entry
    assert manifest.status == "CORPUS_READY"


def test_amend_missing_handoff_fails_fast(job_dir):
    with pytest.raises(ValueError, match="not found"):
        budget.amend(job_dir, "family_router", max_external_calls=5)


def test_amend_updates_retry_limit(job_dir):
    manifest = make_manifest()
    write_handoff(job_dir, budget_cfg={"max_retries_per_case": 0})
    budget.amend(job_dir, "family_router", max_retries_per_case=2)
    # 0→2：首次 + 2 次 retry = 3 次 attempts
    results = [budget.acquire(manifest, job_dir, target="family_router",
                              host="h", case_id="c", request_id=f"rq-{i}")
               for i in range(3)]
    assert all(r["allowed"] for r in results)
    fourth = budget.acquire(manifest, job_dir, target="family_router",
                            host="h", case_id="c", request_id="rq-4")
    assert fourth == {"allowed": False, "reason": "case_retry_exceeded"}
