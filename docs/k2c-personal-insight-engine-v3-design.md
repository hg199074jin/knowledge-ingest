# Knowledge-Ingest V3 设计文档：Personal Insight Engine

> **从“自动读 Telegram”升级为“持续替用户思考外部信息”**

- 状态：**DRAFT FOR USER REVIEW**
- 日期：2026-09-28
- 所属仓库：`hg199074jin/knowledge-ingest`
- 当前基线：Telegram Source V2 / V2.2 已运行
- 设计性质：在既有 Source / Ingestion 能力之上新增 **Personal Insight Lane**
- 首个接入源：Telegram
- 后续可复用源：本地文件、网页、云盘、人工投递内容
- 核心原则：High Recall First / Think, Don’t Summarize / Evidence Before Advice / Personal Reasoning Retrieval / Critical, Not Agreeable / Cognition Delta Must Be Explicit / Human Gate for Long-Term Cognition / Source Facts and Personal Cognition Must Remain Separate

---

# 0. 结论先行

V3 的目标不是继续优化《TG 深读》的摘要质量，而是改变系统的最终目标函数。

V2 主要解决：

> **如何可靠、自动、低噪音地把 Telegram 内容摄取进知识系统。**

V3 新增解决：

> **当一条内容可能让用户产生觉悟、提升认知、发现新的赚钱机会或影响当前项目时，系统如何像用户过去“把一篇材料单独发给 ChatGPT”那样，进行完整理解、质疑、关联、重构与行动判断。**

因此正式架构不再是：

```text
Telegram
  → 筛选
  → 摘要
  → 周报
```

而是：

```text
Telegram / 其他 Source
        ↓
现有 Source Runtime
        ↓
Normalized Source Item
        ↓
V2 Intake Policy
        ↓
可读 Source Material
        ↓
┌─────────────────────────────────────────────┐
│ Personal Insight Lane                       │
│                                             │
│ High-Recall Candidate Filter                │
│        ↓                                    │
│ Deep-Value Gate                             │
│        ↓                                    │
│ Personal Reasoning Retrieval                │
│        ↓                                    │
│ Evidence Pack                               │
│        ↓                                    │
│ Personal Thinking Engine                    │
│        ↓                                    │
│ Quality Critic                              │
│        ↓                                    │
│ Deep Insight Card                           │
│        ↓                                    │
│ Cognitive Change Proposal                   │
│        ↓                                    │
│ Human Gate                                  │
└─────────────────────────────────────────────┘
        ↓
长期认知 / 实验 / 观察 / 归档
```

V3 的核心验收标准不是“生成了多少摘要”，而是：

> **真正有价值的内容有没有被漏掉；进入深思后的内容有没有被充分榨取价值；最终产物有没有形成用户自己的认知，而不是高级摘要。**

---

# 1. 背景：为什么 V2 输出质量仍然不够

现有 Telegram 系统已经解决了大量工程问题：白名单来源、Source Registry、实时监听与周期 reconcile、不做历史 backfill、5 分钟文字合并、PDF / 视频 / cloud link 路由、三层 Noise / Interest 分类、静态规则、learned fingerprint、LLM 边界判断、SQLite 事实层、ORICO fail-closed、去重、review 队列、knowledge-ingest handoff、K2C staged，以及 Publish / Activate Human Gate。

这些能力仍然保留。

当前问题发生在更下游：

> 系统已经能够找到“值得读的东西”，但最终更像一个优秀编辑在生成高密度周刊，而不是一个长期认识用户的思考伙伴。

《TG 深读》的典型目标是：

```text
大量内容
→ 去重
→ 精选
→ 完整重述
→ 加工
→ 周报
```

这会自然优化覆盖面、信息密度、新闻完整度、单周主题组织和内容压缩效率。

但用户真正需要的是：

```text
一条值得思考的内容
→ 理解
→ 质疑
→ 与过去认知碰撞
→ 与当前项目碰撞
→ 重新建模
→ 形成自己的版本
→ 决定是否改变认知 / 行动
```

二者不是同一个任务。

因此 V3 不继续通过“增加更多 Prompt 要求”“让摘要更长”“多写一点个性化”修补旧产物，而是新增一条独立的 Personal Insight Lane。

---

# 2. V3 产品目标

## 2.1 一句话定义

> **不是帮用户读 Telegram，而是持续替用户思考 Telegram。**

更一般化后：

> **把外部信息转化为用户自己的认知、判断、实验、行动和商业机会。**

## 2.2 什么内容值得进入 V3

只要一条内容具有合理可能：

1. 让用户产生新的觉悟；
2. 提升或修正已有认知；
3. 暴露一个新的赚钱机会；
4. 提出可迁移的方法论；
5. 挑战既有常识或既有判断；
6. 影响当前正在推进的项目；
7. 多个弱信号聚合后形成新趋势；

就应该进入 Value Filter。

不人为限定每天几篇、每周几篇或周报最多多少条。如果一天有 12 条真正值得深思，则 12 条全部进入深度加工。

