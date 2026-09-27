"""V3 M2: Telegram shadow adapter — read-only projection of eligible items.

资格是 V2 Intake 的下游（不重实现 noise/interest 策略）：
- text：processing_status == 'materialized'；
- pdf/video：handoff_completed == 1（V2 已接受）；corpus 可达（job.yaml
  → docchunk.corpus_path）→ corpus_verified；corpus 未就绪 → view 存在
  full_text_available=False（WAITING_CONTENT，可恢复，绝不 REJECT）；
- 其余状态（open/review/PENDING_RESOURCE/skipped_*/empty/deleted）→ None。
适配器只读：不修改任何 Telegram 行。
"""

from pathlib import Path

import pytest

from knowledge_ingest.telegram.event_store import TelegramEventStore
from knowledge_ingest.telegram.insight_adapter import TelegramInsightAdapter

NOW = "2026-09-28T12:00:00+00:00"


def make_store(tmp_path: Path) -> TelegramEventStore:
    return TelegramEventStore(tmp_path / "telegram" / "state.db")


def make_adapter(tmp_path: Path, store: TelegramEventStore):
    return TelegramInsightAdapter(store, pipeline_root=tmp_path / "kp")


def add_text_item(store, item_id="tg_a:1", text="正文内容"):
    store.add_source("tg_a", chat_id=-1001, start_at=None)
    store.upsert_message("tg_a", message_id=1, message_date=NOW,
                         sender_id="99", text=text)
    store.create_source_item(item_id, "tg_a", "text", [1])
    store.finalize_item(item_id)
    return item_id


def materialize_text(tmp_path, store, item_id="tg_a:1", body="# T\n\n正文内容"):
    mat = tmp_path / "kp" / "telegram" / "materialized" / item_id / "message.md"
    mat.parent.mkdir(parents=True, exist_ok=True)
    mat.write_text(body, encoding="utf-8")
    store.set_item_materialized(item_id, str(mat))


def add_pdf_item(store, item_id="tg_a:9", *, handoff_job=None):
    store.add_source("tg_b", chat_id=-1002, start_at=None)
    store.upsert_message("tg_b", message_id=9, message_date=NOW,
                         sender_id="99", text="深度报告",
                         has_document=True, document_name="report.pdf",
                         document_mime="application/pdf")
    store.create_source_item(item_id, "tg_b", "pdf", [9])
    store.finalize_item(item_id)
    store.set_item_processing_status(item_id, "interest_include")
    if handoff_job:
        store.set_item_handoff(item_id, handoff_job)


def make_job_yaml(tmp_path: Path, job_id: str, corpus_dir: Path) -> None:
    job_dir = tmp_path / "kp" / "jobs" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "job.yaml").write_text(
        f"status: COMPLETED\ndocchunk:\n  status: verified\n"
        f"  corpus_path: {corpus_dir}\n", encoding="utf-8")


def make_corpus(tmp_path: Path, name="document-set-abc") -> Path:
    corpus = tmp_path / "LongDocCorpus" / name
    (corpus / "batches").mkdir(parents=True)
    (corpus / "index.jsonl").write_text('{"atomic_id": "a0"}\n', encoding="utf-8")
    (corpus / "batches" / "B0001.md").write_text("批一", encoding="utf-8")
    (corpus / "batches" / "B0002.md").write_text("批二", encoding="utf-8")
    return corpus


# ---------- case 1: finalized text + materialized ----------

def test_materialized_text_is_eligible_with_full_text(tmp_path):
    store = make_store(tmp_path)
    add_text_item(store)
    materialize_text(tmp_path, store)
    view = make_adapter(tmp_path, store).to_view("tg_a:1")
    assert view is not None
    assert view.content_kind == "text"
    assert view.full_text_available is True
    assert view.verification_status == "source_only"
    assert view.materialized_path is not None
    assert "正文内容" in view.visible_text
    assert view.provider == "telegram"
    assert view.source_item_id == "tg_a:1"
    assert view.original_refs == ({"message_id": 1},)


def test_materialized_text_with_missing_file_still_view_but_waiting(tmp_path):
    store = make_store(tmp_path)
    add_text_item(store)
    store.set_item_materialized("tg_a:1", str(tmp_path / "gone" / "message.md"))
    view = make_adapter(tmp_path, store).to_view("tg_a:1")
    assert view is not None
    assert view.full_text_available is False   # WAITING_CONTENT, never reject


# ---------- case 2: ineligible V2 states ----------

@pytest.mark.parametrize("status", [
    "skipped_noise", "skipped_interest", "skipped_unsupported",
    "empty_item", "deleted_source", "open", "noise_review",
    "interest_review", "PENDING_RESOURCE"])
