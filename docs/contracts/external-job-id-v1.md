# external-job-id-v1（`ki-external-job-v1`）

> Issue #35 / GPT-KI-TAD-IDEMPOTENCY。跨仓库幂等 Job API。
> **本功能为 opt-in：不带 `--external-id` 的旧行为完全不变。**

## 0. 问题

拆分前 Telegram handoff 与 KI 共享进程与文件系统，能靠「先写 `job_id`
再注册」拿幂等。跨仓库后这条链断了：

```
job create  →  KI 落盘 job.yaml、job_id 生成（含 uuid4）
               ↓  ← 崩溃点：调用方还没拿到 job_id
             调用方记录 job_id
```

`job_id` 形如 `20261009-101500-telegram-abc123-f0e1d2`，后 6 位是
`uuid.uuid4().hex[:6]` —— **随机、不可推导**，因此重试既无法定位孤儿 job，
也会**重复创建**。

本协议用**确定性 job_id** 消除该窗口：同一 external-id 在任何时刻都指向
同一 job 目录。

## 1. 确定性 job_id

```python
external_job_id(external_id) -> "ext-v1-" + sha256(external_id)[:32]
```

- 纯函数、路径安全（仅小写十六进制 + 连字符）、定长、**不可逆**；
- **绝不**把 user-controlled 串拼进路径（不是 `slugify` 复用）；
- 同一键 → 同一目录 → create/lookup 都可重放。

## 2. CLI

### 2.1 create（幂等）

```bash
knowledge-ingest job create \
  --provider telegram --source <item-id> --target k2c \
  --external-id 'tg:<source_id>:<item_id>:<fingerprint>'
```

- 首次创建 / 重试 / 并发 → **同一个 `job_id`**，只存在一个 job 目录；
- stdout 仍是**纯 job_id**（一行），向后兼容既有调用方；
- 同键不同意图（provider/source/targets/prompt 任一不同）→ `EXTERNAL_ID_CONFLICT`，
  非零退出，**不**隐式复用、**不**创建第二个 job；
- 不带 `--external-id` → 旧行为（时间 + provider + source + 随机 uuid 后缀）。

### 2.2 lookup（只读）

```bash
knowledge-ingest job lookup --external-id '<key>' [--json|--plain]
```

`--json`（默认）stdout：

```json
{"contract_version": 1, "external_id": "tg:...", "found": true,
 "job_id": "ext-v1-...", "status": "CREATED"}
```

未找到：

```json
{"contract_version": 1, "external_id": "tg:...", "found": false}
```

**不变量**

- 只读：**绝不**执行 schema migration、创建 manifest、touch mtime、推进 Job；
- 输出只含 `found` / `external_id` / `job_id` / `status` / `contract_version`
  —— **不含**原材料或私密 provenance；
- 旧 job（无 external-id）→ `found: false`（清晰的无映射，不是错误）；
- 结果可复算：同一键同一状态，重复调用输出一致。

### 2.3 capabilities（能力自证）

```bash
knowledge-ingest capabilities --json
```

```json
{"contract_version": 1, "external_lookup": true, "idempotent_create": true,
 "lookup_json_fields": ["found", "external_id", "job_id", "status",
                        "contract_version"],
 "protocol": "ki-external-job-v1", "register_idempotent": true}
```

客户端应**解析这个 JSON** 判断能否启用自动投递 —— `--help` 子串命中
不构成语义证据（历史上 Bridge 就栽在这里）。

## 3. source register 幂等

```bash
knowledge-ingest source register <job-id> --handoff <path>
```

| 场景 | 行为 | exit |
| --- | --- | --- |
| 首次注册（job 为 CREATED） | 正常注册并推进 | 0 |
| **相同 handoff 重放**（ACK 丢失） | `ALREADY_REGISTERED`，**严格零写入**（bytes / mtime / events 全不变） | 0 |
| 同 job 不同 handoff | `HANDOFF_CONFLICT`，**不覆盖**旧记录 | 6 |
| job 已推进 / BLOCKED | `HANDOFF_CONFLICT`，**不复活、不重置** | 6 |

### 并发原子性（Issue #37）

判定与写入在**同一个 per-job flock 临界区**内完成，并且**锁内重读** manifest
后才做最终判断。早期实现把判定放在锁外、写入时又不重检 —— 并发 A/B 都读到
`CREATED + empty source` 时，败方会覆盖胜方的 handoff，或在已推进状态上继续
`_advance`。