---

# 3. 非目标

V3 明确不做以下事情：

1. **不替换 Telegram Source V2。** V2 仍负责 Source Runtime、事件事实、广告/噪音过滤、附件处理和恢复。
2. **不把所有 KEEP 内容都送给昂贵模型。** 高成本智能只用于通过两级价值筛选的内容。
3. **不自动修改用户长期认知。** 深度加工可以全自动；长期认知写入必须 Human Gate。
4. **不把所有历史聊天全量塞进模型。** 使用 Personal Reasoning Retrieval，只取会影响本次判断的个人证据。
5. **不强行把每篇内容和用户项目建立联系。** `NO_RELEVANT_PERSONAL_CONTEXT` 是合法结果。
6. **不要求每篇都反驳原作者。** 必须检查反例和边界，但允许最终完全同意。
7. **不把“赚钱案例”自动解释成“适合用户的机会”。** 市场机会与个人机会必须分开。
8. **不让周报成为主产物。** 独立 Deep Insight Card 是主产物；日报/周报只做索引、回顾和提醒。
9. **V3.0 不要求对每条 Telegram 内容自动进行外部 Web 事实核验。** Source-derived 事实与推断必须明确区分；对需要额外验证的关键事实，可生成 `VERIFY_REQUIRED`；自动联网研究可作为后续独立扩展，不得伪造“已验证”。

---

# 4. 与现有 V2 / K2C 的职责边界

## 4.1 V2 Source Runtime 保持稳定

V2 继续负责：

```text
Telegram
  → Raw Event Store
  → Source Item Builder
  → Noise / Interest Policy
  → Materializer
  → knowledge-ingest handoff
```

V3 不重写 Source Registry、Watcher、Item 聚合、Telethon Adapter、Materializer、现有 Noise / Interest 分类原则、learned-rules、review / retention / notification 的既有职责。

## 4.2 Personal Insight Engine 是独立派生层

V3 新增的 Personal Insight Engine 不属于 Telegram 事实层。

原因：

`telegram/state.db` 的职责是回答：

> Telegram 当时发生了什么？

Personal Insight Engine 的职责是回答：

> 这些材料对用户意味着什么？

二者不能混为同一个 source of truth。

建议事实边界：

```text
Telegram source facts
    → telegram/state.db

knowledge-ingest job facts
    → jobs/<job-id>/job.yaml

Personal Insight facts
    → insight/state.db

Capability lifecycle
    → K2C Registry

Approved personal cognition
    → Personal Knowledge Store（Human Gate 后）
```

## 4.3 Personal Insight 与 K2C 是并行关系，不是替代关系

V2 的 K2C Lane 解决：

> 这份材料能否蒸馏成可执行能力 / Skill / SOP？

V3 的 Insight Lane 解决：

> 这份材料是否改变用户的认知、决策、实验或商业判断？

因此允许一份 Source Item 同时：

```text
Source Item
   ├── Capability Lane → K2C
   └── Insight Lane    → Deep Insight Card
```

也允许只有其中之一。禁止为了“统一流水线”把认知变更强行包装成 K2C Capability。

---

# 5. 总体架构

```text
┌─────────────────────────────┐
│ Source Runtime              │
│ Telegram / Local / Web ...  │
└──────────────┬──────────────┘
               ↓
┌─────────────────────────────┐
│ Normalized Source Item      │
└──────────────┬──────────────┘
               ↓
┌─────────────────────────────┐
│ Existing Intake Policy      │
│ Noise / Interest / Review   │
└──────────────┬──────────────┘
               ↓
┌──────────────────────────────────────────┐
│ Insight Candidate Adapter                │
│ Source Item → InsightSourceView          │
└──────────────┬───────────────────────────┘
               ↓
┌──────────────────────────────────────────┐
│ Stage 1: High-Recall Candidate Filter    │
│ “有没有可能值得想？”                    │
└──────────────┬───────────────────────────┘
               ↓
┌──────────────────────────────────────────┐
│ Candidate Pool                           │
│ + Weak Signal / Trend Aggregator         │
└──────────────┬───────────────────────────┘
               ↓
┌──────────────────────────────────────────┐
│ Stage 2: Deep-Value Gate                 │
│ “值不值得花真正的智能？”                 │
└──────────────┬───────────────────────────┘
               ↓
      DEEP_READ / WATCH /
      ARCHIVE_ONLY / REJECT
               ↓
        [DEEP_READ only]
               ↓
┌──────────────────────────────────────────┐
│ Personal Reasoning Retrieval             │
└──────────────┬───────────────────────────┘
               ↓
┌──────────────────────────────────────────┐
│ Evidence Pack                            │
└──────────────┬───────────────────────────┘
               ↓
┌──────────────────────────────────────────┐
│ Personal Thinking Engine                 │
└──────────────┬───────────────────────────┘
               ↓
┌──────────────────────────────────────────┐
│ Quality Critic                           │
│ max 1 revision by default                │
└──────────────┬───────────────────────────┘
               ↓
┌──────────────────────────────────────────┐
│ Deep Insight Card                        │
│ Cognitive Change Proposal                │
└──────────────┬───────────────────────────┘
               ↓
┌──────────────────────────────────────────┐
│ Human Gate                               │
│ ADOPT / EXPERIMENT / WATCH /             │
│ ARCHIVE / REJECT                         │
└──────────────────────────────────────────┘
```

