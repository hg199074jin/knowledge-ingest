"""Task 21 + 22: K2C output manifest (target-aware dispatch) + status mapping.

设计约束（实施方案 V3 Task 18 M7 / Task 21 / Task 22）：
- output_manifest.py 保持 Cangjie 专用不动；新增 k2c_output_manifest.py；
  CLI target complete 按 target dispatch，禁止复用 scan_skills() 语义。
- K2C 侧在 run 目录写 k2c-target-manifest.yaml（status + 产物清单）；
  KI 读取并决定性转存为 handoff/k2c-output-manifest.json。
- 状态映射：completed / needs_review_nonblocking 允许 complete；
  needs_review_blocking / paused_budget / failed 拒绝 complete。
"""

import json
from argparse import Namespace
from pathlib import Path

import pytest
import yaml

from knowledge_ingest.cli import _cmd_target_complete
from knowledge_ingest.k2c_output_manifest import (
    K2CManifestError,
    build_k2c_output_manifest,
    render_k2c_output_manifest,
)
from knowledge_ingest.manifest_store import ManifestStore
from knowledge_ingest.next_action import target_start

from .test_preprocess_cli import make_config
from .test_target_runtime_e2e import _make_job


@pytest.fixture()
def env(tmp_path):
    config = make_config(tmp_path)
    store = ManifestStore(jobs_root=config.pipeline_root / "jobs")
    return config, store


def _write_run_manifest(run_dir: Path, status: str = "completed") -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "staged").mkdir(exist_ok=True)
    skill = run_dir / "staged" / "k2c-demo"
    skill.mkdir(exist_ok=True)
    (skill / "SKILL.md").write_text("---\nname: k2c-demo\n---\n", encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "run_id": "run.0123456789abcdef",
        "status": status,
        "learning_units": 7,
        "families": 1,
        "variants": 1,
        "cross_source": 0,
        "personal_candidates": 0,
        "coverage_report": {"disposition_coverage": 1.0},
        "eval_report": {"base_model_gain": 1.0, "marginal_system_gain": 1.0},
        "review_pack": "generated/review-pack.md",
        "runtime_snapshot": {"active": {}, "deprecated": []},
        "staged_assets": ["staged/k2c-demo/SKILL.md"],
    }
    (run_dir / "k2c-target-manifest.yaml").write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=True),
        encoding="utf-8",
    )
    return run_dir


def test_build_k2c_output_manifest_is_deterministic(tmp_path: Path):
    run_dir = _write_run_manifest(tmp_path / "run")
    first = build_k2c_output_manifest(run_dir)
    second = build_k2c_output_manifest(run_dir)
    assert first == second
    assert first["status"] == "completed"
    assert first["target"] == "k2c"
    assert first["learning_units"] == 7
    assert first["staged_assets"] == ["staged/k2c-demo/SKILL.md"]
    # canonical JSON: sorted keys, stable bytes
    assert render_k2c_output_manifest(first) == render_k2c_output_manifest(second)


def test_missing_k2c_target_manifest_rejected(tmp_path: Path):
    with pytest.raises(K2CManifestError):
        build_k2c_output_manifest(tmp_path)


def test_staged_assets_must_exist(tmp_path: Path):
    run_dir = _write_run_manifest(tmp_path / "run")
    (run_dir / "staged" / "k2c-demo" / "SKILL.md").unlink()
    with pytest.raises(K2CManifestError, match="staged asset missing"):
        build_k2c_output_manifest(run_dir)


# ---------- 状态映射（Task 22） ----------


def _complete(config, store, job_id: str, run_dir: Path):
    return _cmd_target_complete(config, Namespace(
        job_id=job_id, target="k2c", output_path=str(run_dir),
        pipeline_state=None))


def test_completed_status_allows_complete(env):
    config, store = env
    job_id = _make_job(store, ["k2c"])
    run_dir = _write_run_manifest(config.pipeline_root / "run-ok")
    with store.edit(job_id) as m:
        target_start(m, "k2c")
    assert _complete(config, store, job_id, run_dir) == 0
    manifest = store.load(job_id)
    assert manifest.targets["k2c"].status == "COMPLETED"
    out = store.job_dir(job_id) / "handoff" / "k2c-output-manifest.json"
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["status"] == "completed"
    assert data["target"] == "k2c"


def test_needs_review_nonblocking_completes_with_ref(env):
    config, store = env
    job_id = _make_job(store, ["k2c"])
    run_dir = _write_run_manifest(config.pipeline_root / "run-nb",
                                  status="needs_review_nonblocking")
    with store.edit(job_id) as m:
        target_start(m, "k2c")
    assert _complete(config, store, job_id, run_dir) == 0
    data = json.loads((store.job_dir(job_id) / "handoff"
                       / "k2c-output-manifest.json").read_text(encoding="utf-8"))
    assert data["status"] == "needs_review_nonblocking"
    # review ref 留在 K2C 侧产物里，KI manifest 不吞掉它
    assert data["review_pack"] == "generated/review-pack.md"


def test_needs_review_blocking_rejects_complete(env):
    config, store = env
    job_id = _make_job(store, ["k2c"])
    run_dir = _write_run_manifest(config.pipeline_root / "run-blk",
                                  status="needs_review_blocking")
    with store.edit(job_id) as m:
        target_start(m, "k2c")
    assert _complete(config, store, job_id, run_dir) == 2
    assert store.load(job_id).targets["k2c"].status == "RUNNING"  # 未推进


def test_paused_budget_rejects_complete(env):
    config, store = env
    job_id = _make_job(store, ["k2c"])
    run_dir = _write_run_manifest(config.pipeline_root / "run-paused",
                                  status="paused_budget")
    with store.edit(job_id) as m:
        target_start(m, "k2c")
    assert _complete(config, store, job_id, run_dir) == 2
    assert store.load(job_id).targets["k2c"].status == "RUNNING"
