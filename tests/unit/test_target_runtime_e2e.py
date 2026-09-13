"""tests/unit/test_target_runtime_e2e.py — v0.3 Part C e2e。

- 假 target e2e：registry 注册 test_target，跑 start/gate/complete 全链
- E9：COMPLETED Job amend --add-target family_router（重入/依赖判定）
- E14 依赖传播两例：reject cangjie → family_router SKIPPED(dependency_skipped)
  → personal COMPLETED → Job COMPLETED；cangjie FAILED → PARTIAL
"""

from argparse import Namespace
from pathlib import Path

import pytest

import knowledge_ingest.targets as targets_module
from knowledge_ingest.cli import (
    _build_parser,
    _cmd_budget,
    _cmd_job_amend,
    _cmd_distill_prepare,
    _cmd_gate,
    _cmd_next,
    _cmd_target_complete,
    _cmd_target_resume,
    _cmd_target_start,
)
from knowledge_ingest.manifest_store import ManifestStore
from knowledge_ingest.models import JobRequest
from knowledge_ingest.next_action import (
    gate_enter,
    gate_resolve,
    next_action,
    settle,
    target_start,
)
from knowledge_ingest.targets import (
    REGISTRY,
    TargetRuntime,
    register,
    target_choices,
)

from .test_preprocess_cli import make_config


@pytest.fixture()
def test_target():
    """注册一个假 target（e2e 用），测试结束后清理。"""
    runtime = TargetRuntime(
        name="test_target", display_name="Test Target",
        invoke_key="invoke_test_target", skill_config_key="cangjie",
    )
    register(runtime)
    yield runtime
    REGISTRY.pop("test_target", None)


@pytest.fixture()
def env(tmp_path: Path):
    config = make_config(tmp_path)
    store = ManifestStore(jobs_root=config.pipeline_root / "jobs")
    return config, store


def _make_job(store: ManifestStore, targets) -> str:
    manifest = store.create(JobRequest(
        raw_prompt="x", provider="local", source=str(tmp_source(targets)),
        targets=list(targets)))
    with store.edit(manifest.job_id) as m:
        m.status = "CORPUS_READY"
    return manifest.job_id


def tmp_source(targets) -> str:
    return f"/tmp/source-{'-'.join(targets)}"


# ---------- 假 target e2e：start → gate → complete 全链 ----------


def test_fake_target_full_chain(env, test_target):
    config, store = env
    job_id = _make_job(store, ["test_target"])
    loaded = store.load(job_id)
    assert list(loaded.targets) == ["test_target"]
    action = next_action(loaded)
    assert action["next_action"] == "invoke_test_target"

    rc = _cmd_target_start(config, Namespace(job_id=job_id,
                                             target="test_target"))
    assert rc == 0
    loaded = store.load(job_id)
    assert loaded.status == "TARGET_RUNNING"
    assert loaded.active_target == "test_target"
    assert loaded.targets["test_target"].status == "RUNNING"

    rc = _cmd_gate(config, Namespace(
        job_id=job_id, gate_command="enter", target="test_target",
        name="approval", preauthorization=None))
    assert rc == 0
    loaded = store.load(job_id)
    assert loaded.status == "WAITING_USER"
    assert next_action(loaded)["next_action"] == "ask_user"

    rc = _cmd_gate(config, Namespace(
        job_id=job_id, gate_command="resolve", target="test_target",
        name="approval", decision="confirmed", preauthorization=None))
    assert rc == 0

    output = store.job_dir(job_id) / "handoff" / "out"
    output.mkdir(parents=True)
    rc = _cmd_target_complete(config, Namespace(
        job_id=job_id, target="test_target", output_path=str(output),
        pipeline_state=None))
    assert rc == 0
    loaded = store.load(job_id)
    assert loaded.status == "COMPLETED"
    assert loaded.targets["test_target"].status == "COMPLETED"
    assert loaded.active_target is None
    # next → report，target_outputs 含假 target
    rc = _cmd_next(config, Namespace(job_id=job_id, json_output=True))
    assert rc == 0


def test_parser_choices_follow_registry(test_target):
    """CLI choices 从 registry 派生：注册假 target 后立即成为合法 choice。"""
    assert "test_target" in target_choices()
    parser = _build_parser()
    args = parser.parse_args([
        "target", "start", "j", "--target", "test_target"])
    assert args.target == "test_target"
    with pytest.raises(SystemExit):
        parser.parse_args([
            "target", "start", "j", "--target", "not_a_target"])