---

# 6. InsightSourceView：源无关输入契约

Personal Insight Engine 不直接消费 Telethon Message，也不直接依赖 Telegram 表结构。

新增源无关对象：

```yaml
schema_version: 1

source:
  provider: telegram
  source_id: tg_side_hustle
  source_item_id: ...
  original_refs:
    - message_id: ...

content:
  kind: text | pdf | video_transcript | web | document
  title: ...
  visible_text: ...
  materialized_path: ...
  verified_corpus_id: null

provenance:
  captured_at: ...
  author: ...
  source_deleted: false

processing:
  full_text_available: true
  verification_status: source_only | corpus_verified
```

设计目的：Telegram 只是首个 producer；后续本地文件、网页、云盘可复用同一 Insight Engine；主思考模块不感知 MTProto / Telegram 业务对象。

---

# 7. Stage 1：High-Recall Candidate Filter

## 7.1 目标

Stage 1 只回答：

> **这条内容有没有合理可能值得进一步思考？**

不是“这篇是不是精品”，也不是“这篇最后会不会改变认知”。

## 7.2 设计偏好

采用：

> **高 Recall、允许较低 Precision。**

宁可多放候选，也不要在第一层永久丢掉真正重要的内容。

核心 KPI：

```text
Valuable Idea Recall
```

## 7.3 候选价值类型

最少支持：

```text
COGNITION
METHOD
BUSINESS_OPPORTUNITY
PROJECT_IMPACT
CONTRARIAN
WEAK_SIGNAL
```

## 7.4 第一层允许使用的个人信息

只允许使用轻量主题画像，例如 AI / Agent、审计 / 财税、商业 / 副业、认知、知识系统、民宿 / OTA、内容 / 产品、个人效率。

第一层不得运行完整 Personal Retrieval，防止“当前没有直接项目关系”导致误杀。

## 7.5 输出示例

```yaml
candidate: true

possible_value:
  - cognition
  - project_impact

reasons:
  - "提出新的 Agent decision-layer 架构"
  - "可能影响现有分类系统成本结构"

confidence: 0.62
```

低于高置信不等于拒绝。

## 7.6 禁止自动蒸馏价值 SKIP 规则

V2 learned-rules 可以继续用于明确广告 / 噪音。

V3 Stage 1 暂不允许从“过去判定无价值”自动学习永久价值过滤规则，因为这会造成难以观察的长期 Recall 损失。

---

# 8. Candidate Pool 与 Weak Signal Aggregator

单篇弱信号可能没有深读价值，但多个弱信号可能形成趋势。

因此 Candidate Pool 必须支持：

```text
单篇强信号
   → Deep-Value Gate

多个弱信号
   → 主题聚类
   → 重复出现 / 证据增长
   → Trend Candidate
   → Deep-Value Gate
```

示例：

```text
A：AI 自动生成门店短视频
B：AI 私域客服
C：AI 自动客户回访
```

单条可能普通，聚合后可能形成：

> **AI 正在成为线下小商户的低成本数字员工。**

弱信号可以进入 `WATCH`。未来出现第二/第三个独立来源、可验证收入数据、实际产品、当前项目关联或与其他 WATCH 条目聚合时重新评估。

---

# 9. Stage 2：Deep-Value Gate

## 9.1 目标

回答：

> **这条内容是否值得消耗一次真正昂贵的 Personal Thinking Engine？**

## 9.2 不采用简单总分阈值

不冻结类似“总分 >= 80 → DEEP_READ”。采用多维结构化判断：

```yaml
novelty: high
cognitive_delta_potential: high
business_potential: medium
project_relevance: high
transferability: high
evidence_quality: medium
contradiction_value: medium
thinking_space: high
```

最终输出：

```text
DEEP_READ
WATCH
ARCHIVE_ONLY
REJECT
```

## 9.3 判断维度

### A. 认知增量
是否出现新机制、新边界、新变量、新因果链、新判断框架。

### B. 冲突价值
是否与已有认知产生真实冲突。冲突本身是高价值信号。

### C. 商业机会价值
是否出现新的“需求 → 支付 → 获客 → 交付”结构，而不是只看“赚了多少钱”。

### D. 当前项目影响
是否可能改变 knowledge-ingest、K2C、Agent / ZCode、审计工作流、民宿、当前副业或其他 active project。

### E. 可迁移性
是否可以从个案抽象出通用机制。

### F. 思考空间
是否存在隐藏假设、边界、争议、不确定性或值得验证的反例。

## 9.4 反驳价值也是价值

