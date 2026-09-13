"""先测后改（characterization）：统一 build_media_cache_key 不得改变现有键。

v0.3 A3：cli.py 查询侧与 adapters/media.py put 侧要统一为 cache.py 的单一函数
build_media_cache_key。本测试用固定输入（config_sha/glossary/hotwords 均为 None
——与两侧现状一致）断言三键一致：

  1. cli.py _cmd_preprocess 查询侧内联参数算出的 key
  2. media.py transcribe() put 侧实际算出的 key
  3. 新统一函数 build_media_cache_key(...) 的 key

若统一前三者已一致，统一后必须保持一致（现有缓存零失效）；
若发现不一致，按冻结规格停止并报告，不得让现有缓存失效。
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from knowledge_ingest.adapters.media import MediaAdapter
from knowledge_ingest.cache import build_media_cache_key
from knowledge_ingest.cache import build_transcript_cache_key
from knowledge_ingest.runner import CommandResult

# 与 cli.py 查询侧 / media.py put 侧现状一致的固定输入
# （source_sha256 由 put 侧对真实文件指纹计算后回填给另两侧，保证三侧输入一致）
FIXED_MT_HEAD = "096e9e385c7885f0704788884571256cf1000ae3"
FIXED_DEVICE = "auto"
FIXED_TIMESTAMP = "10m"


def cli_query_side_key(source_sha256: str) -> str:
    """cli.py _cmd_preprocess 查询侧的内联参数（config_sha/glossary/hotwords 恒 None）。"""
    return build_transcript_cache_key(
        source_sha256=source_sha256,
        mt_head=FIXED_MT_HEAD,
        config_sha=None,
        device=FIXED_DEVICE,
        timestamp=FIXED_TIMESTAMP,
        glossary=None,
        hotwords=None,
    )


def media_put_side_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, str]:
    """adapters/media.py transcribe() put 侧实际算出的 cache_key（config_file=None）。

    返回 (cache_key, source_sha256)——source_sha256 回填给另两侧，保证三侧输入一致。
    """

    def fake_run(argv, cwd, timeout=None):
        output_root = tmp_path / "output"
        md = output_root / "lesson01" / "lesson01.md"
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text("# 转写内容", encoding="utf-8")
        (output_root / "lesson01" / "metadata.yaml").write_text(
            "source: x\n", encoding="utf-8")
        now = datetime.now(timezone.utc)
        return CommandResult(argv=tuple(argv), returncode=0, stdout=str(md),
                             stderr="", started_at=now, ended_at=now)

    monkeypatch.setattr("knowledge_ingest.adapters.media.run_checked", fake_run)
    monkeypatch.setattr(
        "knowledge_ingest.adapters.media.MediaAdapter.head",
        lambda self: FIXED_MT_HEAD,
    )
    adapter = MediaAdapter(project=tmp_path / "media-proj",
                           output_root=tmp_path / "output")
    source = tmp_path / "lesson01.mp4"
    source.write_bytes(b"v")
    result = adapter.transcribe(source, device=FIXED_DEVICE,
                                timestamp=FIXED_TIMESTAMP)
    return result.cache_key, result.source_sha256


def unified_key(source_sha256: str) -> str:
    """新统一函数 build_media_cache_key（cli 查询侧与 media put 侧统一后的入口）。"""
    return build_media_cache_key(
        source_sha256=source_sha256,
        mt_head=FIXED_MT_HEAD,
        config_sha=None,
        device=FIXED_DEVICE,
        timestamp=FIXED_TIMESTAMP,
        glossary=None,
        hotwords=None,
    )


def test_three_sides_produce_identical_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    k_put, source_sha = media_put_side_key(tmp_path, monkeypatch)
    k_cli = cli_query_side_key(source_sha)
    k_unified = unified_key(source_sha)
    assert k_cli == k_put, (
        "cli 查询侧与 media put 侧的现有键不一致——停止统一并报告，"
        f"cli={k_cli} put={k_put}")
    assert k_cli == k_unified, (
        "统一函数改变了现有键——禁止（现有缓存会全部失效），"
        f"cli={k_cli} unified={k_unified}")
    assert k_unified.startswith("sha256:")
