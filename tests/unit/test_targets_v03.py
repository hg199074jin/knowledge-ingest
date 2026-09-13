"""tests/unit/test_targets_v03.py — v0.3 Part C 核心结构（TDD）。

覆盖：
- V1→V2 迁移（targets dict / DISTILLING→TARGET_RUNNING / bak-v1 备份字节级 /
  EEXIST 不覆盖 + 完整性校验）
- 依赖图拓扑五件套（unknown dep / self dep / 2-cycle / 3-cycle / 合法 DAG）
- 交叉不变式（规格 8）
- round-trip 保序（规格 2：声明顺序决定先运行谁，序列化必须保序）
"""

import yaml
import pytest

from knowledge_ingest.manifest_store import (
    ManifestStore,
    V1BackupError,
    backup_v1_manifest,
)
from knowledge_ingest.models import JobManifest, JobRequest, TargetState
from knowledge_ingest.state_machine import transition_to
from knowledge_ingest.targets import REGISTRY


def make_manifest(targets=("cangjie", "personal"), status="CORPUS_READY"):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return JobManifest(
        job_id="20260913-000000-local-v03",
        created_at=now, updated_at=now, status=status,
        request=JobRequest(raw_prompt="x", provider="local", source="/tmp/x",
                           targets=list(targets)),
    )


# ---------- registry 基线 ----------


def test_registry_declares_frozen_targets():
    assert set(REGISTRY) >= {"cangjie", "personal", "family_router"}
    # 规格 2：personal depends_on=[]（cangjie 被 reject/skipped 不影响它）
    assert list(REGISTRY["personal"].depends_on) == []
    assert list(REGISTRY["cangjie"].depends_on) == []
    assert list(REGISTRY["family_router"].depends_on) == ["cangjie"]
    # invoke 键向后兼容
    assert REGISTRY["cangjie"].invoke_key == "invoke_cangjie"
    assert REGISTRY["personal"].invoke_key == "invoke_personal_distiller"
    assert REGISTRY["family_router"].invoke_key == "invoke_family_router"


# ---------- V1→V2 迁移：结构 ----------


def test_v1_manifest_migrates_to_targets_dict():
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    v1 = {
        "schema_version": 1,
        "job_id": "20260906-120000-quark-course",
        "created_at": now, "updated_at": now,
        "status": "CREATED",
        "request": {"raw_prompt": "x", "provider": "local",
                    "source": "/tmp/x", "targets": ["cangjie", "personal"]},
        "cangjie": {"status": "success"},
        "personal": {"status": "pending"},
    }
    manifest = JobManifest.model_validate(v1)
    assert manifest.schema_version == 2
    # 保持 cangjie, personal 顺序
    assert list(manifest.targets.keys()) == ["cangjie", "personal"]
    # 旧 status 字符串原样映射（success→COMPLETED / pending→PENDING）
    assert manifest.targets["cangjie"].status == "COMPLETED"
    assert manifest.targets["personal"].status == "PENDING"


def test_v1_distilling_statuses_migrate_to_target_running():
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    base = {
        "schema_version": 1,
        "job_id": "j", "created_at": now, "updated_at": now,
        "request": {"raw_prompt": "x", "provider": "local",
                    "source": "/tmp/x", "targets": ["cangjie", "personal"]},
        "cangjie": {"status": "running"},
        "personal": {"status": "pending"},
    }
    cangjie_running = dict(base, status="DISTILLING_CANGJIE")
    migrated = JobManifest.model_validate(cangjie_running)
    assert migrated.status == "TARGET_RUNNING"
    assert migrated.active_target == "cangjie"

    waiting = dict(base, status="WAITING_USER",
                   cangjie={"status": "waiting_user",
                            "waiting_for": "stage0_overview"})
    migrated = JobManifest.model_validate(waiting)
    assert migrated.status == "WAITING_USER"
    assert migrated.active_target == "cangjie"
    assert migrated.targets["cangjie"].waiting_for == "stage0_overview"

    personal_running = dict(base, status="DISTILLING_PERSONAL",
                            cangjie={"status": "success"})
    migrated = JobManifest.model_validate(personal_running)
    assert migrated.status == "TARGET_RUNNING"
    assert migrated.active_target == "personal"


def test_v1_unknown_target_status_fails_fast():
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    v1 = {
        "schema_version": 1,
        "job_id": "j", "created_at": now, "updated_at": now,
        "status": "CREATED",
        "request": {"raw_prompt": "x", "provider": "local",
                    "source": "/tmp/x", "targets": ["cangjie"]},
        "cangjie": {"status": "warp_drive"},
    }
    with pytest.raises(Exception):
        JobManifest.model_validate(v1)


