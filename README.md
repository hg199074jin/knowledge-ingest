# knowledge-ingest

[![Tests](https://github.com/hg199074jin/knowledge-ingest/actions/workflows/tests.yml/badge.svg)](https://github.com/hg199074jin/knowledge-ingest/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Release](https://img.shields.io/github/v/release/hg199074jin/knowledge-ingest)](https://github.com/hg199074jin/knowledge-ingest/releases)

[简体中文](README.zh-CN.md)

A multi-source knowledge ingestion and capability-distillation pipeline for
macOS (Apple Silicon). It routes material from **Telegram channels, Baidu
Netdisk, Quark Drive, or local paths** through the existing toolchain:

```text
media-transcriber (video/audio → Markdown)
docchunk (long documents → verifiable Corpus)
cangjie-skill (methodology → executable Agent Skills)
personal-capability-distiller (capability cards / SOPs / prompts → Obsidian)
```

`knowledge-ingest` itself does **no** OCR, ASR, chunking, or distillation —
it provides orchestration, durable state, resumable breakpoints, dedup,
caching, human confirmation gates, and final reporting.

```text
Telegram channels → filter → materialize ─┐
Baidu / Quark / local material → Corpus ───┴→ Agent Skills + capability assets (k2c target)
```

## How it works

```text
Cloud skills (baidu-drive / quarkclouddrive) download into the job's source dir
  → Source Handoff (canonical source.json)
  → file-type router (documents / media / unsupported)
  → media transcription (cached by content hash + tool revision)
  → Document Set (symlinks + provenance map, never merged into one blob)
  → docchunk split → verify PASS (hard gate)
  → cangjie-skill → personal-capability-distiller (serial, human gates)
  → final report (secrets-redacted)
```

Key guarantees:

- **`docchunk verify` FAIL blocks distillation** — structurally enforced by the
  explicit state machine, not by convention.
- **Human confirmation gates are never fabricated.** Gate resolutions can only
  come from a real user reply, and every gate event is recorded.
- **No credentials anywhere.** Cloud tokens/cookies are never read; free-text
  prompts and reports pass through a redactor; event logs are structured and
  sanitized.
- **Dedup across sources.** Same content from Baidu and Quark reuses the same
  verified corpus (handoff fingerprint + tool HEAD + config in the cache key).
- **Resumable.** Every stage transition is atomically persisted; `next` always
  returns the exact remaining action, never a re-run of completed stages.

## Install

```bash
git clone git@github.com:hg199074jin/knowledge-ingest.git
cd knowledge-ingest
uv sync
uv run knowledge-ingest doctor --config ./config.example.yaml
```

Optional CLI wrapper (`~/.local/bin/knowledge-ingest`):

```zsh
#!/bin/zsh
cd /path/to/knowledge-ingest || exit 1
exec uv run knowledge-ingest "$@"
```

## Quick start

```bash
knowledge-ingest job create --provider local --source /path/book.pdf \
  --target cangjie [--with-router [family_router]]
knowledge-ingest source init JOB --local-path DIR [--remote-path ...]  # builds handoff
knowledge-ingest source register JOB --handoff .../handoff/source.json
knowledge-ingest route JOB
knowledge-ingest preprocess JOB       # long task: background; per-file progress in status
knowledge-ingest status JOB / resume  # N/M progress; resume = next step for every job
knowledge-ingest next JOB --json
knowledge-ingest distill prepare JOB --target cangjie   # scaffolds distill workspace
knowledge-ingest target start JOB --target cangjie
knowledge-ingest gate enter|resolve JOB --target cangjie --name GATE [--decision ...]
knowledge-ingest gate preauthorize JOB --target cangjie --name GATE --value VALUE  # whitelisted gates only
knowledge-ingest budget acquire|outcome|amend JOB --target T [...]    # two-phase external-call budget
knowledge-ingest target checkpoint|resume JOB --target T [...]        # checkpoints / explicit BLOCKED recovery
knowledge-ingest target complete JOB --target cangjie --output-path PATH [--pipeline-state PATH]
knowledge-ingest watchdog install|status|uninstall                    # reboot watchdog (LaunchAgent)
knowledge-ingest report JOB           # includes the audit section
```

As an Agent Skill, the natural-language entry points are: *"turn this Baidu
Netdisk PDF into a skill"*, *"learn this Quark course and distill it"*, *"continue
the ingestion task"*, *"where is that job at?"* — see [SKILL.md](SKILL.md).

### Telegram source (quick start)

```bash
knowledge-ingest telegram watch --config ./config.example.yaml   # always-on watcher
knowledge-ingest telegram watch-agent install    # launchd wrapper (RunAtLoad + KeepAlive)
knowledge-ingest telegram doctor                 # 18-check read-only health audit
knowledge-ingest telegram review list            # human queue for uncertain items
knowledge-ingest telegram review resolve ID --decision SKIP|KEEP|DOWNLOAD_ONCE
knowledge-ingest telegram classify-dryrun [--limit N]   # read-only LLM channel validation
knowledge-ingest telegram rules-distill [--source llm] [--apply]  # distill SKIP verdicts into fingerprint rules
knowledge-ingest telegram rules-list / rules-disable ID      # audit / rollback learned rules
knowledge-ingest telegram budget show / reset    # per-source LLM call ledger
knowledge-ingest telegram digest [--send]        # collection digest (3x-daily agent)
knowledge-ingest telegram handoff ITEM_ID        # explicit item → k2c capability handoff
```

## Repository layout

| Path | Purpose |
|---|---|
| `src/knowledge_ingest/` | Python CLI (stdlib + PyYAML + pydantic, managed by uv) |
| `src/knowledge_ingest/telegram/` | Telegram Source V2 runtime: watcher, classification, learned rules, review queue, digest/notification, doctor |
| `SKILL.md` | Agent Skill entry point |
| `references/` | Architecture, routing, cloud sources, target gates, recovery, handoff contracts |
| `schemas/` | Source/Target handoff contract examples |
| `docs/` | Design v1.1 + Telegram V2 design/implementation/acceptance, runtime inventory |
| `tests/` | Unit + integration tests, 562 green (TDD throughout) |

## v0.2.0 — evolved from the first production run

The first end-to-end job (11 GB / 32 videos / two overnight reboots) drove five upgrades:

- **Per-file progress persistence**: `status` shows real N/M during hours-long ASR;
  every file emits a `media_transcribed` event (survives crashes).
- **`resume` command**: one call reports the next action for every job and
  `--exec` continues the first resumable one — reboot survival is built-in.
- **Watchdog installer**: `knowledge-ingest watchdog install|status|uninstall` generates the reboot watchdog (ki-resume.sh + LaunchAgent plist) with dynamic paths from your environment — the hand-built script from the production run, productized; ops/ templates remain as archived samples.
- **`job amend --add-target`**: guarded by a pidfile lock so manifest edits are
  refused (not silently lost) while preprocess holds its in-memory copy.
- **`source init` / `distill prepare`**: handoff JSON and distill-workspace
  scaffolding are generated, not hand-authored.

## v0.3.0 — generic target runtime + budget guard

- **Generic Target Runtime**: targets are data, not code paths. `cangjie`,
  `personal`, and the new `family_router` (depends_on `[cangjie]`) register in
  one registry; the dependency graph, serial scheduling, per-target
  checkpoints, and transactional output manifests are uniform.
- **Budget Guard** (protocol-level, cooperative): two-phase
  `budget acquire` / `budget outcome` with idempotent permits, per-target
  quotas, per-host breakers, and per-case retry caps. KI does not intercept
  arbitrary host-side external calls; a BLOCKED target is only ever lifted by
  an explicit `target resume` — `budget amend` never auto-restores.
- **Gate preauthorization**: whitelisted operational gates (cangjie
  `stage5_install_location`, family_router `cost_budget_confirmed`) accept
  day-time grants (`gate preauthorize`) that overnight runs reference with
  `gate resolve --preauthorization` — values still come from the real user.
  Knowledge gates stay live-only and reject grants by design.
- **Three-bucket transcription accounting**: every media output records
  `transcribed` / `cache_reused` / `unknown`; v1-era history stays `unknown`
  and is never back-filled.
- **Strict UTF-8 encoding preflight**: text files are streamed and verified
  before preprocess; violations BLOCK with the detected encoding — KI never
  transcodes or guesses.
- **Watchdog installer**: `watchdog install|status|uninstall` productizes the
  reboot watchdog with fully dynamic paths (now also documented in the
  Chinese README).
- **Audit report section**: `report` renders a 运行审计 section — stage
  durations, transcription accounting, budget usage, and honest
  "unknown / not instrumented" markers, all sourced from existing manifest
  fields (v1-migrated manifests render safely).

## Telegram Source V2 — an always-on knowledge channel

The Telegram source turns channel noise into a curated, auditable knowledge
stream that feeds the same Corpus → capability pipeline:

```text
Telegram channels (live updates + periodic reconcile)
  → deterministic item aggregation (albums/collections merge; edits & deletes tracked)
  → three-layer classification:
      1. static rules (obvious ads / whitelist — zero LLM calls)
      2. learned fingerprints (distilled SKIP verdicts; NFKC-normalized so
         emoji/spacing/full-width obfuscation still matches — zero LLM calls)
      3. injectable LLM for boundary cases (any stdin→stdout CLI; per-source
         budget ledger, empty/rate-limit breakers, fail-safe → human REVIEW)
  → KEEP: materialized Markdown per item (provenance preserved)
  → PDF/video attachments: SHA-256-verified download, ASR via media-transcriber
  → handoff → knowledge-ingest job → docchunk Corpus → k2c target
    (staged capability; publish/activate stays a human gate)
```

Design guarantees:

- **Fail-safe, never silent**: parse errors, unknown enums, channel failures,
  and exhausted budgets all degrade to REVIEW (a human queue) — knowledge is
  never silently skipped to save cost.
- **Review decisions actually act**: `review resolve` materializes KEEP items
  and terminalizes SKIP ones (no write-only queue); every decision is
  auditable in `classifier_audit`.
- **Learned rules are reversible**: fingerprints distilled only from
  high-confidence SKIP verdicts (or human rulings), never from KEEP;
  `rules-disable` rolls any rule back individually.
- **Ops-first**: `telegram doctor` runs 18 read-only checks (session, schema,
  cursors, reconcile freshness, file integrity, backlog, delivery, agents);
  digest agent summarizes 3× daily; WxPusher notifications are write-ahead,
  idempotent, and retry-safe.
- **Crash-proof state**: SQLite WAL + `user_version` schema migrations under
  maintenance locks; atomic materialization; watcher survives `kill -9` and
  reboots via launchd.

Production-proven on 14 channels (~300 items/day): eight review rounds,
dual acceptance records (A–W matrix, 25 checks), and a real end-to-end
sample (SCMP 10.87 MB PDF → staged k2c capability).

## Docs

- [Design document v1.1](docs/knowledge-ingest-design-v1.md)
- [Implementation plan v1.1](docs/knowledge-ingest-implementation-v1.md)
- [Acceptance record](docs/acceptance-v1.md)
- [Telegram Source V2 design (frozen)](docs/k2c-telegram-knowledge-source-v2-design.md)
- [Telegram Source V2 implementation plan](docs/k2c-telegram-knowledge-source-v2-实施方案.md)
- [Telegram Source V2 acceptance record](docs/k2c-telegram-knowledge-source-v2-验收记录.md)


## Related projects

- [docchunk](https://github.com/hg199074jin/docchunk) — verifiable long-document corpora
- [media-transcriber](https://github.com/hg199074jin/media-transcriber) — local ASR to structured Markdown

## License

[MIT](LICENSE)
