"""Safe zip extraction for uploaded server packs.

A pack comes from the internet, so nothing in it may write outside the target folder, create links, or blow up
the disk: absolute paths, `..`, symlinks, device files, too many files and too many bytes are all refused before
anything is written.
"""
from __future__ import annotations

import stat
import zipfile
from pathlib import Path, PurePosixPath

MAX_FILES = 20_000
MAX_BYTES = 8 * 1024**3  # unpacked size
CHUNK = 1 << 20


class UnsafeArchive(ValueError):
    pass


def _check(info: zipfile.ZipInfo) -> PurePosixPath:
    name = info.filename.replace("\\", "/")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or (path.parts and ":" in path.parts[0]):
        raise UnsafeArchive(f"unsafe path in the zip: {info.filename!r}")
    mode = info.external_attr >> 16
    if mode and (stat.S_ISLNK(mode) or stat.S_ISCHR(mode) or stat.S_ISBLK(mode) or stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)):
        raise UnsafeArchive(f"{info.filename!r} is a link or special file, which packs may not contain")
    return path


def single_top_folder(names: list[PurePosixPath]) -> str | None:
    """Many packs wrap everything in one folder; return its name so it can be stripped."""
    tops = {p.parts[0] for p in names if p.parts}
    only_one_dir = len(tops) == 1 and all(len(p.parts) > 1 for p in names if p.parts)
    return next(iter(tops)) if only_one_dir else None


def safe_extract(zip_path: Path, dest: Path, max_files: int = MAX_FILES, max_bytes: int = MAX_BYTES) -> int:
    """Unpack into `dest` (created if needed); returns the number of files written. Raises UnsafeArchive before writing
    anything when the zip's listing is unsafe or too big, and stops if the real data turns out larger than it claimed."""
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as e:
        raise UnsafeArchive("that is not a valid zip file") from e
    with zf:
        members = zf.infolist()
        paths = [_check(i) for i in members]
        files = [i for i in members if not i.is_dir()]
        if len(files) > max_files:
            raise UnsafeArchive(f"the zip has {len(files)} files; the limit is {max_files}")
        if sum(i.file_size for i in files) > max_bytes:
            raise UnsafeArchive("the zip unpacks to more than the size limit")
        strip = single_top_folder([p for p, i in zip(paths, members) if not (i.is_dir() and len(p.parts) == 1)])  # the wrapper's own dir entry doesn't count
        written = total = 0
        for info, path in zip(members, paths):
            parts = path.parts[1:] if strip else path.parts
            if not parts:
                continue
            target = (root / Path(*parts)).resolve()
            if root not in target.parents and target != root:
                raise UnsafeArchive(f"unsafe path in the zip: {info.filename!r}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as out:
                while chunk := src.read(CHUNK):
                    total += len(chunk)
                    if total > max_bytes:
                        raise UnsafeArchive("the zip unpacks to more than the size limit")
                    out.write(chunk)
            if (info.external_attr >> 16) & 0o100:  # keep the executable bit for start scripts
                target.chmod(target.stat().st_mode | 0o111)
            written += 1
    return written