# ---------- V1→V2 迁移：bak-v1 备份 ----------


V1_YAML = """schema_version: 1
job_id: 20260906-120000-local-legacy
created_at: 2026-09-06T00:00:00+00:00
updated_at: 2026-09-06T00:00:00+00:00
status: CREATED
request:
  raw_prompt: x
  provider: local
  source: /tmp/x
  targets:
    - cangjie
cangjie:
  status: pending
personal:
  status: pending
gate_history: []
errors: []
"""


def _write_v1(store: ManifestStore, job_id: str, text: str = V1_YAML) -> None:
    job_dir = store.job_dir(job_id)
    (job_dir / "logs").mkdir(parents=True, exist_ok=True)
    (store.manifest_path(job_id)).write_text(text, encoding="utf-8")


def test_first_v2_save_creates_bak_v1_byte_exact(tmp_path):
    store = ManifestStore(jobs_root=tmp_path / "jobs")
    job_id = "20260906-120000-local-legacy"
    _write_v1(store, job_id)
    raw_before = store.manifest_path(job_id).read_bytes()

    loaded = store.load(job_id)  # 读入即迁移为 v2 内存副本
    assert loaded.schema_version == 2
    store.save(loaded)  # 首次 v1→v2 持久化

    backup = store.manifest_path(job_id).with_suffix(".yaml.bak-v1")
    assert backup.is_file()
    # 字节级一致
    assert backup.read_bytes() == raw_before
    # 原 job.yaml 已是 v2
    reloaded = store.load(job_id)
    assert reloaded.schema_version == 2
    # 再次 save 不重写备份（仍字节一致）
    store.save(reloaded)
    assert backup.read_bytes() == raw_before


def test_existing_bak_v1_not_overwritten_when_identical(tmp_path):
    store = ManifestStore(jobs_root=tmp_path / "jobs")
    job_id = "20260906-120000-local-legacy"
    _write_v1(store, job_id)
    raw = store.manifest_path(job_id).read_bytes()
    loaded = store.load(job_id)
    store.save(loaded)
    backup = store.manifest_path(job_id).with_suffix(".yaml.bak-v1")
    # 已有备份与待备份字节一致 → 允许继续，绝不覆盖
    backup_v1_manifest(store.manifest_path(job_id), raw)
    assert backup.read_bytes() == raw


def test_existing_bak_v1_divergent_fails_fast(tmp_path):
    store = ManifestStore(jobs_root=tmp_path / "jobs")
    job_id = "20260906-120000-local-legacy"
    _write_v1(store, job_id)
    loaded = store.load(job_id)
    store.save(loaded)
    backup = store.manifest_path(job_id).with_suffix(".yaml.bak-v1")
    # 篡改已有备份 → 与预期不一致 → fail-fast，绝不覆盖
    backup.write_text("schema_version: 1\ntampered: true\n", encoding="utf-8")
    with pytest.raises(V1BackupError):
        backup_v1_manifest(store.manifest_path(job_id),
                           b"schema_version: 1\nother: bytes\n")
    assert backup.read_text(encoding="utf-8") == \
        "schema_version: 1\ntampered: true\n"


def test_v2_save_never_creates_backup(tmp_path):
    store = ManifestStore(jobs_root=tmp_path / "jobs")
    manifest = store.create(JobRequest(
        raw_prompt="x", provider="local", source="/tmp/x",
        targets=["cangjie"]))
    manifest.status = "ROUTING"
    store.save(manifest)
    assert not (store.job_dir(manifest.job_id)
                / "job.yaml.bak-v1").exists()


# ---------- 依赖图拓扑 ----------


def _manifest_with(targets: dict[str, list[str]]) -> JobManifest:
    manifest = make_manifest(targets=tuple(targets))
    manifest.request.targets = list(targets)
    manifest.targets = {name: TargetState(depends_on=deps)
                        for name, deps in targets.items()}
    # 变更后强制回验（拓扑校验在 model_validator 层）
    return JobManifest.model_validate(manifest.model_dump(mode="json"))


def test_unknown_dependency_rejected():
    with pytest.raises(ValueError, match="unknown dependency"):
        _manifest_with({"cangjie": [], "personal": ["ghost"]})


def test_self_dependency_rejected():
    with pytest.raises(ValueError, match="self dependency"):
        _manifest_with({"cangjie": ["cangjie"]})


def test_two_cycle_rejected():
    with pytest.raises(ValueError, match="cycle"):
        _manifest_with({"a": ["b"], "b": ["a"]})


