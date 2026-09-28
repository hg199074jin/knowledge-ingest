"""V3 M8: Insight CLI, Digest, Doctor, and Shadow Agent (M8-c)."""

from __future__ import annotations

from pathlib import Path

from knowledge_ingest.config import AppConfig
from knowledge_ingest.insight.digest import build_insight_digest
from knowledge_ingest.insight.doctor import run_insight_doctor
from knowledge_ingest.insight.shadow_agent import (
    generate_insight_plist_bytes,
)
from knowledge_ingest.insight.store import InsightStore


def make_config(tmp_path: Path) -> AppConfig:
    return AppConfig.model_validate({
        "pipeline_root": str(tmp_path / "kp"),
        "media_project": str(tmp_path / "m"),
        "docchunk_project": str(tmp_path / "d"),
        "media_output_root": str(tmp_path / "mo"),
        "docchunk_corpus_root": str(tmp_path / "dc"),
        "skill_roots": ["~/.agents/skills"],
        "skills": {"baidu": "b", "quark": "q", "cangjie": "c",
                   "personal_distiller": "p", "k2c": "k2c"},
    })


def make_store(tmp_path: Path) -> InsightStore:
    return InsightStore(tmp_path / "insight" / "state.db")


# ---------- digest ----------

def test_digest_contains_navigation_sections(tmp_path):
    store = make_store(tmp_path)
    digest = build_insight_digest(store)
    assert "Insight Digest" in digest
    assert "pending" in digest.lower() or "watch" in digest.lower()


def test_digest_empty_store_shows_zero(tmp_path):
    store = make_store(tmp_path)
    digest = build_insight_digest(store)
    assert "0" in digest


# ---------- doctor ----------

def test_doctor_read_only_checks(tmp_path):
    config = make_config(tmp_path)
    store = make_store(tmp_path)
    checks = run_insight_doctor(config, store)
    assert isinstance(checks, list)
    assert all(isinstance(c, tuple) for c in checks)
    names = [c[0] for c in checks]
    assert "insight_root" in names or any("root" in n for n in names)


def test_doctor_detects_unwritable_root(tmp_path):
    config = make_config(tmp_path / "nonexistent" / "deep")
    checks = run_insight_doctor(config, None)
    assert any("FAIL" in c[1] for c in checks)


# ---------- shadow agent ----------

def test_insight_label_and_plist():
    from knowledge_ingest.insight.shadow_agent import (
        insight_label,
    )
    label = insight_label()
    assert "ki-insight-shadow" in label
    plist = generate_insight_plist_bytes(
        ki_exe="/usr/bin/knowledge-ingest",
        config_path="/tmp/config.yaml")
    import plistlib
    cfg = plistlib.loads(plist)
    assert cfg["StartInterval"] == 300
    assert "--config" in cfg["ProgramArguments"]   # pin --config
    assert "insight" in " ".join(cfg["ProgramArguments"])