一篇文章即使结论最终不成立，只要它暴露一个高频认知陷阱，也可以 DEEP_READ，例如：

```text
市场规模 ≠ 可获得客户
单位经济模型 ≠ 获客模型
Excel 乘法 ≠ 商业模式
```

---

# 10. Personal Reasoning Retrieval

## 10.1 核心目标

不是找“主题相似”的旧文本，而是：

> **找会改变这次判断的用户个人证据。**

## 10.2 四层检索

### Layer A：长期稳定认知
回答：“我以前已经形成过什么判断？”

### Layer B：决策
回答：“我以前已经选过什么？为什么？”包括 active decisions、discarded ideas、已关闭方向、既有架构选择。

### Layer C：当前项目
回答：“这篇内容现在能影响什么？”

### Layer D：历史案例 / 实验
回答：“我过去是否真实验证过类似东西？”

## 10.3 先提炼判断命题，再检索

禁止：

```text
文章 → embedding → top 10 相似 chunk
```

建议：

```text
文章
 ↓
提炼判断命题
 ↓
生成 3～5 个 Personal Retrieval Questions
 ↓
分别检索四层知识
 ↓
去重 / 状态过滤 / 冲突识别
 ↓
Personal Context Pack
```

例如“AI 让小众垂直软件重新具备商业可行性”可拆成：

```text
Q1 用户过去如何判断小众软件的市场规模？
Q2 用户过去有没有讨论 AI 降低交付成本？
Q3 用户的副业选择原则是什么？
Q4 当前是否存在可应用该机制的项目？
Q5 是否已经放弃过类似方向？为什么？
```

## 10.4 关联证据要求

每条召回内容必须说明“为什么它会影响当前判断”。

不允许：

> “这与你关注 AI 很相关。”

允许：

> “这篇材料提出专用判定层；用户既有架构是规则→生成模型，因此它可能改变现有组件边界。”

## 10.5 个人知识状态

至少支持：

```text
CONFIRMED
TENTATIVE
SUPERSEDED
REJECTED
EXPERIMENTING
ARCHIVED
```

默认优先 CONFIRMED、ACTIVE decision、active project、最新有效版本。

`SUPERSEDED` / `REJECTED` 只有在解释认知变化历史、判断是否重新打开旧方向或检查新证据是否推翻过去否决时主动召回。

## 10.6 冲突不得静默消除

如果两条旧原则均相关但看似冲突，则输出：

```text
PERSONAL_CONTEXT_CONFLICT
```

交给主思考模型判断适用边界、是否过时或是否需要形成新的条件化原则。

## 10.7 合法空结果

允许：

```text
NO_RELEVANT_PERSONAL_CONTEXT
```

这可能意味着真正的新认知分支。禁止为了“个性化”硬找三条旧笔记。

---

# 11. Personal Context Pack

主模型不直接接收几十页聊天历史。

Personal Retrieval 输出短小证据包：

```markdown
# Personal Context Pack

## A. Confirmed Cognition
### PC-001
认知：
Agent 效率不只依赖模型能力，还依赖上下文质量、流程稳定性与反馈积累。

状态：CONFIRMED

为什么相关：
当前文章提出独立 decision layer，可能补充 Agent 架构判断。

## B. Active Decision
### DEC-014
已决定：
明确规则走低成本路径，模糊边界才调用高成本模型。

为什么相关：
新材料可能新增“专用判定模型”这一中间层。

## C. Active Project
### PROJ-003
knowledge-ingest / Telegram Source

当前问题：
兴趣判断、广告分类、大模型调用成本。

## D. Discarded Ideas
NO_RELEVANT_PERSONAL_CONTEXT

## E. Experiment History
NO_RELEVANT_PERSONAL_CONTEXT
```

每条内容必须可追溯到原始个人资料来源。

---

# 12. Evidence Pack

Personal Thinking Engine 的输入不只是 Personal Context。

完整 Evidence Pack 至少包含：

```text
A. Source Claims
B. Source Evidence
C. Source Inferences
D. Missing / Unknown Variables
E. Personal Context Pack
F. Current Project Context
G. Verification Flags
```

核心目的：

> **先把“作者说什么”“证据是什么”“用户以前怎么想”分开，再开始综合判断。**

避免 Personal Context 直接变成确认偏误。

---

# 13. Personal Thinking Engine

## 13.1 设计选择

V3 采用：

> **单一强主模型 + 多阶段 Thinking Protocol + 独立 Quality Critic。**

不采用默认的多 Agent 群聊架构，因为多 Agent 容易重复、碎片化、拆散上下文并增加成本，最终仍需要一个主模型整合。

## 13.2 模型接口

通过独立端口隔离具体模型：

```text
ThinkingModelPort
CriticModelPort
```

不得把产品逻辑硬编码到某一家模型 SDK。

实现层可以支持 stdin→stdout 外部命令、API adapter、本地/远端模型。模型选择属于实施配置，不属于本设计冻结的产品语义。