零写入重放通过 `ManifestStore.locked()` + `LockedSession.commit()` 实现：
**不 commit 就不落盘**。不能沿用 `edit()`——后者在 contextmanager 退出时
无条件 `save()`，会改动 mtime / bytes / events。

### handoff 等价契约

`handoff_identity` 取 handoff 的稳定 canonical 字段（不含任何正文）：

`schema_version`、`provider`、`remote.{id,name,size_bytes,mtime,path}`、
`local_path` 是否存在、`download_completed`、
`provenance.{source_id,chat_id,message_ids,sender_id,first_message_at,last_message_at,source_deleted}`

规则：

- **排除**下游派生的 `source_fingerprint`（注册时本地 handoff 还没有它）；
- 用 `local_path` 的**存在性**而非绝对路径本身参与身份（路径随环境变化）；
- 递归规范化，使 producer 的键序/序列化差异不影响等价性；
- 因此 `size_bytes` / `mtime` / `source_deleted` / `message_ids` / `chat_id`
  的变化都会被正确识别为**不同** payload。

> 已知边界：若同一路径的**内容**变化而 size 与 mtime 均不变，该 handoff 本身
> 未携带任何可区分信息，无法判为不同 payload。调用方（如 Telegram Bridge）
> 应保证内容变化时至少有一个稳定字段随之变化。

## 4. 原子性与受控状态

```
flock(jobs_root/.external-locks/<ext-job-id>.lock)      跨进程互斥
  └─ job.yaml 存在？
       ├─ 是 → 读；意图一致？→ 返回既有 job
       │        意图不同？→ EXTERNAL_ID_CONFLICT
       │        不可解析？→ INCOMPLETE_IDEMPOTENT_JOB
       └─ 否 → 目录已存在？
                ├─ 是 → marker 证明归属？
                │        ├─ 是 → 有界恢复（重建子目录 + 落 manifest）
                │        └─ 否 → INCOMPLETE_IDEMPOTENT_JOB（绝不覆盖/删除）
                └─ 否 → mkdir → 写 marker(O_EXCL, fsync) → 落 manifest
```

- 锁根是 **non-job** 目录，不占用任何 job 目录；
- **归属证明**（marker）用 `O_EXCL` + `fsync` 原子创建，内容是
  `sha256(external_id)[:16]` 与协议版本；
- 遇到无法证明归属的半成品目录 → `INCOMPLETE_IDEMPOTENT_JOB` 并 STOP，
  **绝不**覆盖或删除未知用户内容；
- 无 DB migration；沿用 `atomic_write_text` / `flock_ctx` 既有纪律。

## 5. 稳定错误码与 exit code

| 码 | exit | 含义 |
| --- | --- | --- |
| `EXTERNAL_ID_CONFLICT` | 4 | 同键已绑定不同意图 |
| `NOT_FOUND` | 3 | lookup 未找到（或旧 job 无外部键） |
| `INCOMPLETE_IDEMPOTENT_JOB` | 5 | 半成品/不可解析，拒绝覆盖 |
| `HANDOFF_CONFLICT` | 6 | 同 job 不同 handoff，或 job 非 CREATED |
| `ALREADY_REGISTERED` | 0 | 幂等早退（成功） |

错误消息**只含哈希短指纹**，绝不回显完整 external-id / 来源原文。

## 6. external-id 语义

格式由 Telegram 侧产生：`tg:<source_id>:<item_id>:<fingerprint>`。
**KI 只把它当 opaque string 保存与哈希**，不解析 Telegram 私有字段。
它不是凭据，但仍可能关联来源 —— 日志与报告只输出哈希短指纹。

## 7. 兼容性

| 场景 | 保证 |
| --- | --- |
| 旧 Job 读取 / 写入 | 完全不变 |
| 旧 `job create`（无 `--external-id`） | 随机 job_id，行为不变 |
| 旧 V1/V2 manifest | `external_id` 读入为 `None`，无需迁移 |
| `JobManifest.schema_version` | **未改动**（仍为 2） |
| 生产 V2 DB | **未触碰**；生产 pin 仍为 `00c2505` |

## 8. 不保证什么

本协议保证**同一 external-id 的 create/lookup 在崩溃与并发下不产生重复
Job**。它**不**保证：

- Telegram Bridge 真的做了 lookup 恢复（那是 `telegram-auto-distill` #2 的事）；
- 生产自动投递已启用 —— 那仍需独立的集成验收与 Human Gate。