# ---------- E9：COMPLETED Job 上 amend ----------


def test_e9_amend_family_router_on_completed_job_ready(env):
    """cangjie COMPLETED → family_router READY → 重入 CORPUS_READY。"""
    config, store = env
    job_id = _make_job(store, ["cangjie"])
    with store.edit(job_id) as m:
        target_start(m, "cangjie")
    output = store.job_dir(job_id) / "handoff" / "out"
    output.mkdir(parents=True, exist_ok=True)
    with store.edit(job_id) as m:
        from knowledge_ingest.next_action import target_complete
        target_complete(m, "cangjie", output)
    assert store.load(job_id).status == "COMPLETED"

    rc = _cmd_job_amend(config, Namespace(job_id=job_id,
                                          add_target="family_router"))
    assert rc == 0
    loaded = store.load(job_id)
    assert "family_router" in loaded.request.targets
    assert loaded.targets["family_router"].status == "READY"  # 依赖已 COMPLETED
    assert loaded.status == "CORPUS_READY"  # 显式重入可调度
    assert loaded.active_target is None
    assert loaded.targets["cangjie"].status == "COMPLETED"  # 旧终态保持


def test_e9_amend_family_router_pending_when_dep_unmet(env):
    """cangjie 未 COMPLETED → family_router PENDING（不重入）。"""
    config, store = env
    job_id = _make_job(store, ["personal"])
    with store.edit(job_id) as m:
        target_start(m, "personal")
    output = store.job_dir(job_id) / "handoff" / "out"
    output.mkdir(parents=True, exist_ok=True)
    with store.edit(job_id) as m:
        from knowledge_ingest.next_action import target_complete
        target_complete(m, "personal", output)
    assert store.load(job_id).status == "COMPLETED"
    assert "cangjie" not in store.load(job_id).targets

    rc = _cmd_job_amend(config, Namespace(job_id=job_id,
                                          add_target="family_router"))
    assert rc == 0
    loaded = store.load(job_id)
    # cangjie entry 被自动补建且未 COMPLETED → family_router PENDING
    assert loaded.targets["cangjie"].status == "PENDING"
    assert loaded.targets["family_router"].status == "PENDING"
    assert loaded.status == "COMPLETED"  # 无新可执行 target → 不重入


# ---------- E14：依赖传播两例 ----------


def test_e14_reject_cangjie_propagates_skip_job_completes(env):
    """reject cangjie → family_router SKIPPED(dependency_skipped) →
    personal COMPLETED → Job COMPLETED。"""
    config, store = env
    job_id = _make_job(store, ["cangjie", "personal", "family_router"])
    with store.edit(job_id) as m:
        target_start(m, "cangjie")
        gate_enter(m, "cangjie", "stage0_overview")
        gate_resolve(m, "cangjie", "stage0_overview", "rejected")
    loaded = store.load(job_id)
    assert loaded.targets["cangjie"].status == "SKIPPED"
    assert loaded.targets["cangjie"].reason == "user_rejected"
    assert loaded.targets["family_router"].status == "SKIPPED"
    assert loaded.targets["family_router"].reason == "dependency_skipped"
    assert loaded.status == "TARGET_RUNNING"  # personal 链式解锁
    assert loaded.active_target == "personal"

    output = store.job_dir(job_id) / "handoff" / "out"
    output.mkdir(parents=True, exist_ok=True)
    with store.edit(job_id) as m:
        target_start(m, "personal")
        from knowledge_ingest.next_action import target_complete
        target_complete(m, "personal", output)
    loaded = store.load(job_id)
    assert loaded.targets["personal"].status == "COMPLETED"
    assert loaded.status == "COMPLETED"  # SKIPPED 计入完成聚合
    # personal 不受 cangjie 被拒影响（depends_on=[]，只消费 Corpus）
    assert loaded.targets["personal"].depends_on == []