## 13.3 Thinking Protocol

### Phase 1：Understand

暂时不个性化。回答：

1. 作者真正主张什么？
2. 使用了什么证据？
3. 哪些是事实？
4. 哪些是观点？
5. 哪些是推断？
6. 结论依赖哪些前提？
7. 还缺哪些重要变量？

原则：

> Evidence before Advice.

### Phase 2：Challenge

主动检查：幸存者偏差、样本偏差、因果倒置、选择性披露、营销包装、数字游戏、概念偷换、不可复制资源、时间窗口、平台红利、政策风险、隐藏成本、忽略竞争。

要求尝试提出最强反例，但不得为了满足模板强行制造反对意见。

### Phase 3：Connect

把“旧认知 A + 新证据 B”转换成：

> 为什么 B 会影响 A？

必须形成真实因果连接，禁止主题级套话。

### Phase 4：Reconstruct

核心任务：

> **转化为“用户自己的版本”。**

要求：不引用原作者金句作为最终认知；不模仿原文结构；不停留在摘要；从第一性原理重新表达机制；产出未来可直接用于判断类似问题的原则。

### Phase 5：Decide

明确：

```text
认知：新增 / 强化 / 修正 / 推翻 / 无变化
商业机会：立即研究 / 小实验 / 观察 / 不值得
行动：立即行动 / 建立实验 / 加入观察 / 无需行动
```

并允许最终 `ARCHIVE` 或 `REJECT`，即使前面已经通过 Deep-Value Gate。

---

# 14. Business Opportunity Lens

只要发现真实商业模式或交易结构，自动启用。

必须回答：

```text
谁付钱？
为什么付？
现在怎么解决？
新方案为什么更好？
钱怎么进来？
成本在哪里？
怎么获客？
怎么交付？
能否重复？
是否复购？
壁垒是什么？
谁会快速复制？
法律 / 政策 / 平台风险是什么？
```

然后分开判断：

## 14.1 Market Opportunity

> 换成一个完全不认识用户的人，这个机会仍然成立吗？

## 14.2 Personal Opportunity

> 为什么用户比普通人更适合或更不适合做？

只有两者都成立，才可以高优先级推荐实验。

禁止：看到赚钱案例 → 自动建议用户做。

---

# 15. Deep Insight Card

## 15.1 主产物

一篇通过最终深思的内容对应一份独立 Deep Insight Card。

日报/周报不得替代卡片。

## 15.2 逻辑质量契约

每张卡必须在后台完成以下问题，即使最终 Markdown 不机械显示十个固定标题：

1. 我的结论；
2. 原文真正说什么；
3. 真正有价值的机制；
4. 有哪些漏洞 / 边界 / 反例；
5. 和过去认知的真实关系；
6. 对当前项目的真实影响；
7. 如涉及商业机会，完成 Business Opportunity Lens；
8. 转化为“我的版本”；
9. 明确认知 Delta；
10. 给出最多 1～3 个真实动作；
11. 给出 Human Gate 建议。

## 15.3 前台表现不强制模板化

V3 不要求每张卡视觉上都固定为 10 节。

原则：

> **结构是后台思考契约，不是前台八股文模板。**

如果没有商业机会、当前项目关联或反对意见，则不强行显示对应段落。

## 15.4 Markdown 元数据

建议：

```yaml
source: telegram
source_item_id: ...
topics:
  - ai
  - business

value_type:
  - cognition
  - project_impact

cognition_delta: revise
action_state: experiment

related_knowledge:
  - cognition.agent-context
  - project.knowledge-ingest

quality_status: passed
human_gate: pending
```

---

# 16. Cognitive Change Proposal

Deep Insight Card 与长期认知变更必须拆开。

示例：

```yaml
proposal_id: cp-...
change_type: revise

target:
  domain: AI.Agent.Architecture

old_cognition:
  text: "明确情况走规则，模糊情况走生成模型"

proposed_cognition:
  text: >
    规则、专用判定模型和生成模型可以形成三级决策结构；
    是否增加中间判定层取决于调用量、稳定性与经济性。

reason:
  - "新材料显示部分 decision task 可由低成本判定模型承担"

evidence_refs:
  - ...

confidence: 0.82
human_gate: pending
```

规则：

> **任何 Cognitive Change Proposal 在 Human Gate 前都不能写入长期认知库。**

---

# 17. Human Gate

## 17.1 Gate 决策

统一支持：

```text
ADOPT
EXPERIMENT
WATCH
ARCHIVE
REJECT
```

语义：

- `ADOPT`：采纳为长期认知。
- `EXPERIMENT`：不改变长期认知，先进入实验系统。
- `WATCH`：进入观察池，等待更多证据。
- `ARCHIVE`：内容有价值，但不改变认知、不启动实验。
- `REJECT`：不进入长期体系。

## 17.2 批量审批优先

默认全天自动生成卡片，不逐卡打扰；日报 / 固定时段集中展示待处理项。

