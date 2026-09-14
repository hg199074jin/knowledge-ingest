"""Task 18 + 20: `target=k2c` registration and handoff (M2).

K2C 是 knowledge-ingest 的第四个 Target：注册为数据、不改编排代码；
distill prepare --target k2c 生成 handoff/target-k2c.yaml，其中必须携带
corpus 身份（fingerprints + verify_status）、budget 段与 k2c data root
（实施方案 V3 Task 18 集成测试要求）。
"""

import json
from argparse import Namespace
from pathlib import Path

import pytest

from knowledge_ingest.cli import _cmd_distill_prepare
from knowledge_ingest.config import SkillNames
from knowledge_ingest.manifest_store import ManifestStore
from knowledge_ingest.targets import REGISTRY
from knowledge_ingest.targets import get as get_target

from .test_preprocess_cli import make_config
from .test_target_runtime_e2e import _make_job


@pytest.fixture()
def env(tmp_path):
    config = make_config(tmp_path)
    store = ManifestStore(jobs_root=config.pipeline_root / "jobs")
    return config, store


def test_k2c_registered_with_frozen_fields():
    runtime = get_target("k2c")
    assert runtime.display_name == "K2C"
    assert runtime.invoke_key == "invoke_k2c"
    assert runtime.skill_config_key == "k2c"
    assert runtime.depends_on == ()
    assert runtime.preauthorizable_gates == ()
    assert runtime.output_manifest == "k2c-output-manifest.json"
    assert runtime.handoff_extra is not None


def test_k2c_does_not_depend_on_legacy_targets():
    """K2C 不依赖 cangjie/personal/family_router（Task 18 禁止项）。"""
    assert get_target("k2c").depends_on == ()


def test_legacy_targets_still_registered():
    """Task 19 regression：旧三 target 一个都不能少。"""
    for name in ("cangjie", "personal", "family_router"):
        assert name in REGISTRY
    runtime = get_target("cangjie")
    assert runtime.output_manifest == "cangjie-output-manifest.json"
    assert runtime.preauthorizable_gates == ("stage5_install_location",)


def test_skill_names_default_k2c():
    names = SkillNames(
        baidu="baidu-drive", quark="quarkclouddrive", cangjie="cangjie-skill",
        personal_distiller="personal-capability-distiller",
    )
    assert names.k2c == "k2c"


def test_config_example_declares_k2c():
    import yaml

    root = Path(__file__).resolve().parents[2]
    cfg = yaml.safe_load((root / "config.example.yaml").read_text(encoding="utf-8"))
    assert cfg["skills"]["k2c"] == "k2c"


# ---------- handoff extra：corpus 身份 + budget ----------


def _mini_corpus(root: Path) -> Path:
    (root / "atomic").mkdir(parents=True)
    (root / "atomic" / "A000001.md").write_text("正文", encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "corpus_id": "mini-1",
        "fingerprints": {
            "source": "s" * 64,
            "normalization": "n" * 64,
            "atomic_policy": "a" * 64,
            "batch_policy": "b" * 64,
        },
        "verification": {"status": "passed", "checked_at": "t", "errors": []},
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    return root


def test_k2c_handoff_extra_reads_corpus_identity(tmp_path: Path):
    from knowledge_ingest.targets import _k2c_handoff_extra

    corpus = _mini_corpus(tmp_path / "corpus")
    lines = _k2c_handoff_extra(manifest=type("M", (), {"job_id": "j1"})(),
                               job_dir=str(tmp_path),
                               corpus_path=str(corpus))
    text = "\n".join(lines)
    assert "data_root: /Volumes/ORICO/Data/KnowledgeToCapability" in text
    assert "corpus_id: mini-1" in text
    assert "verify_status: passed" in text
    assert "source_fingerprint_ref: " + "s" * 64 in text
    assert "budget:" in text and "max_external_calls: 20" in text


def test_k2c_handoff_extra_tolerates_missing_corpus(tmp_path: Path):
    from knowledge_ingest.targets import _k2c_handoff_extra

    lines = _k2c_handoff_extra(manifest=type("M", (), {"job_id": "j1"})(),
                               job_dir=str(tmp_path),
                               corpus_path=str(tmp_path / "nope"))
    text = "\n".join(lines)
    assert "verify_status: unknown" in text


def test_distill_prepare_k2c_writes_handoff(env):
    """集成：distill prepare --target k2c 实际生成 target-k2c.yaml。"""
    config, store = env
    job_id = _make_job(store, ["k2c"])
    corpus = _mini_corpus(config.pipeline_root / "mini-corpus")
    with store.edit(job_id) as m:
        m.docchunk.corpus_path = str(corpus)
    rc = _cmd_distill_prepare(config, Namespace(job_id=job_id, target="k2c"))
    assert rc == 0
    handoff = (store.job_dir(job_id) / "handoff" / "target-k2c.yaml")
    text = handoff.read_text(encoding="utf-8")
    assert f"job_id: {job_id}" in text
    assert "target: k2c" in text
    assert f"corpus_path: {corpus}" in text
    assert "k2c:" in text
    assert "verify_status: passed" in text
    assert "budget:" in text
