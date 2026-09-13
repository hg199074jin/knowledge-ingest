"""tests/unit/test_gate_preauthorize.py — 规格 10：Gate 预授权两阶段。

preauthorize 发 grant → resolve 引用 grant（target/gate 匹配 + 未使用校验）；
knowledge gate 拒绝预授权（registry 白名单）；personal 全门 = live。
"""

from datetime import datetime, timezone

import pytest

from knowledge_ingest.models import JobManifest, JobRequest
from knowledge_ingest.next_action import (
    gate_enter,
    gate_preauthorize,
    gate_resolve,
)


def make_manifest(targets=("cangjie",), status="CORPUS_READY") -> JobManifest:
    return JobManifest(
        job_id="20260913-000000-local-gate",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        status=status,
        request=JobRequest(raw_prompt="x", provider="local", source="/tmp/x",
                           targets=list(targets)),
    )


def _running_with_open_gate(targets=("cangjie",), gate="stage5_install_location"):
    manifest = make_manifest(targets=targets, status="CORPUS_READY")
    gate_enter_flow = manifest
    # 启动 cangjie（需要先启动链上第一个 target）
    from knowledge_ingest.next_action import target_start
    target_start(gate_enter_flow, "cangjie")
    gate_enter(gate_enter_flow, "cangjie", gate)
    return gate_enter_flow


def test_preauthorize_creates_grant():
    manifest = make_manifest()
    grant = gate_preauthorize(manifest, "cangjie", "stage5_install_location",
                              "/Volumes/ORICO/Skills")
    assert grant["grant_id"].startswith("pa_")
    assert grant["target"] == "cangjie"
    assert grant["gate"] == "stage5_install_location"
    assert grant["value"] == "/Volumes/ORICO/Skills"
    assert grant["granted_at"] is not None
    assert manifest.preauthorizations == [grant]
    assert any(h.get("action") == "preauthorize"
               for h in manifest.gate_history)


def test_preauthorize_then_resolve_uses_grant():
    manifest = _running_with_open_gate()
    grant = gate_preauthorize(manifest, "cangjie", "stage5_install_location",
                              "/Volumes/ORICO/Skills")
    gate_resolve(manifest, "cangjie", "stage5_install_location", "confirmed",
                 preauthorization=grant["grant_id"])
    # gate 关闭且 target 恢复 RUNNING
    assert manifest.targets["cangjie"].status == "RUNNING"
    assert manifest.status == "TARGET_RUNNING"
    entry = manifest.gate_history[-1]
    assert entry["approval_mode"] == "preauthorized"
    assert entry["grant_id"] == grant["grant_id"]
    assert entry["decision"] == "confirmed"
    # grant 标记已使用
    assert manifest.preauthorizations[0]["used_at"] is not None


def test_resolve_with_unknown_grant_rejected():
    manifest = _running_with_open_gate()
    with pytest.raises(ValueError, match="unknown preauthorization grant"):
        gate_resolve(manifest, "cangjie", "stage5_install_location",
                     "confirmed", preauthorization="pa_does_not_exist")


def test_resolve_grant_target_gate_mismatch_rejected():
    manifest = _running_with_open_gate()
    # grant 发给 family_router/cost_budget_confirmed，与当前打开的
    # cangjie/stage5_install_location 不匹配
    grant = gate_preauthorize(manifest, "family_router",
                              "cost_budget_confirmed", "50 CNY")
    with pytest.raises(ValueError, match="does not match"):
        gate_resolve(manifest, "cangjie", "stage5_install_location",
                     "confirmed", preauthorization=grant["grant_id"])


def test_grant_cannot_be_used_twice():
    manifest = make_manifest(targets=("cangjie", "family_router"))
    grant = gate_preauthorize(manifest, "cangjie", "stage5_install_location",
                              "/x")
    # 第一次使用
    from knowledge_ingest.next_action import target_start
    target_start(manifest, "cangjie")
    gate_enter(manifest, "cangjie", "stage5_install_location")
    gate_resolve(manifest, "cangjie", "stage5_install_location", "confirmed",
                 preauthorization=grant["grant_id"])
    # 第二次开同一个 gate 再用同一 grant → 拒绝
    gate_enter(manifest, "cangjie", "stage5_install_location")
    with pytest.raises(ValueError, match="already used"):
        gate_resolve(manifest, "cangjie", "stage5_install_location",
                     "confirmed", preauthorization=grant["grant_id"])


def test_preauthorize_with_rejected_decision_rejected():
    manifest = _running_with_open_gate()
    grant = gate_preauthorize(manifest, "cangjie", "stage5_install_location",
                              "/x")
    with pytest.raises(ValueError, match="requires decision=confirmed"):
        gate_resolve(manifest, "cangjie", "stage5_install_location",
                     "rejected", preauthorization=grant["grant_id"])


def test_knowledge_gate_not_in_whitelist_rejects_preauthorization():
    """cangjie 只有 stage5_install_location 可预授权；其余门（如
    stage0_overview）= live。"""
    manifest = make_manifest()
    with pytest.raises(ValueError, match="does not accept"):
        gate_preauthorize(manifest, "cangjie", "stage0_overview", "any")


def test_personal_gates_reject_preauthorization():
    """personal 全部门（含 installation_approved）= live。"""
    manifest = make_manifest(targets=("personal",))
    with pytest.raises(ValueError, match="does not accept"):
        gate_preauthorize(manifest, "personal", "installation_approved",
                          "approved")
    with pytest.raises(ValueError, match="does not accept"):
        gate_preauthorize(manifest, "personal", "inventory_reviewed", "y")


def test_family_router_cost_budget_is_preauthorizable():
    from knowledge_ingest.targets import REGISTRY

    assert "cost_budget_confirmed" \
        in REGISTRY["family_router"].preauthorizable_gates
    manifest = make_manifest(targets=("family_router",))
    grant = gate_preauthorize(manifest, "family_router",
                              "cost_budget_confirmed", "50 CNY")
    assert grant["gate"] == "cost_budget_confirmed"


def test_live_resolve_without_grant_unchanged():
    """未提供 --preauthorization 的 resolve 行为不变（= live）。"""
    manifest = _running_with_open_gate()
    gate_resolve(manifest, "cangjie", "stage5_install_location", "confirmed")
    assert manifest.targets["cangjie"].status == "RUNNING"
    entry = manifest.gate_history[-1]
    assert "approval_mode" not in entry
    assert "grant_id" not in entry
    assert manifest.preauthorizations == []