def test_e14_failed_cangjie_propagates_skip_partial(env):
    """cangjie FAILED → family_router SKIPPED(dependency_failed) →
    personal COMPLETED → PARTIAL。"""
    config, store = env
    job_id = _make_job(store, ["cangjie", "personal", "family_router"])
    with store.edit(job_id) as m:
        target_start(m, "cangjie")
        # 程序化置 FAILED（无 CLI 命令），settle 完成传播与聚合
        m.targets["cangjie"].status = "FAILED"
        settle(m)
    loaded = store.load(job_id)
    assert loaded.targets["cangjie"].status == "FAILED"
    assert loaded.targets["family_router"].status == "SKIPPED"
    assert loaded.targets["family_router"].reason == "dependency_failed"

    output = store.job_dir(job_id) / "handoff" / "out"
    output.mkdir(parents=True, exist_ok=True)
    with store.edit(job_id) as m:
        target_start(m, "personal")
        from knowledge_ingest.next_action import target_complete
        target_complete(m, "personal", output)
    loaded = store.load(job_id)
    assert loaded.status == "PARTIAL"  # ≥1 COMPLETED 且 ≥1 FAILED


# ---------- 规格 4：job create --with-router ----------


def test_with_router_ensures_entry_and_direct_dep_only(env):
    config, store = env
    from knowledge_ingest.cli import _cmd_job_create

    rc = _cmd_job_create(config, Namespace(
        provider="local", source="/tmp/course", targets=["personal"],
        prompt="", with_router="family_router"))
    assert rc == 0
    store = ManifestStore(jobs_root=config.pipeline_root / "jobs")
    job_ids = store.list_jobs()
    manifest = store.load(job_ids[-1])
    # router entry 自动确保 + 其直接依赖 cangjie entry 自动补建
    assert manifest.request.targets == ["personal", "family_router"]
    assert "family_router" in manifest.targets
    assert "cangjie" in manifest.targets
    assert manifest.targets["family_router"].depends_on == ["cangjie"]
    # 不自动启动
    assert manifest.targets["family_router"].status == "PENDING"
    assert manifest.targets["cangjie"].status == "PENDING"
    assert manifest.active_target is None


def test_with_router_default_is_family_router(env):
    config, store = env
    from knowledge_ingest.cli import _cmd_job_create

    rc = _cmd_job_create(config, Namespace(
        provider="local", source="/tmp/course", targets=["cangjie"],
        prompt="", with_router=None))
    assert rc == 0


def test_with_router_unregistered_target_fails_fast(env):
    config, store = env
    from knowledge_ingest.cli import _cmd_job_create

    rc = _cmd_job_create(config, Namespace(
        provider="local", source="/tmp/course", targets=["cangjie"],
        prompt="", with_router="ghost_router"))
    assert rc == 2


# ---------- 规格 10 CLI：gate preauthorize 全链 ----------


def test_gate_preauthorize_cli_full_chain(env):
    import json as _json

    config, store = env
    job_id = _make_job(store, ["cangjie"])
    with store.edit(job_id) as m:
        target_start(m, "cangjie")
        gate_enter(m, "cangjie", "stage5_install_location")
    # 预授权
    rc = _cmd_gate(config, Namespace(
        job_id=job_id, gate_command="preauthorize", target="cangjie",
        name="stage5_install_location", value="/Volumes/ORICO/Skills"))
    assert rc == 0
    loaded = store.load(job_id)
    assert len(loaded.preauthorizations) == 1
    grant_id = loaded.preauthorizations[0]["grant_id"]
    # resolve 引用 grant（不得再传 value）
    rc = _cmd_gate(config, Namespace(
        job_id=job_id, gate_command="resolve", target="cangjie",
        name="stage5_install_location", decision="confirmed",
        preauthorization=grant_id))
    assert rc == 0
    loaded = store.load(job_id)
    assert loaded.targets["cangjie"].status == "RUNNING"
    assert loaded.gate_history[-1]["approval_mode"] == "preauthorized"
    # grant 已标记使用
    assert loaded.preauthorizations[0]["used_at"] is not None



def test_target_checkpoint_cli_emits_event(env):
    import json as _json

    from knowledge_ingest.cli import _cmd_target_checkpoint

    config, store = env
    job_id = _make_job(store, ["cangjie"])
    with store.edit(job_id) as m:
        target_start(m, "cangjie")
    rc = _cmd_target_checkpoint(config, Namespace(
        job_id=job_id, target="cangjie", phase="stage3",
        checkpoint=str(store.job_dir(job_id) / "ckpt.md"),
        evidence=str(store.job_dir(job_id) / "evidence")))
    assert rc == 0
    loaded = store.load(job_id)
    state = loaded.targets["cangjie"]
    assert state.attempt == 1
    assert state.checkpoint_path is not None
    assert state.evidence_dir is not None
    assert state.checkpoint_at is not None
    events = [_json.loads(line) for line in
              (store.job_dir(job_id) / "logs" / "events.jsonl")
              .read_text(encoding="utf-8").splitlines()]
    event = next(e for e in events if e["event"] == "target_checkpointed")
    assert event["target"] == "cangjie"
    assert event["phase"] == "stage3"
    assert event["attempt"] == 1


