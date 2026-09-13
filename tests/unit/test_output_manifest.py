"""tests/unit/test_output_manifest.py — Cangjie Output Manifest（事务化+决定性）。

对 fixture 产物生成 manifest → 字段断言 → 两次生成 byte-identical →
complete 失败注入（rename 失败 → TargetState 不变）。
"""

import json
from argparse import Namespace
from pathlib import Path

import pytest

from knowledge_ingest.cli import _cmd_target_complete
from knowledge_ingest.manifest_store import ManifestStore
from knowledge_ingest.models import JobRequest
from knowledge_ingest.next_action import target_start
from knowledge_ingest.output_manifest import (
    build_output_manifest,
    render_output_manifest,
    skill_tree_sha256,
)

from .test_preprocess_cli import make_config


def build_fixture_output(root: Path) -> Path:
    """造 cangjie 产物：2 个 skill 目录 + DIGEST / GLOSSARY 顶层文件。"""
    output = root / "output" / "skills"
    skill_a = output / "guarantee-course-skill"
    skill_a.mkdir(parents=True)
    (skill_a / "SKILL.md").write_text(
        "---\n"
        "name: guarantee-course-skill\n"
        "description: 融资担保课程技能\n"
        "---\n"
        "# body\n",
        encoding="utf-8")
    (skill_a / "reference.md").write_bytes(b"reference bytes")
    (skill_a / "test_prompts").mkdir()
    (skill_a / "test_prompts" / "p1.md").write_text("p", encoding="utf-8")

    skill_b = output / "audit-basics"
    skill_b.mkdir(parents=True)
    (skill_b / "SKILL.md").write_text(
        "---\n"
        "name: audit-basics\n"
        "description: 审计基础\n"
        "---\n",
        encoding="utf-8")
    # cangjie 实际产物命名：test-results.md（带连字符）
    (skill_b / "test-results.md").write_text("results", encoding="utf-8")

    for name in ("DIGEST", "GLOSSARY"):
        (output / name).write_text(f"{name} content\n", encoding="utf-8")
    return output


@pytest.fixture()
def cangjie_job(tmp_path: Path):
    config = make_config(tmp_path)
    store = ManifestStore(jobs_root=config.pipeline_root / "jobs")
    manifest = store.create(JobRequest(
        raw_prompt="x", provider="local", source=str(tmp_path / "none"),
        targets=["cangjie"]))
    with store.edit(manifest.job_id) as m:
        m.status = "CORPUS_READY"
    with store.edit(manifest.job_id) as m:
        target_start(m, "cangjie")
    return config, store, manifest.job_id


def make_completed_job(tmp_path: Path):
    config = make_config(tmp_path)
    store = ManifestStore(jobs_root=config.pipeline_root / "jobs")
    manifest = store.create(JobRequest(
        raw_prompt="x", provider="local", source=str(tmp_path / "none"),
        targets=["cangjie"]))
    return config, store, manifest.job_id


def test_skill_tree_sha256_is_order_stable(tmp_path: Path):
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "a.txt").write_bytes(b"a")
    sub = skill / "b"
    sub.mkdir()
    (sub / "c.txt").write_bytes(b"c")
    first = skill_tree_sha256(skill)
    # 内容变化 → hash 变化
    (skill / "a.txt").write_bytes(b"A")
    assert skill_tree_sha256(skill) != first


