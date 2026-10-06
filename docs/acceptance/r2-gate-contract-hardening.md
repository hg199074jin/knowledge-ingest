# R2 Deep-Value Gate Contract Hardening（CAL-1）

> **Verdict: implemented.** 第一轮 payload 已携带权威 output contract；parser / first
> prompt / repair 共用同一 contract semantics；违规原因变成有限、sanitized、可统计的
> stable code；repair 退化为异常兜底且仍然**最多一次**。
>
> **业务语义零变更**、**无 DB migration**、**未 redeploy production**。

## 1. Root cause（CAL-1）

R2 前 `DeepValueGate.evaluate()` 第一轮 payload 只有 dimensions / decision_states /
contradiction_policy / source facts / candidate summary，**没有正式 output_contract**。
真正严格的契约只在模型首次违约后由 `CONTRACT_REPAIR_INSTRUCTION` 补发：

> 第一轮让模型"猜结构"，猜错后才告诉它完整结构。

M10-R 的一次有界 repair 把历史 malformed payload 从 blocker 修回，但 CAL-1 保留结论是：

> repair 是安全网，不应成为正常 gate 调用的常态。

结构性根因因此不在 repair 逻辑，而在**契约前置缺失**。

## 2. First-attempt authoritative contract

新增 `deep_value_output_contract()`，第一轮 payload 直接携带：

```json
{
  "type": "object",
  "required": ["decision"],
  "decision": ["DEEP_READ", "WATCH", "ARCHIVE_ONLY", "REJECT"],
  "dimensions": {
    "novelty": ["low", "medium", "high", "unknown", null],
    "cognitive_delta_potential": ["low", "medium", "high", "unknown", null],
    "business_potential": ["low", "medium", "high", "unknown", null],
    "project_relevance": ["low", "medium", "high", "unknown", null],
    "transferability": ["low", "medium", "high", "unknown", null],
    "evidence_quality": ["low", "medium", "high", "unknown", null],
    "contradiction_value": ["low", "medium", "high", "unknown", null],
    "thinking_space": ["low", "medium", "high", "unknown", null]
  },
  "reason": "string (optional, may be empty)",
  "rules": ["decision 必须存在，且只能取 decision 列出的字面量", "..."]
}
```

Normative semantics：`decision` hard-required 且只允许四态；维度**缺失仍为合法 → None**
（未借 R2 强制 required，冻结契约
`test_missing_dimensions_default_none_not_fabricated` 继续成立）；
`contradiction_value="unknown"` 合法；`reason` 可空；只允许 JSON object
（未知额外字段**忽略**，不引入无价值脆弱性）。

## 3. Single contract source

契约语义只有一处定义，三处消费：

| 消费者 | 使用方式 |
|---|---|
| first-attempt payload | `payload["output_contract"] = deep_value_output_contract()` |
| repair instruction | `build_contract_repair_instruction(contract)` —— 由同一 contract 构造 |
| parser | `DEEP_VALUE_STATES` + `DIMENSION_VALUES` + `_DIMENSIONS` —— 同一批常量 |

新增 `DIMENSION_VALUES = ("low","medium","high","unknown")` 与
`deep_value_output_contract()`，取代原先散落在 prompt 字符串与 parser 里的手写四态/八维。

## 4. Violation taxonomy

`GateContractViolationCode`（有限、sanitized）：

| code | 触发 |
|---|---|
| `NOT_OBJECT` | 非 dict，或 port 层 `ModelBadOutputError`（stdout 无完整 JSON / 散文 / fence） |
| `MISSING_DECISION` | 缺 `decision` 字段 |
| `INVALID_DECISION` | `decision` 不在四态内 |
| `INVALID_DIMENSION_VALUE` | 维度取值不在 enum 内（字段名来自冻结常量） |

`GateContractViolation(ModelBadOutputError)` 携带 `.code` / `.field`；
`field` **只接受冻结字段名**，绝不携带模型返回的取值或 source 正文
（专测断言非法取值 marker 不出现在 provenance 与 repair payload 中）。

## 5. Repair semantics（未放宽）

```
attempt 1 invalid → repair attempt 1 → valid => success
                              → invalid => GateContractViolation(BLOCKED_MODEL_BAD_OUTPUT)
```

- 恰好一次（`MAX_CONTRACT_REPAIRS = 1`），禁止无限重试；
- parser **不猜** decision，不把 malformed WATCH/DEEP_READ 自动归一；
- **transient / model execution error 不触发 repair**：`ModelTimeoutError` /
  `ModelCommandFailedError` / `ModelEmptyOutputError` / `ModelNotConfiguredError`
  直接向上抛、只调用一次（4 类各有专测）；
- repair payload 携带：同一份原始业务输入 + 权威契约 + stable violation code +
  repair instruction；**不携带第一轮 raw output**（旧 `previous_violation` 自由文本
  字段已移除），专测断言 repair payload 中不含首轮输出 marker。

## 6. Provenance semantics

| 情形 | `contract_repair` |
|---|---|
| 首轮合法 | `None`（absence = first-valid） |
| repair 成功 | `{"first_attempt_valid": false, "repair_attempted": true, "repair_result": "repaired", "first_violation_code": "<CODE>"}` |
| repair 耗尽 | 不持久化决策；抛 stable code，`insight_runs` 落 blocked |

字段 `first_violation`（自由文本）→ `first_violation_code`（stable code）。
**无 schema migration**：该字段是已有 `decision_json` 内的自由 dict 键。