只有以下情况允许即时提醒：强烈建议立即行动、可能影响当前 active project、高价值且时效性强的商业机会、与已确认长期认知发生重大冲突。

## 17.3 Human Gate 不得被模型伪造

模型只能提出建议，不能代表用户完成长期认知批准。

---

# 18. Quality Critic

## 18.1 职责

主模型完成初稿后，先进入 Critic，不直接交付。

Critic 不重新做完整内容分析，而是检查成品是否退化。

## 18.2 核心检查项

至少检查：

```text
1. 是否主要在复述原文？
2. 是否存在泛泛“对你有启发”？
3. 是否存在硬关联？
4. 是否把作者观点误当事实？
5. 是否真正形成认知 Delta？
6. “我的版本”是否只是换句话说？
7. 行动是否是假行动？
8. 商业机会是否只看收入案例？
9. 是否忽视相关旧认知状态？
10. 是否强行制造反对意见？
```

## 18.3 两个特殊质量测试

### 陌生人测试

遮掉用户身份后，这份卡片能否原封不动发给任何一个喜欢 AI / 商业的人？

如果可以：

```text
personalization = FAIL
```

### 三个月后测试

三个月后的用户能否知道：旧判断是什么、新证据是什么、为什么改变、新判断是什么、当时决定做什么？

如果不能：

```text
cognition_traceability = FAIL
```

## 18.4 修订循环

默认：

```text
主模型初稿
  → Critic
  → PASS → 发布
  → FAIL → 带明确 revision instructions 重写一次
```

默认最大修订 1 次。允许配置为 2，但 V3 初始不允许无限循环。

---

# 19. Insight Store

## 19.1 独立状态库

建议：

```text
/Volumes/ORICO/KnowledgePipeline/insight/
├── state.db
├── evidence/
├── cards/
├── proposals/
├── watch/
└── indexes/
```

## 19.2 不污染 telegram/state.db

`telegram/state.db` 保持 Source Facts only。

Insight Store 保存 candidate decision、Deep-Value decision、Personal Retrieval references、Evidence Pack、thinker run、critic result、card path、cognition proposal、Human Gate 状态、trend / watch 状态。

## 19.3 建议最小表

```text
insight_candidates
insight_runs
insight_context_refs
insight_cards
cognition_proposals
watch_signals
trend_clusters
```

具体字段与 schema migration 留给实施方案。

---

# 20. Deep Insight Card 文件组织

建议：

```text
insight/cards/
└── 2026/
    └── 09/
        └── 28/
            ├── insight-<id>-jev-decision-model.md
            ├── insight-<id>-vertical-tool-economics.md
            └── ...
```

主文件：一篇内容 = 一份独立认知卡。

如果多个弱信号共同形成趋势，则一个 trend cluster = 一份趋势认知卡。

---

# 21. 日报 / 周报重新定义

旧模式：周报本身承载全部深度内容。

V3 模式：日报 / 周报只负责导航。

示例：

```markdown
# 本周 Personal Insight Index

本周：
- 生成 Deep Insight Card：12
- 建议 ADOPT：3
- 建议 EXPERIMENT：2
- WATCH：4
- ARCHIVE：3

## 最值得处理
1. Jev 判定模型 → 可能改变 knowledge-ingest 分类架构
2. 垂直小工具 → 修正“市场太小就不能做”的旧判断
3. 某商业模式 → 数学成立但获客模型不成立

## 等待 Human Gate
...

## WATCH 信号
...
```

《TG 深读》可暂时保留为兼容产物，但不再作为 V3 主交付。

---

# 22. 成本与模型预算

## 22.1 预算原则

```text
大量内容
→ 便宜筛选

少量候选
→ 中等成本判断 + Retrieval

真正高价值
→ 强模型深思 + 一次 Critic
```

## 22.2 Source AI Budget 与 Insight AI Budget 分离

建议新增独立 `Insight AI Budget`，至少分：

```text
candidate_filter
deep_value_gate
retrieval_query_planner
thinking
critic
```

这样未来可以清楚知道钱到底花在“筛选”还是“真正思考”。

## 22.3 Fail-safe

如果强模型不可用：

- 不得退化成廉价摘要并冒充 Deep Insight Card；
- 卡片状态进入 `BLOCKED_MODEL_UNAVAILABLE`；
- 后续可恢复；
- 原始 Source Item 不丢失。

---

# 23. 失败模式与防护

## 23.1 高 Recall 失败
风险：第一层太保守，真正有价值内容被漏掉。

防护：Candidate Filter Recall 优先；禁止自动蒸馏 value-SKIP 永久规则；建立人工历史金标准集。

## 23.2 个性化确认偏误
风险：只因为“用户过去这么想”，新内容就被强行解释为支持旧认知。

防护：Source Claim / Personal Claim / Current Evidence 分开；主模型可修正或推翻旧认知。

## 23.3 硬关联
风险：“你是 CPA，所以任何 AI 都和审计相关。”