def test_build_output_manifest_fields(tmp_path: Path):
    output = build_fixture_output(tmp_path)
    data = build_output_manifest(output)
    assert data["schema_version"] == 1
    # skills 按 name 排序（audit-basics < guarantee-course-skill）
    names = [s["name"] for s in data["skills"]]
    assert names == sorted(names)
    assert names == ["audit-basics", "guarantee-course-skill"]
    by_name = {s["name"]: s for s in data["skills"]}
    assert by_name["audit-basics"]["description"] == "审计基础"
    assert by_name["guarantee-course-skill"]["description"] == "融资担保课程技能"
    assert by_name["guarantee-course-skill"]["sha256"] == \
        skill_tree_sha256(output / "guarantee-course-skill")
    # artifacts 存在性
    assert by_name["guarantee-course-skill"]["artifacts"] == \
        {"test_prompts": True, "test_results": False}
    assert by_name["audit-basics"]["artifacts"] == \
        {"test_prompts": False, "test_results": True}
    # 顶层文件存在性 + counts
    assert data["top_level_files"] == {
        "DIGEST": True, "INDEX": False, "GLOSSARY": True,
        "PIPELINE_STATE": False}
    assert data["counts"] == {"skills": 2, "top_level_files_present": 2,
                              "top_level_files_expected": 4}


def test_render_is_deterministic_and_has_no_generated_at(tmp_path: Path):
    output = build_fixture_output(tmp_path)
    first = render_output_manifest(build_output_manifest(output))
    second = render_output_manifest(build_output_manifest(output))
    assert first == second  # byte-identical
    assert "generated_at" not in first
    # canonical JSON：sort_keys + 缩进 2 + \n 结尾
    assert first.endswith("\n")
    parsed = json.loads(first)
    assert first == json.dumps(parsed, sort_keys=True, indent=2,
                               ensure_ascii=False) + "\n"
    # 与顺序无关：反序重扫（先建 b 目录的情形已由排序断言覆盖）
    data = json.loads(first)
    assert [s["name"] for s in data["skills"]] == \
        ["audit-basics", "guarantee-course-skill"]


def test_target_complete_cangjie_writes_output_manifest(
        tmp_path: Path, cangjie_job):
    config, store, job_id = cangjie_job
    output = build_fixture_output(tmp_path)
    rc = _cmd_target_complete(config, Namespace(
        job_id=job_id, target="cangjie", output_path=str(output),
        pipeline_state=None))
    assert rc == 0
    manifest_path = (store.job_dir(job_id) / "handoff"
                     / "cangjie-output-manifest.json")
    assert manifest_path.is_file()
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert data["counts"]["skills"] == 2
    loaded = store.load(job_id)
    assert loaded.targets["cangjie"].status == "COMPLETED"
    assert loaded.targets["cangjie"].output_manifest == manifest_path
    assert loaded.status == "COMPLETED"
    # 无残留 tmp
    assert not list((store.job_dir(job_id) / "handoff")
                    .glob(".cangjie-output-manifest.json.tmp*"))


def test_target_complete_rename_failure_keeps_target_state(
        tmp_path: Path, cangjie_job, monkeypatch):
    """complete 失败注入：rename 失败 → TargetState 不变（事务放弃）。"""
    import knowledge_ingest.cli as cli_module

    config, store, job_id = cangjie_job
    output = build_fixture_output(tmp_path)

    def broken_replace(src, dst):
        raise OSError("injected rename failure")

    monkeypatch.setattr(cli_module.os, "replace", broken_replace)
    with pytest.raises(OSError, match="injected rename failure"):
        _cmd_target_complete(config, Namespace(
            job_id=job_id, target="cangjie", output_path=str(output),
            pipeline_state=None))
    loaded = store.load(job_id)
    assert loaded.targets["cangjie"].status == "RUNNING"  # 未变
    assert loaded.targets["cangjie"].output_manifest is None
    assert loaded.status == "TARGET_RUNNING"
    assert not (store.job_dir(job_id) / "handoff"
                / "cangjie-output-manifest.json").exists()


def test_target_complete_requires_running_target(tmp_path: Path):
    config, store, job_id = make_completed_job(tmp_path)
    output = build_fixture_output(tmp_path)
    with pytest.raises(ValueError, match="not completable"):
        _cmd_target_complete(config, Namespace(
            job_id=job_id, target="cangjie", output_path=str(output),
            pipeline_state=None))
    loaded = store.load(job_id)
    assert loaded.targets["cangjie"].status == "PENDING"