# ---------- budget CLI 全链（acquire → outcome → 拒绝 → resume） ----------


def test_budget_cli_acquire_outcome_and_block_resume(env):
    config, store = env
    job_id = _make_job(store, ["family_router"])
    # family_router 依赖 cangjie：先补一个 COMPLETED cangjie entry
    with store.edit(job_id) as m:
        m.targets["cangjie"].status = "COMPLETED"
    with store.edit(job_id) as m:
        target_start(m, "family_router")
    # 3 连 empty → breaker 熔断 → BLOCKED
    for i in range(3):
        rc = _cmd_budget(config, Namespace(
            job_id=job_id, budget_command="acquire", target="family_router",
            host="llmapi", case_id=f"case-{i}", request_id=f"rq-{i}"))
        assert rc == 0
        events = (store.job_dir(job_id) / "logs" / "events.jsonl") \
            .read_text(encoding="utf-8").splitlines()
        permit_id = __import__("json").loads(events[-1])["permit_id"]
        rc = _cmd_budget(config, Namespace(
            job_id=job_id, budget_command="outcome",
            permit=permit_id, result="empty"))
        assert rc == 0
    events = [__import__("json").loads(line) for line in
              (store.job_dir(job_id) / "logs" / "events.jsonl")
              .read_text(encoding="utf-8").splitlines()]
    assert any(e["event"] == "external_call_acquired" for e in events)
    assert any(e["event"] == "external_call_outcome" for e in events)

    rc = _cmd_budget(config, Namespace(
        job_id=job_id, budget_command="acquire", target="family_router",
        host="llmapi", case_id="case-x", request_id="rq-x"))
    assert rc == 1  # 拒绝
    loaded = store.load(job_id)
    assert loaded.targets["family_router"].status == "BLOCKED"
    assert loaded.status == "BLOCKED"
    assert loaded.active_target is None

    rc = _cmd_target_resume(config, Namespace(job_id=job_id,
                                              target="family_router"))
    assert rc == 0
    loaded = store.load(job_id)
    assert loaded.targets["family_router"].status == "READY"  # cangjie COMPLETED
    assert loaded.status == "CORPUS_READY"


def test_budget_amend_cli_does_not_restore(env):
    config, store = env
    job_id = _make_job(store, ["family_router"])
    with store.edit(job_id) as m:
        m.targets["cangjie"].status = "COMPLETED"
    # handoff 先行（真实流程：CORPUS_READY 时 distill prepare）
    assert _cmd_distill_prepare(config, Namespace(
        job_id=job_id, target="family_router")) == 0
    with store.edit(job_id) as m:
        target_start(m, "family_router")
    # 熔断 → BLOCKED
    for i in range(3):
        _cmd_budget(config, Namespace(
            job_id=job_id, budget_command="acquire",
            target="family_router", host="h", case_id=f"c{i}",
            request_id=f"r{i}"))
        events = (store.job_dir(job_id) / "logs" / "events.jsonl") \
            .read_text(encoding="utf-8").splitlines()
        permit_id = __import__("json").loads(events[-1])["permit_id"]
        _cmd_budget(config, Namespace(
            job_id=job_id, budget_command="outcome", permit=permit_id,
            result="empty"))
    _cmd_budget(config, Namespace(
        job_id=job_id, budget_command="acquire", target="family_router",
        host="h", case_id="cx", request_id="rx"))
    assert store.load(job_id).status == "BLOCKED"
    rc = _cmd_budget(config, Namespace(
        job_id=job_id, budget_command="amend", target="family_router",
        max_external_calls=99, max_retries_per_case=5))
    assert rc == 0
    loaded = store.load(job_id)
    # 修改预算 ≠ 自动恢复
    assert loaded.status == "BLOCKED"
    assert loaded.targets["family_router"].status == "BLOCKED"
    handoff = (store.job_dir(job_id) / "handoff"
               / "target-family_router.yaml").read_text(encoding="utf-8")
    assert "max_external_calls: 99" in handoff