防护：每个关联必须解释“为什么会改变当前判断”；否则不进入 Context Pack。

## 23.4 高级摘要退化
风险：输出很长，但仍主要是原文重述。

防护：Quality Critic、Stranger Test、Own-Version Test、Cognition Delta Test。

## 23.5 八股文退化
风险：每张卡机械输出十个固定标题。

防护：思考结构固定；展现结构自适应。

## 23.6 假行动
风险：“持续关注、深入学习、积极探索。”

防护：行动只能是立即行动、建立实验、加入观察、无需行动，最多 1～3 个。

## 23.7 认知污染
风险：模型自动把错误判断写进长期知识。

防护：Cognitive Change Proposal 与 Card 分离；Human Gate 强制；无自动写入长期认知。

---

# 24. 质量指标

## 24.1 Stage 1
核心：`Valuable Idea Recall`，优先于 Precision。

## 24.2 Stage 2
核心：`Deep-Read Precision`，即被送进昂贵思考的内容，最终有多少确实产生了认知 / 决策 / 商业价值。

## 24.3 Personal Retrieval
核心：

```text
Relevant Context Precision
False Personal Link Rate
Stale / Superseded Context Leakage
```

## 24.4 Deep Insight Card
建议人工验收维度：

```text
Source Understanding
Critical Reasoning
Personal Connection
Mechanism Extraction
Own-Version Quality
Cognition Delta
Actionability
Business Opportunity Rigor
Traceability
```

## 24.5 最重要的 Golden Test

建立一批历史材料：

> 用户过去确实单独发给 ChatGPT，并认为回复质量高。

将其作为 Golden Set。

V3 对同类材料运行后，人工比较是否达到过去一对一对话的思考深度。

这比 Rouge / BLEU / 摘要相似度更有意义。

---

# 25. V3 验收 Golden Cases

### G1 认知升级
输入：一篇人生认知文章。
期待：形成新的“我的版本”，不是摘要。

### G2 商业模式质疑
输入：类似“1000 个群 × 每天 5 单”的赚钱模型。
期待：识别 Excel 算术与真实商业模型差异。

### G3 AI / Agent 架构
输入：一个新 Agent 技术观点。
期待：准确关联旧 Agent 判断，并指出是否改变现有系统边界。

### G4 不值得深思
输入：看起来新颖但无机制增量的内容。
期待：ARCHIVE / REJECT，不强行生成宏大启发。

### G5 个人上下文空结果
输入：与已有知识体系真正无关联的新领域。
期待：`NO_RELEVANT_PERSONAL_CONTEXT`，不硬关联。

### G6 旧认知冲突
输入：与 CONFIRMED cognition 相冲突的新证据。
期待：生成 Cognition Change Proposal，不自动覆盖旧认知。

### G7 多个弱信号聚合
输入：3～5 条单独普通但方向一致的内容。
期待：形成 Trend Candidate。

### G8 赚钱机会不适合用户
输入：市场机会真实，但依赖用户不具备的资源。
期待：Market Opportunity 与 Personal Opportunity 分离。

---

# 26. 兼容迁移策略

## 26.1 V2 不原地大改

建议迁移顺序：

```text
V2 Source Runtime
   保持生产运行
        ↓
新增 Insight Lane（旁路）
        ↓
历史/实时 shadow mode
        ↓
与现有 TG 深读对比
        ↓
Golden Set 验收
        ↓
V3 Insight Card 成为主产物
        ↓
旧 TG 深读降级为兼容索引或停止
```

## 26.2 Shadow Mode

V3 初期：

- 不改变 V2 KEEP / SKIP；
- 不改变 K2C 自动链路；
- Insight Lane 只消费派生副本；
- 不自动写长期认知；
- 不影响现有生产 watcher。

这是 V3 最重要的安全迁移原则。

---

# 27. 代码边界建议

设计级建议，不冻结具体文件名。

新增模块倾向：

```text
src/knowledge_ingest/insight/
├── source_view.py
├── candidate_filter.py
├── value_gate.py
├── trend.py
├── retrieval.py
├── context_pack.py
├── evidence.py
├── thinker.py
├── critic.py
├── cards.py
├── proposals.py
├── store.py
├── digest.py
└── ports.py
```

Telegram 侧只新增最薄 Adapter：

```text
src/knowledge_ingest/telegram/
└── insight_adapter.py
```

原则：

> Insight Engine 不进入 `telegram/` 内核。

这样未来 local → insight、web → insight、cloud → insight、telegram → insight 都复用同一套核心。

---

# 28. CLI 设计方向

仅定义产品表面，不冻结最终命令。

候选：

```bash
knowledge-ingest insight candidates list
knowledge-ingest insight cards list
knowledge-ingest insight card show ID
knowledge-ingest insight proposals list
knowledge-ingest insight gate resolve ID --decision ADOPT|EXPERIMENT|WATCH|ARCHIVE|REJECT
knowledge-ingest insight watch list
knowledge-ingest insight digest
knowledge-ingest insight doctor
```