def test_non_eligible_statuses_project_to_none(tmp_path, status):
    store = make_store(tmp_path)
    add_text_item(store)
    store.set_item_processing_status("tg_a:1", status)
    assert make_adapter(tmp_path, store).to_view("tg_a:1") is None


def test_pdf_pending_resource_without_handoff_not_eligible(tmp_path):
    store = make_store(tmp_path)
    add_pdf_item(store)                        # interest_include, no handoff
    assert make_adapter(tmp_path, store).to_view("tg_a:9") is None


# ---------- case 3: pdf/video with handoff ----------

def test_pdf_with_verified_corpus_projects_corpus_verified(tmp_path):
    store = make_store(tmp_path)
    corpus = make_corpus(tmp_path)
    make_job_yaml(tmp_path, "job-1", corpus)
    add_pdf_item(store, handoff_job="job-1")
    view = make_adapter(tmp_path, store).to_view("tg_a:9")
    assert view is not None
    assert view.full_text_available is True
    assert view.verification_status == "corpus_verified"
    assert view.materialized_path == str(corpus)
    assert "深度报告" in view.visible_text      # caption as metadata


def test_pdf_handoff_done_but_corpus_missing_waits(tmp_path):
    store = make_store(tmp_path)
    make_job_yaml(tmp_path, "job-1", tmp_path / "LongDocCorpus" / "not-built")
    add_pdf_item(store, handoff_job="job-1")
    view = make_adapter(tmp_path, store).to_view("tg_a:9")
    assert view is not None                    # source retained
    assert view.full_text_available is False   # WAITING_CONTENT
    assert view.verification_status == "source_only"


def test_pdf_handoff_done_without_job_yaml_waits(tmp_path):
    store = make_store(tmp_path)
    add_pdf_item(store, handoff_job="job-missing-yaml")
    view = make_adapter(tmp_path, store).to_view("tg_a:9")
    assert view is not None
    assert view.full_text_available is False


# ---------- stability / purity ----------

def test_rescan_produces_identical_view(tmp_path):
    store = make_store(tmp_path)
    add_text_item(store)
    materialize_text(tmp_path, store)
    adapter = make_adapter(tmp_path, store)
    v1 = adapter.to_view("tg_a:1")
    v2 = adapter.to_view("tg_a:1")
    assert v1 == v2
    assert v1.content_fingerprint == v2.content_fingerprint


def test_edit_changes_fingerprint(tmp_path):
    store = make_store(tmp_path)
    add_text_item(store)
    materialize_text(tmp_path, store)
    adapter = make_adapter(tmp_path, store)
    fp1 = adapter.to_view("tg_a:1").content_fingerprint
    materialize_text(tmp_path, store, body="# T\n\n编辑后的新正文")
    fp2 = adapter.to_view("tg_a:1").content_fingerprint
    assert fp1 != fp2


def test_adapter_is_read_only(tmp_path):
    store = make_store(tmp_path)
    add_text_item(store)
    materialize_text(tmp_path, store)
    add_pdf_item(store, handoff_job="job-x")
    before = store._conn.execute(
        "SELECT item_id, processing_status, materialized_path, "
        "knowledge_ingest_job_id, handoff_completed FROM source_items "
        "ORDER BY item_id").fetchall()
    adapter = make_adapter(tmp_path, store)
    adapter.to_view("tg_a:1")
    adapter.to_view("tg_a:9")
    list(adapter.iter_views())
    after = store._conn.execute(
        "SELECT item_id, processing_status, materialized_path, "
        "knowledge_ingest_job_id, handoff_completed FROM source_items "
        "ORDER BY item_id").fetchall()
    assert [tuple(r) for r in before] == [tuple(r) for r in after]


def test_iter_views_yields_only_eligible(tmp_path):
    store = make_store(tmp_path)
    add_text_item(store, "tg_a:1")
    materialize_text(tmp_path, store)
    add_text_item(store, "tg_a:2")
    store.set_item_processing_status("tg_a:2", "skipped_noise")
    add_pdf_item(store, "tg_b:9", handoff_job="job-1")
    add_pdf_item(store, "tg_b:10")            # no handoff → not eligible
    views = list(make_adapter(tmp_path, store).iter_views())
    ids = {v.source_item_id for v in views}
    # materialized text + handoff'd pdf (WAITING semantics) in;
    # skipped text + pending pdf out.
    assert ids == {"tg_a:1", "tg_b:9"}


def test_iter_views_limit(tmp_path):
    store = make_store(tmp_path)
    for i in (1, 2, 3):
        add_text_item(store, f"tg_a:{i}")
        materialize_text(tmp_path, store, item_id=f"tg_a:{i}",
                         body=f"# T{i}\n\n正文{i}")
    views = list(make_adapter(tmp_path, store).iter_views(limit=2))
    assert len(views) == 2
