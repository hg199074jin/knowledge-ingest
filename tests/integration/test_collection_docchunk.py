"""Real docchunk integration over a symlink-based Document Set (Task 6 Step 3)."""

import shutil
from pathlib import Path

import pytest

from knowledge_ingest.adapters.docchunk import DocchunkAdapter
from knowledge_ingest.collection import build_document_set

FIXTURES = Path(__file__).parents[1] / "fixtures"
DOCSHUNK_PROJECT = Path("/Volumes/ORICO/Projects/docchunk")
requires_docchunk = pytest.mark.skipif(
    not DOCSHUNK_PROJECT.is_dir(),
    reason="real docchunk project not available on this machine",
)


@requires_docchunk
def test_docchunk_reads_symlinked_document_set(tmp_path: Path):
    source = tmp_path / "source" / "course"
    source.mkdir(parents=True)
    shutil.copy(FIXTURES / "small.md", source / "01.md")
    shutil.copy(FIXTURES / "small.txt", source / "02.txt")

    handoff = tmp_path / "handoff" / "document-set"
    handoff.mkdir(parents=True)
    result = build_document_set(
        handoff_dir=handoff, source_root=source,
        document_paths=[source / "01.md", source / "02.txt"],
        media_paths=[], transcripts={}, excluded=set(),
    )
    assert result.map_path.parent.name == "handoff"
    assert all(p.is_symlink() for p in handoff.iterdir())

    corpus_root = tmp_path / "corpus-root"
    corpus_root.mkdir()
    adapter = DocchunkAdapter(project=Path("/Volumes/ORICO/Projects/docchunk"))
    corpus = adapter.split(handoff, corpus_root=corpus_root)
    assert (corpus / "manifest.json").is_file()
    assert adapter.verify(corpus) is True
