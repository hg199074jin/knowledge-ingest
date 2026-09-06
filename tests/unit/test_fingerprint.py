from pathlib import Path

from knowledge_ingest.fingerprint import (
    fingerprint_collection,
    fingerprint_file,
)


def test_fingerprint_file_content_based(tmp_path: Path):
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    a.write_bytes(b"same content")
    b.write_bytes(b"same content")
    assert fingerprint_file(a) == fingerprint_file(b)
    assert fingerprint_file(a).startswith("sha256:")


def test_fingerprint_file_differs_on_content(tmp_path: Path):
    a = tmp_path / "a.bin"
    a.write_bytes(b"one")
    b = tmp_path / "b.bin"
    b.write_bytes(b"two")
    assert fingerprint_file(a) != fingerprint_file(b)


def test_fingerprint_large_file_streams(tmp_path: Path):
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * (9 * 1024 * 1024))
    value = fingerprint_file(big)
    assert value.startswith("sha256:")


def test_collection_hash_is_order_independent(tmp_path: Path):
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    for name in ("01.pdf", "02.md", "sub/03.txt"):
        (dir_a / name).parent.mkdir(parents=True, exist_ok=True)
        (dir_a / name).write_bytes(name.encode())
    for name in ("02.md", "sub/03.txt", "01.pdf"):
        (dir_b / name).parent.mkdir(parents=True, exist_ok=True)
        (dir_b / name).write_bytes(name.encode())
    fa = fingerprint_collection(dir_a)
    fb = fingerprint_collection(dir_b)
    assert fa.sha256 == fb.sha256
    assert [f.relative_path for f in fa.files] == [
        "01.pdf", "02.md", "sub/03.txt",
    ]


def test_collection_ignores_noise(tmp_path: Path):
    clean = tmp_path / "clean"
    noisy = tmp_path / "noisy"
    clean.mkdir()
    noisy.mkdir()
    (clean / "doc.pdf").write_bytes(b"pdf")
    (noisy / "doc.pdf").write_bytes(b"pdf")
    (noisy / ".DS_Store").write_bytes(b"noise")
    (noisy / "._doc.pdf").write_bytes(b"appledouble")
    assert fingerprint_collection(clean).sha256 == fingerprint_collection(noisy).sha256
