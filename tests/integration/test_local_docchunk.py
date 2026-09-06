"""Real docchunk integration: TXT fixture -> verified corpus (isolated corpus root)."""

from pathlib import Path

from knowledge_ingest.adapters.docchunk import DocchunkAdapter
from knowledge_ingest.config import AppConfig

FIXTURE = Path(__file__).parents[1] / "fixtures" / "small.txt"


def make_config(tmp_path: Path) -> AppConfig:
    return AppConfig.model_validate({
        "pipeline_root": str(tmp_path / "kp"),
        "media_project": "/Volumes/ORICO/Projects/media-transcriber",
        "docchunk_project": "/Volumes/ORICO/Projects/docchunk",
        "media_output_root": str(tmp_path / "media-out"),
        "docchunk_corpus_root": str(tmp_path / "corpus-root"),
        "skill_roots": ["~/.agents/skills"],
        "skills": {
            "baidu": "baidu-drive",
            "quark": "quarkclouddrive",
            "cangjie": "cangjie-skill",
            "personal_distiller": "personal-capability-distiller",
        },
        "processing": {"media_device": "auto", "media_timestamp": "10m",
                       "require_orico": False},
    })


def test_local_txt_split_and_verify(tmp_path: Path):
    config = make_config(tmp_path)
    adapter = DocchunkAdapter(project=config.docchunk_project)
    corpus_root = tmp_path / "corpus-root"
    corpus_root.mkdir()
    corpus = adapter.split(FIXTURE, corpus_root=corpus_root)
    assert (corpus / "manifest.json").is_file()
    assert (corpus / "index.jsonl").is_file()
    assert adapter.verify(corpus) is True
