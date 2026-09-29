# Golden Set（V3 M9）

## 目的

建立可反复使用的质量基准集，验证 V3 Personal Insight Engine 的深度
判断是否达到"用户过去一对一 ChatGPT 深聊"的质量水平。

## 私有存储

所有真实材料、历史 ChatGPT 高质量回复、Personal Context Pack 等存放在：

```
<VolumeORICO>/KnowledgePipeline/qa/insight-golden/
```

**不入 Git**（隐私 + 体积）。Git 只保存 harness、manifest schema、
脱敏统计与说明。

## 8 类覆盖

| 类别 | 说明 | 校准关联 |
|---|---|---|
| G1 认知升级 | 新机制 / 新判断框架 | |
| G2 商业模式质疑 | 数学成立但获客不成立 | |
| G3 AI/Agent 架构 | 与现有项目真实关联 | M9-CAL-02 |
| G4 应 ARCHIVE 的新颖内容 | 敢于 ARCHIVE / REJECT | |
| G5 个人上下文空结果 | NO_RELEVANT_PERSONAL_CONTEXT | |
| G6 旧认知冲突 | Cognition Change Proposal | |
| G7 弱信号聚合 | Trend Candidate | |
| G8 市场成立但不适合 | Market ≠ Personal | M9-CAL-01 |

## 指标

自动指标（harness 计算）：
- valuable_idea_recall
- deep_read_precision
- false_personal_link_rate
- stale_context_leakage
- critic_reach_rate（到达 Critic 的比例，≠ pass rate）
- critic_first_pass_rate（首轮 PASS / 到达 Critic 数）
- critic_final_pass_rate（修订后最终 PASS / 到达 Critic 数）
- human_quality_pending

⚠️ Calls = 实际模型调用次数，不等于 case 数；revision/retry 会增加
call count。safe fail-closed（如模型输出缺 decision → blocked）不计入
正确语义分类，单独归类为 SAFE_FAIL_CLOSED。

人工指标（与 ChatGPT 对照，M10-P 执行）：
- semantic judgment convergence（8 维度 MATCH/PARTIAL/MISMATCH）

## 运行

```bash
knowledge-ingest insight golden run --manifest <qa>/insight-golden/manifest.json
```

## 已知校准项

- **M9-CAL-01**：P2-india（原则级 Context + cognition NONE 时，
  "陌生人测试"与"禁止硬个性化"的 rubric 张力）
- **M9-CAL-02**：P3-jev（ADD/EXPERIMENT 但 traceability/actionability
  不足时，Critic 是否能稳定拦住）