def test_three_cycle_rejected():
    with pytest.raises(ValueError, match="cycle"):
        _manifest_with({"a": ["b"], "b": ["c"], "c": ["a"]})


def test_legal_dag_accepted_in_declaration_order():
    # 声明顺序故意"倒着"：依赖在前，被依赖在后——合法 DAG 必须接受
    manifest = _manifest_with({
        "family_router": ["cangjie"],
        "personal": [],
        "cangjie": [],
    })
    assert list(manifest.targets) == ["family_router", "personal", "cangjie"]
    # 合法 DAG 按声明顺序解锁：只有 cangjie（无依赖）可先跑
    transition_to(manifest, "TARGET_RUNNING")
    assert manifest.targets["cangjie"].status == "PENDING"


# ---------- 交叉不变式（规格 8） ----------


def test_invariant_running_requires_matching_active_target():
    manifest = make_manifest()
    manifest.targets["cangjie"].status = "RUNNING"
    manifest.active_target = None  # 非法：RUNNING ⇒ active_target==cangjie
    with pytest.raises(ValueError, match="active_target"):
        JobManifest.model_validate(manifest.model_dump(mode="json"))


def test_invariant_running_requires_matching_overall():
    manifest = make_manifest()
    manifest.targets["cangjie"].status = "RUNNING"
    manifest.active_target = "cangjie"
    manifest.status = "CORPUS_READY"  # 非法：RUNNING ⇒ overall TARGET_RUNNING
    with pytest.raises(ValueError, match="overall status"):
        JobManifest.model_validate(manifest.model_dump(mode="json"))


def test_invariant_at_most_one_active_target():
    manifest = make_manifest()
    manifest.targets["cangjie"].status = "RUNNING"
    manifest.targets["personal"].status = "WAITING_USER"
    manifest.active_target = "cangjie"
    with pytest.raises(ValueError, match="at most one"):
        JobManifest.model_validate(manifest.model_dump(mode="json"))


def test_invariant_terminal_must_not_be_active_target():
    manifest = make_manifest()
    manifest.targets["cangjie"].status = "COMPLETED"
    manifest.active_target = "cangjie"
    with pytest.raises(ValueError, match="must not be active_target"):
        JobManifest.model_validate(manifest.model_dump(mode="json"))


def test_invariant_waiting_user_requires_matching_overall():
    manifest = make_manifest()
    manifest.targets["cangjie"].status = "WAITING_USER"
    manifest.active_target = "cangjie"
    manifest.status = "TARGET_RUNNING"  # 非法：WAITING_USER ⇒ overall 对应
    with pytest.raises(ValueError, match="overall status"):
        JobManifest.model_validate(manifest.model_dump(mode="json"))


def test_valid_running_state_passes_validation():
    manifest = make_manifest()
    manifest.targets["cangjie"].status = "RUNNING"
    manifest.active_target = "cangjie"
    manifest.status = "TARGET_RUNNING"
    validated = JobManifest.model_validate(manifest.model_dump(mode="json"))
    assert validated.active_target == "cangjie"


# ---------- round-trip 保序 ----------


def test_targets_declaration_order_survives_save_load_roundtrip(tmp_path):
    """规格 2：多 READY 按声明顺序取第一个 → 序列化必须保序。"""
    store = ManifestStore(jobs_root=tmp_path / "jobs")
    manifest = make_manifest(
        targets=("personal", "cangjie", "family_router"))
    # 首次 save：request.targets 驱动 entry 创建顺序（personal, cangjie,
    # family_router + 依赖补建的 cangjie 已存在）
    store.save(manifest)
    loaded1 = store.load(manifest.job_id)
    assert list(loaded1.targets.keys()) == \
        ["personal", "cangjie", "family_router"]
    store.save(loaded1)
    loaded2 = store.load(manifest.job_id)
    assert list(loaded2.targets.keys()) == \
        ["personal", "cangjie", "family_router"]
    # yaml 文本本身也保序（sort_keys=False）
    text = store.manifest_path(manifest.job_id).read_text(encoding="utf-8")
    assert text.index("personal:") < text.index("cangjie:") \
        < text.index("family_router:")


def test_with_router_semantics_at_model_level():
    """规格 4：family_router entry 自动确保其直接 depends_on（cangjie）entry，
    不递归补（cangjie 无依赖，此处验证 entry 补建不自动启动）。"""
    manifest = make_manifest(targets=("family_router",))
    assert "family_router" in manifest.targets
    assert "cangjie" in manifest.targets  # 直接依赖自动补 entry
    assert manifest.targets["family_router"].depends_on == ["cangjie"]
    assert manifest.targets["family_router"].status == "PENDING"  # 不自动启动