## 7. Service error-code

`InsightService` 的 deep-value 契约耗尽现在写入**可直接查询**的稳定 code：

```sql
WHERE stage='deep_value_gate' AND error_code='BLOCKED_MODEL_BAD_OUTPUT'
```

原先 `error_code=f"{code}: {str(exc)[:100]}"` 会把自由异常文本混入 identity；R2 改为
`getattr(exc, "code", None)` 优先，transient 仍是 `MODEL_TRANSIENT_ERROR`（同样不再带
自由文本尾巴）。**未改任何 run 表 schema**（`insight_runs` 仍有 `error_code`、无 `detail` 列）。

## 8. Historical read-only CAL-1 baseline（production V3 state.db）

查询以 SQLite `mode=ro` 执行；查询前后 DB `mtime`/`size` 完全一致（**未写入**）。

| 指标 | 值 |
|---|---|
| total value-gate decisions | **37** |
| first-valid（absence） | **33** |
| repaired-success | **4** |
| blocked after repair | **0** |
| historical repair rate | **4/37 ≈ 10.8%** |
| unclassifiable（malformed decision_json） | 0 |

decision 分布：`WATCH 24` / `ARCHIVE_ONLY 7` / `DEEP_READ 6`。

**stable code 维度 = UNKNOWN**：4 条历史 repaired 记录写于 R2 之前，只有自由文本
`first_violation`（4 条均为 `missing decision field`），没有 `first_violation_code`。
文本上与新 code `MISSING_DECISION` 一致，但**按纪律不回填伪数据** → R3 只能把它当
"UNKNOWN，文本一致"，不能计入 code 级统计。

历史 `error_code` 也不存在 `BLOCKED_MODEL_BAD_OUTPUT` 形态（该表本轮 0 blocked），
故 §10 的新稳定 code 是**前向**契约。

样本量小（n=37），**R2 不据此调整任何 gate 阈值**；这些数字只作为 R3 的 contract-health
基线。

## 9. Tests

新增 24 例（R2 → 1129 passed / 1 skipped）：

- **first attempt**：payload 含权威契约（required/四态/八维 enum/`null` 合法）、
  合法首轮恰好 1 次调用、三处同源、缺失维度仍 None、额外未知字段被忽略、
  contradiction unknown 合法
- **taxonomy**：missing/invalid decision、非法维度枚举、非 object、port 级 bad output
  → 对应 stable code + `violation_field`
- **repair**：payload 无 raw model output、无旧 `previous_violation`、同源契约、
  指令含四态与维度值域、第二次仍坏 → 有界耗尽且 code 稳定、恰好 2 次调用
- **transient**：4 类 ModelPortError 各自**不 repair、只调用一次**
- **service/store**：契约耗尽落 `error_code='BLOCKED_MODEL_BAD_OUTPUT'` 且可直接查询、
  repaired 决策与 provenance 落库、无自由文本泄漏、首轮合法无 provenance、
  restart 不重复调用 gate、transient 记 `MODEL_TRANSIENT_ERROR`、无 schema migration

既有 `test_insight_gate_repair.py` 断言的是**旧契约**（`first_violation` 自由文本），
已按 §6/§8 更新为 stable code —— 这是本轮刻意的契约变更，非回归。

## 10. Scope proof

改动文件仅：`insight/value_gate.py`、`insight/service.py`（error-code 一处）、
`tests/unit/test_insight_value_gate.py`、`tests/unit/test_insight_gate_repair.py`、
`tests/unit/test_insight_service.py`、本文件。

未触碰：candidate filter recall policy、R1 isolation / Codex profile / shadow agent、
Telegram V2、critic、thinker、R3 阈值、R4 rubric、store schema。

## 11. Production 未改动

- production runtime 仍 pinned `00c2505` + `r1-5-prod-v1 @ HOSTILE_SMOKE_PASS`
- 未 redeploy / 未重启 V3 / 未改 production plist、profile、Codex
- 未重跑 R1 N1–N5（R2 未触及 isolation / model execution boundary）
- R2 只产出 canonical dev code + tests + CAL-1 baseline；是否 rollout 由后续决定

## 12. Residuals

- **历史 provenance 无 stable code**：R3 的 code 级统计只能从 R2 上线后的数据开始；
  历史 4 条为 UNKNOWN。
- **历史 `error_code` 格式**：R2 之前该列带自由文本尾巴（`CODE: detail`）；R2 之后为纯
  stable code。跨版本查询需注意。
- **样本量**：production n=37 极小，且与 M10 §3 记录的 32–36% 首过失败率来自不同总体
  （不同频道/样本）。两者不可直接比较；R3 需在自己的口径下重建基线。
- **`NOT_OBJECT` 归并**：port 层"无完整 JSON object"与 parser 层"非 object"共用同一
  code。当前 CLI 无法区分二者（port 在 parser 之前抛错）；若 R3 需要区分，需 port 层
  暴露更细的分类，本轮未做。
- **CLI 文案陈旧**（R1.5 遗留）：`shadow-agent uninstall` 仍提示 "unload is manual"，
  与实际已执行 bootout 不一致。仅提示性，不影响安全。
- R1 全套 residuals 继续有效：WEBFLAG-DEPRECATION、MODEL-CMD-PARSE-ERROR、
  N3 deny code 134 绑定本机 sandbox、V2 proxy 运维耦合。