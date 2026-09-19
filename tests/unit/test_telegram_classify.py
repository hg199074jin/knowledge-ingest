"""TG5: 分类层 + Source AI Budget（方案 §7；冻结设计 §10/§11/§21.1）。

规则优先：普通白名单 TEXT 与明确广告都是 0 次模型调用；
LLM 只接疑似广告与 PDF Interest，且必须可注入。
预算与熔断拒绝时退化 REVIEW/KEEP-safe，绝不静默 SKIP 知识。
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from knowledge_ingest.telegram.classify import (
    CLASSIFIER_VERSION,
    POLICY_VERSION,
    classify_interest,
    classify_noise,
    parse_noise_json,
)
from knowledge_ingest.telegram.event_store import TelegramEventStore

T0 = "2026-09-18T09:00:00+08:00"

class CountingLLM:
    """可注入的假模型：按脚本返回，并计数调用。"""

    def __init__(self, script=()):
        self.script = list(script)
        self.calls = []

    def __call__(self, prompt):
        self.calls.append(prompt)
        if not self.script:
            raise RuntimeError("model unavailable")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

def make_store(tmp_path: Path) -> TelegramEventStore:
    return TelegramEventStore(tmp_path / "telegram" / "state.db")

def dt(second):
    return datetime(2026, 9, 18, 10, 0, 0, tzinfo=UTC) + timedelta(
        seconds=second)

# ---------- Noise：规则优先 ----------

def test_plain_text_keeps_with_zero_llm_calls():
    llm = CountingLLM()
    decision = classify_noise("Agent 的记忆管理有三个层次：工作记忆、"
                              "情景记忆与语义记忆……", llm=llm)
    assert decision.decision == "KEEP"
    assert llm.calls == []

def test_explicit_ad_skips_with_zero_llm_calls():
    llm = CountingLLM()
    for text in (
        ("📄【体验细节】睡醒，看见蕾蕾遂问，空，即刻约上。到了地方，"
        "一进门就……"),
        "👥老色批交流群3.0 5k，点击进群，更多资源等你",
        "加微信 vk123456 私聊，优惠券返利下单立减",
    ):
        decision = classify_noise(text, llm=llm)
        assert decision.decision == "SKIP", text[:20]
        assert decision.confidence == 1.0
    assert llm.calls == []

def test_suspected_ad_uses_llm_once():
    llm = CountingLLM(script=[json.dumps(
        {"decision": "SKIP", "reason_code": "ad_mixed", "confidence": 0.9})])
    decision = classify_noise("这个方法论很实用；想要完整版的可以私聊我",
                              llm=llm)
    assert decision.decision == "SKIP"
    assert decision.reason_code == "ad_mixed"
    assert len(llm.calls) == 1

def test_llm_failure_falls_back_to_review():
    llm = CountingLLM(script=[RuntimeError("boom")])
    decision = classify_noise("私聊我拿完整版方法论", llm=llm)
    assert decision.decision == "REVIEW"

def test_invalid_json_falls_back_to_review():
    llm = CountingLLM(script=["not json at all"])
    decision = classify_noise("私聊我拿完整版", llm=llm)
    assert decision.decision == "REVIEW"

def test_unknown_enum_falls_back_to_review():
    llm = CountingLLM(script=[json.dumps(
        {"decision": "DEFINITELY", "reason_code": "x", "confidence": 1})])
    decision = classify_noise("私聊我拿完整版", llm=llm)
    assert decision.decision == "REVIEW"

def test_no_llm_channel_suspect_goes_review():
    decision = classify_noise("私聊我拿完整版方法论")
    assert decision.decision == "REVIEW"

def test_parse_noise_json_valid():
    decision = parse_noise_json(json.dumps(
        {"decision": "KEEP", "reason_code": "knowledge", "confidence": 0.8}))
    assert decision is not None and decision.decision == "KEEP"

# ---------- Interest：可注入 + 正例保护 ----------

def pdf_meta(caption="课程说明", filename="课.pdf"):
    return {"caption": caption, "filename": filename, "size_bytes": 1024,
            "source_display_name": "AI探索指南"}

def test_interest_include_by_llm():
    llm = CountingLLM(script=[json.dumps({
        "decision": "INCLUDE", "primary_topic": "AI",
        "reason": "AI 工具主题", "confidence": 0.9})])
    decision = classify_interest(pdf_meta(), llm=llm)
    assert decision.decision == "INCLUDE"
    assert decision.primary_topic == "AI"

def test_interest_exclude_by_llm():
    llm = CountingLLM(script=[json.dumps({
        "decision": "EXCLUDE", "primary_topic": "荐股",
        "reason": "个股推荐", "confidence": 0.95})])
    decision = classify_interest(
        pdf_meta(caption="明日涨停黑马股推荐", filename="荐股.pdf"), llm=llm)
    assert decision.decision == "EXCLUDE"

def test_interest_prompt_carries_positive_protection():
    """冻结 §11.3：投资认知正例必须随 prompt 下发（防语义误杀）。"""
    llm = CountingLLM(script=[json.dumps({
        "decision": "INCLUDE", "primary_topic": "商业分析",
        "reason": "ok", "confidence": 0.9})])
    classify_interest(
        pdf_meta(caption="上市公司商业模式分析", filename="analysis.pdf"),
        llm=llm)
    assert any("上市公司商业模式分析" in prompt for prompt in llm.calls)

def test_interest_insufficient_info_review_without_llm():
    decision = classify_interest(pdf_meta(caption="", filename="file.pdf"))
    assert decision.decision == "REVIEW"

def test_interest_ad_prescreen_skips_llm():
    llm = CountingLLM()
    decision = classify_interest(
        pdf_meta(caption="加微信 vk888 领取优惠", filename="券.pdf"), llm=llm)
    assert decision.decision == "EXCLUDE"
    assert decision.reason == "contact_sale"
    assert llm.calls == []

# ---------- Source AI Budget（SQLite 分账） ----------

def test_budget_acquire_replay_idempotent(tmp_path):
    store = make_store(tmp_path)
    from knowledge_ingest.telegram.event_store import AIBudgetGuard

    guard = AIBudgetGuard(store, max_calls=3)
    assert guard.acquire("tg_a", "noise_classifier", "req-1") == \
        ("permit", "req-1")
    assert guard.acquire("tg_a", "noise_classifier", "req-1") == \
        ("permit", "req-1")            # 重放不重复计数
    row = store.get_ai_budget("tg_a", "noise_classifier")
    assert row["calls"] == 1

def test_budget_exhausted_denies(tmp_path):
    store = make_store(tmp_path)
    from knowledge_ingest.telegram.event_store import AIBudgetGuard

    guard = AIBudgetGuard(store, max_calls=1)
    guard.acquire("tg_a", "noise_classifier", "req-1")
    status, reason = guard.acquire("tg_a", "noise_classifier", "req-2")
    assert status == "denied"
    assert reason == "budget_exhausted"
    # 分类层收到 denied → REVIEW（KEEP-safe）
    llm = CountingLLM(script=["{}"])
    decision = classify_noise("私聊我拿完整版", llm=llm, budget=guard,
                              source_id="tg_a")
    assert decision.decision == "REVIEW"
    assert llm.calls == []             # 拒绝后不调用模型

def test_budget_breaker_opens_on_empties(tmp_path):
    store = make_store(tmp_path)
    from knowledge_ingest.telegram.event_store import AIBudgetGuard

    guard = AIBudgetGuard(store, max_calls=10, breaker_empty=2)
    for i in range(2):
        guard.acquire("tg_a", "noise_classifier", f"req-{i}")
        guard.outcome("tg_a", "noise_classifier", f"req-{i}", "empty")
    status, reason = guard.acquire("tg_a", "noise_classifier", "req-x")
    assert (status, reason) == ("denied", "breaker_open")

def test_budget_outcome_idempotent_and_success_resets(tmp_path):
    store = make_store(tmp_path)
    from knowledge_ingest.telegram.event_store import AIBudgetGuard

    guard = AIBudgetGuard(store, max_calls=10, breaker_empty=2)
    guard.acquire("tg_a", "noise_classifier", "r1")
    guard.outcome("tg_a", "noise_classifier", "r1", "empty")
    guard.outcome("tg_a", "noise_classifier", "r1", "empty")  # 幂等
    guard.acquire("tg_a", "noise_classifier", "r2")
    guard.outcome("tg_a", "noise_classifier", "r2", "success")
    row = store.get_ai_budget("tg_a", "noise_classifier")
    assert row["consecutive_empty"] == 0   # success 双清零

def test_budget_separate_ledger_per_kind(tmp_path):
    store = make_store(tmp_path)
    from knowledge_ingest.telegram.event_store import AIBudgetGuard

    guard = AIBudgetGuard(store, max_calls=1)
    guard.acquire("tg_a", "noise_classifier", "r1")
    assert guard.acquire("tg_a", "pdf_interest_classifier", "r2") == \
        ("permit", "r2")               # 分账：互不挤占

# ---------- pipeline 集成（§7.2 顺序落地） ----------

def make_pipeline(tmp_path, store, **kwargs):
    from knowledge_ingest.telegram.client_port import (
        TelegramDocumentRef,
        TelegramEvent,
        TelegramEventKind,
    )
    from knowledge_ingest.telegram.items import SourceItemPipeline
    from knowledge_ingest.telegram.watcher import TelegramWatcher
    pipeline = SourceItemPipeline(store, data_root=tmp_path,
                                  orico_check=lambda: True, **kwargs)
    watcher = TelegramWatcher(store, pipeline=pipeline)

    def send_text(message_id, second, text):
        watcher.handle_event(TelegramEvent(
            kind=TelegramEventKind.NEW, chat_id=-1001234567890,
            message_id=message_id, message_date=dt(second), sender_id=99,
            text=text))
        watcher.handle_event(TelegramEvent(
            kind=TelegramEventKind.NEW, chat_id=-1001234567890,
            message_id=message_id + 1000, message_date=dt(second + 302),
            sender_id=99, text="tail"))          # 关窗

    def send_pdf(message_id, caption, size=1024):
        doc = TelegramDocumentRef(
            document_id=message_id, file_name="f.pdf",
            mime_type="application/pdf", size_bytes=size)
        watcher.handle_event(TelegramEvent(
            kind=TelegramEventKind.NEW, chat_id=-1001234567890,
            message_id=message_id, message_date=dt(1), sender_id=99,
            text=caption, document=doc))

    return pipeline, send_text, send_pdf

def test_pipeline_text_keep_then_materialized(tmp_path):
    store = make_store(tmp_path)
    store.add_source(source_id="tg_a", chat_id=-1001234567890,
                     start_at=T0)
    llm = CountingLLM()
    _pipeline, send_text, _ = make_pipeline(tmp_path, store, noise_llm=llm)
    send_text(1, 1, "认知负荷理论指出工作记忆的容量限制……")
    item = store.get_source_item("tg_a:1")
    assert item["noise_decision"] == "KEEP"
    assert item["materialized_path"] is not None
    audit = store.list_classifier_audit("tg_a:1")
    assert audit and audit[0]["policy_version"] == POLICY_VERSION
    assert audit[0]["classifier_version"] == CLASSIFIER_VERSION

def test_pipeline_text_ad_skip(tmp_path):
    store = make_store(tmp_path)
    store.add_source(source_id="tg_a", chat_id=-1001234567890,
                     start_at=T0)
    llm = CountingLLM()
    _pipeline, send_text, _ = make_pipeline(tmp_path, store, noise_llm=llm)
    send_text(1, 1, "加微信 vk123456 优惠券返利")
    item = store.get_source_item("tg_a:1")
    assert item["noise_decision"] == "SKIP"
    assert item["processing_status"] == "skipped_noise"
    assert llm.calls == []

def test_pipeline_pdf_interest_flow(tmp_path):
    store = make_store(tmp_path)
    store.add_source(source_id="tg_a", chat_id=-1001234567890,
                     start_at=T0)
    llm = CountingLLM(script=[json.dumps({
        "decision": "INCLUDE", "primary_topic": "AI",
        "reason": "ok", "confidence": 0.9})])
    _pipeline, _, send_pdf = make_pipeline(tmp_path, store,
                                          interest_llm=llm)
    send_pdf(1, "Agent 课程讲义")
    item = store.get_source_item("tg_a:1")
    assert item["interest_decision"] == "INCLUDE"
    assert item["processing_status"] == "interest_include"  # 待 TG6 下载

def test_pipeline_pdf_oversize_include_creates_review(tmp_path):
    store = make_store(tmp_path)
    store.add_source(source_id="tg_a", chat_id=-1001234567890,
                     start_at=T0)
    llm = CountingLLM(script=[json.dumps({
        "decision": "INCLUDE", "primary_topic": "AI",
        "reason": "ok", "confidence": 0.9})] * 2)
    _pipeline, _, send_pdf = make_pipeline(tmp_path, store,
                                          interest_llm=llm)
    send_pdf(1, "大课", size=80 * 1024 * 1024)
    item = store.get_source_item("tg_a:1")
    assert item["interest_decision"] == "INCLUDE"
    reasons = [r["reason"] for r in store.list_open_reviews()]
    assert "size over 50 MiB" in reasons

def test_pipeline_pdf_exclude_skips(tmp_path):
    store = make_store(tmp_path)
    store.add_source(source_id="tg_a", chat_id=-1001234567890,
                     start_at=T0)
    llm = CountingLLM(script=[json.dumps({
        "decision": "EXCLUDE", "primary_topic": "荐股",
        "reason": "个股推荐", "confidence": 0.95})])
    _pipeline, _, send_pdf = make_pipeline(tmp_path, store,
                                          interest_llm=llm)
    send_pdf(1, "明日涨停黑马")
    item = store.get_source_item("tg_a:1")
    assert item["interest_decision"] == "EXCLUDE"
    assert item["processing_status"] == "skipped_interest"

def test_pipeline_cloud_link_stays_pending_zero_calls(tmp_path):
    from knowledge_ingest.telegram.client_port import (
        TelegramEvent,
        TelegramEventKind,
    )
    from knowledge_ingest.telegram.watcher import TelegramWatcher

    store = make_store(tmp_path)
    store.add_source(source_id="tg_a", chat_id=-1001234567890,
                     start_at=T0)
    llm = CountingLLM()
    pipeline, _, _ = make_pipeline(tmp_path, store, noise_llm=llm,
                                   interest_llm=llm)
    watcher = TelegramWatcher(store, pipeline=pipeline)
    watcher.handle_event(TelegramEvent(
        kind=TelegramEventKind.NEW, chat_id=-1001234567890,
        message_id=5, message_date=dt(1), sender_id=99,
        text="资源 https://pan.quark.cn/s/x",
        cloud_links=("https://pan.quark.cn/s/x",)))
    item = store.get_source_item("tg_a:5")
    assert item["processing_status"] == "PENDING_RESOURCE"
    assert llm.calls == []

# ---------- TG5 评审加固（C1 churn / I2 dedup / I3 pdf rebuild / I4 park /
# I6 长正文保护 / I7 可见性 / M8 reason 文案） ----------

def _prep(tmp_path, source_id="tg_a"):
    store = make_store(tmp_path)
    store.add_source(source_id=source_id, chat_id=-1001234567890,
                     start_at=T0)
    return store

def test_c1_skipped_item_not_rescanned_by_recovery(tmp_path):
    store = _prep(tmp_path)
    llm = CountingLLM()
    _pipeline, send_text, _ = make_pipeline(tmp_path, store, noise_llm=llm)
    send_text(1, 1, "加微信 vk123456 优惠券返利下单立减")   # 规则 SKIP
    item_id = "tg_a:1"
    assert store.get_source_item(item_id)["processing_status"] == \
        "skipped_noise"
    audits_before = len(store.list_classifier_audit(item_id))
    calls_before = len(llm.calls)
    from knowledge_ingest.telegram.items import SourceItemPipeline
    pipeline2 = SourceItemPipeline(store, data_root=tmp_path,
                                   orico_check=lambda: True, noise_llm=llm)
    assert pipeline2.recover_stranded() == 0            # 不再重扫 SKIP 项
    assert len(store.list_classifier_audit(item_id)) == audits_before
    assert len(llm.calls) == calls_before

def test_i6_long_methodology_with_ad_tail_not_rule_skipped(tmp_path):
    """§10.3：长方法论正文 + 广告尾巴 → 不允许规则级 SKIP。"""
    body = ("工作记忆的容量限制是认知负荷理论的核心：一次只能主动 "
            "保持约 4 个组块。" * 30) + "\n文末优惠券返利，加微信领取"
    assert len(body) >= 400
    llm = CountingLLM(script=[json.dumps({
        "decision": "KEEP", "reason_code": "knowledge_with_ad_tail",
        "confidence": 0.8})])
    decision = classify_noise(body, llm=llm)
    assert decision.decision == "KEEP"          # 语义判定说了算
    assert len(llm.calls) == 1                  # 走了疑似路径而非规则直杀

def test_i2_repeated_edits_do_not_duplicate_reviews(tmp_path):
    from knowledge_ingest.telegram.client_port import (
        TelegramEvent,
        TelegramEventKind,
    )
    from knowledge_ingest.telegram.watcher import TelegramWatcher

    store = _prep(tmp_path)
    pipeline, send_text, _ = make_pipeline(tmp_path, store)  # llm=None→REVIEW
    send_text(1, 1, "方法论干货：想要完整版的可以私聊我领取")

    watcher = TelegramWatcher(store, pipeline=pipeline)
    for i, text in enumerate(["改一", "改二"], start=2):
        watcher.handle_event(TelegramEvent(
            kind=TelegramEventKind.EDIT, chat_id=-1001234567890,
            message_id=1, message_date=dt(1), sender_id=99, text=text,
            edited_at=dt(60 + i)))
    opens = [r for r in store.list_open_reviews()
             if r["reason"] == "noise_uncertain"]
    assert len(opens) == 1                      # 去重：不随编辑翻倍

def test_i4_review_items_parked_not_materialized(tmp_path):
    store = _prep(tmp_path)
    _pipeline, send_text, _ = make_pipeline(tmp_path, store)  # llm=None
    send_text(1, 1, "干货方法论，想要完整版私聊我领取")
    item = store.get_source_item("tg_a:1")
    assert item["processing_status"] == "noise_review"   # 停车场语义
    assert item["materialized_path"] is None             # 未物化
    assert any(r["reason"] == "noise_uncertain"
               for r in store.list_open_reviews())

def test_i4_empty_item_terminal(tmp_path):
    store = _prep(tmp_path)
    _pipeline, send_text, _ = make_pipeline(tmp_path, store)
    send_text(1, 1, "   ")                      # 空白正文
    item = store.get_source_item("tg_a:1")
    assert item["processing_status"] == "empty_item"
    assert item["materialized_path"] is None
    assert store.list_open_reviews() == []      # 空项不产生人工队列噪音

def test_i3_pdf_edit_reclassifies(tmp_path):
    from knowledge_ingest.telegram.client_port import (
        TelegramEvent,
        TelegramEventKind,
    )
    from knowledge_ingest.telegram.watcher import TelegramWatcher

    store = _prep(tmp_path)
    llm = CountingLLM(script=[
        json.dumps({"decision": "REVIEW", "primary_topic": "?",
                    "reason": "unclear", "confidence": 0.4}),
        json.dumps({"decision": "INCLUDE", "primary_topic": "AI",
                    "reason": "clear now", "confidence": 0.9}),
    ])
    _pipeline, _, send_pdf = make_pipeline(tmp_path, store,
                                           interest_llm=llm)
    send_pdf(1, "讲义")                          # 第一次：REVIEW
    item_id = "tg_a:1"
    assert store.get_source_item(item_id)["interest_decision"] == "REVIEW"

    pipeline = _pipeline
    watcher = TelegramWatcher(store, pipeline=pipeline)
    watcher.handle_event(TelegramEvent(
        kind=TelegramEventKind.EDIT, chat_id=-1001234567890,
        message_id=1, message_date=dt(1), sender_id=99,
        text="Agent 架构讲义", edited_at=dt(60)))  # caption 编辑
    item = store.get_source_item(item_id)
    assert item["interest_decision"] == "INCLUDE"          # 重分类生效
    assert item["processing_status"] == "interest_include"
    assert len(llm.calls) == 2

def test_i7_status_shows_review_backlog(tmp_path):
    store = _prep(tmp_path)
    store.add_source(source_id="tg_a", chat_id=-1001234567890,
                     start_at=T0)
    store.create_source_item("item-1", "tg_a", "text", [1])
    store.create_review("item-1", "noise_uncertain", kind="text")
    summary = store.status_summary()
    assert summary["reviews_by_reason"].get("noise_uncertain") == 1
    assert summary["oldest_open_review_at"] is not None

def test_m8_unknown_size_reason_distinguishes(tmp_path):
    store = _prep(tmp_path)
    llm = CountingLLM(script=[json.dumps({
        "decision": "INCLUDE", "primary_topic": "AI",
        "reason": "ok", "confidence": 0.9})])
    pipeline, _, _send_pdf = make_pipeline(tmp_path, store,
                                           interest_llm=llm)
    from knowledge_ingest.telegram.client_port import (
        TelegramDocumentRef,
        TelegramEvent,
        TelegramEventKind,
    )
    from knowledge_ingest.telegram.watcher import TelegramWatcher
    watcher = TelegramWatcher(store, pipeline=pipeline)
    watcher.handle_event(TelegramEvent(
        kind=TelegramEventKind.NEW, chat_id=-1001234567890,
        message_id=1, message_date=dt(1), sender_id=99, text="课件",
        document=TelegramDocumentRef(document_id=1, file_name="x.pdf",
                                     mime_type="application/pdf",
                                     size_bytes=None)))   # 大小未知
    reasons = [r["reason"] for r in store.list_open_reviews()]
    assert "pdf size unknown" in reasons
    assert "size over 50 MiB" not in reasons
