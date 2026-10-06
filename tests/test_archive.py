import zipfile
from pathlib import Path

import pytest

from panel.archive import UnsafeArchive, safe_extract


def make_zip(path: Path, entries: dict[str, bytes | tuple[bytes, int]]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name, data in entries.items():
            body, mode = data if isinstance(data, tuple) else (data, 0o644)
            i = zipfile.ZipInfo(name)
            i.external_attr = mode << 16
            z.writestr(i, body)
    return path


def test_extracts_and_strips_a_single_wrapper_folder(tmp_path):
    z = make_zip(tmp_path / "p.zip", {"Pack-1.0/mods/a.jar": b"a", "Pack-1.0/run.sh": (b"#!/bin/sh\n", 0o755), "Pack-1.0/config/x.toml": b"x"})
    assert safe_extract(z, tmp_path / "out") == 3
    assert (tmp_path / "out" / "mods" / "a.jar").read_bytes() == b"a"
    assert (tmp_path / "out" / "run.sh").stat().st_mode & 0o111  # still executable


def test_keeps_layout_when_there_is_no_single_wrapper(tmp_path):
    z = make_zip(tmp_path / "p.zip", {"mods/a.jar": b"a", "run.sh": b"x"})
    safe_extract(z, tmp_path / "out")
    assert (tmp_path / "out" / "mods" / "a.jar").exists() and (tmp_path / "out" / "run.sh").exists()


@pytest.mark.parametrize("name", ["../evil.txt", "/etc/passwd", "a/../../evil", "C:/win.txt", "a\\..\\..\\evil"])
def test_path_tricks_are_refused_and_nothing_is_written(tmp_path, name):
    z = make_zip(tmp_path / "p.zip", {"ok.txt": b"ok", name: b"bad"})
    with pytest.raises(UnsafeArchive):
        safe_extract(z, tmp_path / "out")
    assert not (tmp_path / "out" / "ok.txt").exists() and not (tmp_path / "evil.txt").exists()


def test_symlinks_are_refused(tmp_path):
    z = make_zip(tmp_path / "p.zip", {"link": (b"/etc/passwd", 0o120777)})
    with pytest.raises(UnsafeArchive, match="link"):
        safe_extract(z, tmp_path / "out")


def test_limits_on_file_count_and_size(tmp_path):
    z = make_zip(tmp_path / "p.zip", {f"f{i}": b"x" for i in range(5)})
    with pytest.raises(UnsafeArchive, match="files"):
        safe_extract(z, tmp_path / "o1", max_files=3)
    big = make_zip(tmp_path / "b.zip", {"big": b"x" * 5000})
    with pytest.raises(UnsafeArchive, match="size limit"):
        safe_extract(big, tmp_path / "o2", max_bytes=1000)


def test_not_a_zip(tmp_path):
    (tmp_path / "x.zip").write_text("nope")
    with pytest.raises(UnsafeArchive, match="not a valid zip"):
        safe_extract(tmp_path / "x.zip", tmp_path / "o")


def test_wrapper_folder_with_its_own_directory_entry_is_still_stripped(tmp_path):
    z = tmp_path / "p.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("Pack/", "")
        zf.writestr("Pack/run.sh", "x")
    safe_extract(z, tmp_path / "out")
    assert (tmp_path / "out" / "run.sh").exists()