V3 初始仍允许 Telegram 快捷入口，但内部必须转发到通用 Insight Runtime。

---

# 29. 运维与可观测性

`insight doctor` 至少检查：

- Insight Store schema；
- Source View 积压；
- Candidate Filter backlog；
- Deep-Value backlog；
- Personal Retrieval 可用性；
- Personal Knowledge Index freshness；
- blocked model runs；
- Critic fail rate；
- pending Human Gate 数量；
- WATCH aging；
- Card 文件一致性；
- proposal/card 引用完整性。

关键运行统计：

```text
source_items_seen
candidate_true
deep_read
watch
archive
reject
cards_generated
critic_failed
critic_revised
proposals_pending
adopted
experiments_created
```

---

# 30. 隐私与安全

Personal Reasoning Retrieval 会读取比 V2 更敏感的个人长期资料。

因此必须：

1. 只读访问默认开启；
2. Personal Knowledge Writer 与 Retrieval 分离；
3. Human Gate 前禁止写入；
4. Evidence Pack 只保存必要片段与引用，不默认复制全部历史聊天；
5. 模型调用日志不得记录完整个人长期知识；
6. debug 日志只记录 ref / hash / decision，不记录敏感正文；
7. 任何外部模型 provider 都必须明确数据边界；
8. 后续如涉及审计客户敏感资料，必须单独设计 local-only / redaction 策略，不能默认上传。

---

# 31. 冻结产品原则候选

用户批准本设计后，建议冻结以下原则：

```text
1. V3 不推翻 V2 Source Runtime。
2. Personal Insight Engine 为源无关派生层。
3. 第一层 High Recall，宁可多收，不漏重要内容。
4. 第二层保护昂贵智能预算。
5. “文章质量”不是“个人价值”的同义词。
6. 反驳价值也是价值。
7. 多个弱信号允许聚合触发深读。
8. Personal Retrieval 找“影响判断的个人证据”，不是主题相似文本。
9. 允许 NO_RELEVANT_PERSONAL_CONTEXT。
10. 旧认知不是事实，可被新证据修正或推翻。
11. Deep Insight Card 是主产物。
12. 日报/周报只做索引和回顾。
13. 每篇通过深读的内容必须形成“我的版本”。
14. 必须显式输出 Cognition Delta。
15. 商业机会必须拆 Market Opportunity 与 Personal Opportunity。
16. 主模型必须先理解、再质疑、再连接、再重构。
17. Quality Critic 独立复核，默认最多修订一次。
18. Stranger Test 与三个月后 Traceability Test 作为硬质量门。
19. 深度加工可自动化。
20. 长期认知变更必须 Human Gate。
21. Human Gate 默认批量审批，即时提醒仅用于高优先级例外。
22. Insight Store 与 Telegram Source Facts 分离。
23. Insight Lane 与 K2C Capability Lane 并行，不互相替代。
24. 强模型不可用时 fail closed，不得用普通摘要冒充深度认知卡。
25. Golden Set 以过去用户认可的“一对一 ChatGPT 深度回复”为质量基线。
```

---

# 32. 成功标准

V3 成功，不是因为系统生成了更多文字。

真正的成功标准是：

> **当用户回看一张卡时，他能明显感到：这不是“别人文章的摘要”，而是这篇材料已经和自己的旧认知、现实项目与约束发生过一次真正碰撞。**

更进一步：

> **即使以后忘记原文，仍然保留下来一条属于自己的判断原则、一个值得验证的实验，或者一个经过现实拆解的商业机会。**

最终目标不是：

```text
更多知识
```

而是：

```text
更好的判断
+
更少的重复思考
+
更快的认知积累
+
更可信的行动
```

---

# 33. 设计审阅 Gate

本文件当前状态：

```text
DRAFT FOR USER REVIEW
```

本阶段只确认：

- 产品目标；
- 架构边界；
- 数据流；
- 核心对象；
- Human Gate；
- 质量标准；
- 迁移原则。

**不在本阶段冻结：**

- 具体模型供应商；
- Prompt 最终全文；
- SQLite 具体字段；
- CLI 最终名称；
- Milestone；
- 测试文件清单；
- 代码改动文件清单；
- 预算具体数值；
- LaunchAgent / 调度参数。

这些内容应在本设计被用户批准后，结合真实本机仓库、当前代码和运行环境，由实施方案阶段确定。

---

# 34. 下一阶段

用户批准本设计文档后，下一阶段才进入：

> **V3 实施方案**

实施方案需要：

1. 读取当前 `main`；
2. 读取全局 / 项目 `AGENTS.md`、README、现有 V2 设计/验收；
3. 只验证与 V3 直接相关的本机事实；
4. 明确复用点与新模块边界；
5. 设计 schema migration；
6. 设计 Golden Set；
7. 设计 TDD / Milestone；
8. 设计 shadow mode 与回滚；
9. 设计模型端口和预算；
10. 完成后 STOP，等待用户批准实施。
