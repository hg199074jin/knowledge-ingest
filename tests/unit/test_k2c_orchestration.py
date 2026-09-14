"""Task 23: k2c rides the generic KI orchestration machinery.

K2C 负责 reasoning task / transaction recovery；KI 只负责 target 编排。
本文件证明 invoke_key 调度、target checkpoint、budget-breaker resume 这些
冻结的通用机制对 k2c 天然生效，且 KI 不假装能恢复 K2C 内部的 host call
（checkpoint_path 指向 K2C 侧产物，resume 语义 = 重新调度 target）。
"""

import json
from argparse import Namespace

import pytest

from knowledge_ingest.cli import (
    _cmd_budget,
    _cmd_distill_prepare,
    _cmd_target_checkpoint,
    _cmd_target_resume,
)
from knowledge_ingest.manifest_store import ManifestStore
from knowledge_ingest.next_action import next_action, target_start

from .test_preprocess_cli import make_config
from .test_target_runtime_e2e import _make_job


@pytest.fixture()
def env(tmp_path):
    config = make_config(tmp_path)
    store = ManifestStore(jobs_root=config.pipeline_root / "jobs")
    return config, store


def test_next_action_routes_to_invoke_k2c(env):
    _config, store = env
    job_id = _make_job(store, ["k2c"])
    action = next_action(store.load(job_id))
    assert action["next_action"] == "invoke_k2c"


def test_k2c_checkpoint_records_k2c_side_artifacts(env):
    config, store = env
    job_id = _make_job(store, ["k2c"])
    with store.edit(job_id) as m:
        target_start(m, "k2c")
    # K2C 侧 checkpoint 产物路径由 host agent 传入（KI 不生产它）
    k2c_ckpt = config.pipeline_root / "k2c-run" / "state.yaml"
    k2c_ckpt.parent.mkdir(parents=True)
    k2c_ckpt.write_text("state: CAPABILITY_MINING_PENDING\n", encoding="utf-8")
    rc = _cmd_target_checkpoint(config, Namespace(
        job_id=job_id, target="k2c", phase="build",
        checkpoint=str(k2c_ckpt), evidence=None))
    assert rc == 0
    state = store.load(job_id).targets["k2c"]
    assert state.checkpoint_path is not None
    assert state.checkpoint_at is not None
    events = [json.loads(line) for line in
              (store.job_dir(job_id) / "logs" / "events.jsonl")
              .read_text(encoding="utf-8").splitlines()]
    event = next(e for e in events if e["event"] == "target_checkpointed")
    assert event["target"] == "k2c"


def test_k2c_budget_breaker_resume_roundtrip(env):
    """熔断 → BLOCKED → resume → READY：与 family_router 同一套机制。"""
    config, store = env
    job_id = _make_job(store, ["k2c"])
    assert _cmd_distill_prepare(config, Namespace(
        job_id=job_id, target="k2c")) == 0
    with store.edit(job_id) as m:
        target_start(m, "k2c")
    for i in range(3):
        assert _cmd_budget(config, Namespace(
            job_id=job_id, budget_command="acquire", target="k2c",
            host="llmapi", case_id=f"c{i}", request_id=f"r{i}")) == 0
        events = (store.job_dir(job_id) / "logs" / "events.jsonl") \
            .read_text(encoding="utf-8").splitlines()
        permit_id = json.loads(events[-1])["permit_id"]
        _cmd_budget(config, Namespace(
            job_id=job_id, budget_command="outcome", permit=permit_id,
            result="empty"))
    # 第 4 次 acquire 被 breaker 拒绝 → BLOCKED
    assert _cmd_budget(config, Namespace(
        job_id=job_id, budget_command="acquire", target="k2c",
        host="llmapi", case_id="cx", request_id="rx")) == 1
    assert store.load(job_id).status == "BLOCKED"
    assert _cmd_target_resume(config, Namespace(
        job_id=job_id, target="k2c")) == 0
    loaded = store.load(job_id)
    assert loaded.targets["k2c"].status == "READY"
    assert loaded.status == "CORPUS_READY"


def test_k2c_handoff_carries_job_ref_for_checkpoint_linkage(env):
    """handoff 里的 k2c.job_ref 把 KI job 与 K2C run 关联起来（Task 23）。"""
    config, store = env
    job_id = _make_job(store, ["k2c"])
    assert _cmd_distill_prepare(config, Namespace(
        job_id=job_id, target="k2c")) == 0
    import yaml

    handoff = yaml.safe_load(
        (store.job_dir(job_id) / "handoff" / "target-k2c.yaml")
        .read_text(encoding="utf-8"))
    assert handoff["k2c"]["job_ref"] == job_id
