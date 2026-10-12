#!/usr/bin/env python3
"""Rename ebook files (epub/pdf/azw3/mobi) to their embedded title.

- Reads the title from the ebook's metadata.
- Replaces spaces with underscores (and sanitizes illegal filename chars).
- Falls back to the cleaned-up original filename when the title is missing or
  a placeholder, stripping common download-site prefixes such as ``sanet.st_``.
- Dry-run preview + confirmation by default; never overwrites an existing file.
- Expands ``.rar``/``.zip`` archives (including nested directories) and gives all
  books inside one archive a single shared base name.

Only the standard library is required for EPUB. PDF support needs ``pypdf``
and AZW3/MOBI support uses a small built-in parser (no extra packages).
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import zipfile
import xml.etree.ElementTree as ET

SUPPORTED_EXTS = (".epub", ".pdf", ".azw3", ".mobi")
ARCHIVE_EXTS = (".rar", ".zip")
ARCHIVE_DIR = "archive"
DEFAULT_ARCHIVE_PASSWORD = "sanet.st@Mercurybooks"
APP_IMAGE = os.environ.get("EBOOK_RENAME_IMAGE", "ebook-rename")

OCR_IMAGE = "ebook-ocr"
OCR_DOCKERFILE = """\
FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq \\
 && apt-get install -y -qq --no-install-recommends software-properties-common \\
 && add-apt-repository -y multiverse \\
 && apt-get update -qq \\
 && apt-get install -y -qq --no-install-recommends \\
      tesseract-ocr imagemagick unar unrar p7zip-full ca-certificates \\
 && rm -rf /var/lib/apt/lists/*
WORKDIR /work
"""

# lines on a cover that are never the title
COVER_NOISE = re.compile(
    r"(gitforgits|publishing|press|cover|www\.|https?:|copyright|"
    r"all rights reserved|edition|isbn)",
    re.IGNORECASE,
)

PLACEHOLDER_TITLES = {
    "",
    "book title",
    "untitled",
    "unknown",
    "unknown title",
    "no title",
    "title",
    "default title",
    "ebook",
}


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


ILLEGAL_CHARS = re.compile(r'[/\\:*?"<>|]')

SITE_PREFIXES = [
    r"^sanet\.st[_\-]+",
    r"^sanet[_\-]+",
    r"^z-?lib(?:rary)?[._\-]+",
    r"^libgen(?:\.\w+)?[._\-]+",
    r"^annas?-?archive[._\-]+",
    r"^annas?[._\-]+",
    r"^epub(?:k)?[._\-]+",
    r"^www\.[^_\-]+[._\-]+",
    r"^[a-z0-9\-]+\.(?:com|net|org|st|to|cc|me|ru|io|info|xyz)[_\-]+",
    r"^\[\w+\][_\-]+",
]

WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


# --------------------------------------------------------------------------- #
# metadata extraction
# --------------------------------------------------------------------------- #
def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _first_dc_title(opf_bytes: bytes) -> str | None:
    try:
        root = ET.fromstring(opf_bytes)
    except ET.ParseError:
        return None

    titles: list[str] = []
    for el in root.iter():
        if _localname(el.tag) == "title" and el.text and el.text.strip():
            titles.append(el.text.strip())
        if _localname(el.tag) == "metadata" and len(titles) > 1:
            break
    if not titles:
        return None
    # Prefer the longest real title (many OPFs include a short main title plus
    # a fuller title, or a filename-ish surrogate title).
    titles.sort(key=len, reverse=True)
    return titles[0]


def title_from_epub(path: str) -> str | None:
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        opf_name = None
        container = "META-INF/container.xml"
        if container in names:
            try:
                root = ET.fromstring(zf.read(container))
                for el in root.iter():
                    if _localname(el.tag) == "rootfile":
                        opf_name = el.attrib.get("full-path")
                        break
            except ET.ParseError:
                opf_name = None
        if not opf_name:
            candidates = [n for n in names if n.lower().endswith(".opf")]
            opf_name = candidates[0] if candidates else None
        if not opf_name:
            return None
        return _first_dc_title(zf.read(opf_name))


def creator_from_epub(path: str) -> str | None:
    try:
        with zipfile.ZipFile(path) as zf:
            opf_name = _resolve_opf(zf)
            if not opf_name:
                return None
            root = ET.fromstring(zf.read(opf_name))
    except (zipfile.BadZipFile, ET.ParseError, OSError):
        return None
    for el in root.iter():
        if _localname(el.tag) == "creator" and el.text and el.text.strip():
            return el.text.strip()
    return None


def title_from_pdf(path: str) -> str | None:
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        try:
            from PyPDF2 import PdfReader  # type: ignore
        except ImportError:
            raise MissingDependency("PDF", "pip install pypdf")
    reader = PdfReader(path)
    meta = reader.metadata
    if not meta:
        return None
    title = meta.get("/Title") or getattr(meta, "title", None)
    return str(title).strip() if title else None


def _be32(data: bytes, off: int) -> int:
    return int.from_bytes(data[off:off + 4], "big")


def mobi_fields(path: str) -> dict:
    """Minimal MOBI/AZW3 (PalmDB) metadata reader.

    Reads the "full name" field of the MOBI header plus EXTH records 503
    (updated title) and 106 (publishing date).
    """
    out = {"title": None, "year": None}
    with open(path, "rb") as fh:
        data = fh.read()

    if len(data) < 90:
        return out
    num_records = int.from_bytes(data[76:78], "big")
    if num_records < 1:
        return out
    rec0_start = _be32(data, 78)
    if rec0_start <= 0 or rec0_start >= len(data):
        return out

    mobi = rec0_start + 16  # skip PalmDOC header
    if data[mobi:mobi + 4] != b"MOBI":
        return out

    enc = "utf-8"
    text_encoding = _be32(data, mobi + 12)
    if text_encoding == 1252:
        enc = "cp1252"

    name_off = _be32(data, mobi + 0x54)
    name_len = _be32(data, mobi + 0x58)
    full_name = None
    if 0 < name_len and 0 < name_off < len(data):
        raw = data[name_off:name_off + name_len]
        try:
            full_name = raw.decode(enc, "replace").strip("\x00").strip()
        except Exception:
            full_name = None

    # EXTH records follow the MOBI header.
    header_len = _be32(data, mobi + 4)
    exth = mobi + header_len
    exth_title = None
    if data[exth:exth + 4] == b"EXTH":
        count = _be32(data, exth + 8)
        pos = exth + 12
        for _ in range(count):
            if pos + 8 > len(data):
                break
            rec_type = _be32(data, pos)
            rec_len = _be32(data, pos + 4)
            if rec_len < 8:
                break
            payload = data[pos + 8:pos + rec_len]
            if payload:
                if rec_type == 503:
                    exth_title = payload.decode(enc, "replace").strip("\x00").strip()
                elif rec_type == 106:
                    text = payload.decode(enc, "replace").strip("\x00").strip()
                    m = re.search(r"(1[5-9]\d{2}|20\d{2})", text)
                    if m:
                        out["year"] = m.group(1)
            pos += rec_len

    out["title"] = exth_title or full_name
    return out


def title_from_mobi(path: str) -> str | None:
    return mobi_fields(path)["title"]


def year_from_mobi(path: str) -> str | None:
    return mobi_fields(path)["year"]



class MissingDependency(Exception):
    def __init__(self, fmt: str, hint: str):
        super().__init__(f"{fmt} support needs an extra package: {hint}")
        self.fmt = fmt
        self.hint = hint


def extract_title(path: str) -> str | None:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".epub":
        return title_from_epub(path)
    if ext == ".pdf":
        return title_from_pdf(path)
    if ext in (".azw3", ".mobi"):
        return title_from_mobi(path)
    return None


def year_from_epub(path: str) -> str | None:
    try:
        with zipfile.ZipFile(path) as zf:
            opf_name = _resolve_opf(zf)
            if not opf_name:
                return None
            root = ET.fromstring(zf.read(opf_name))
    except (zipfile.BadZipFile, ET.ParseError, OSError):
        return None
    for el in root.iter():
        if _localname(el.tag) == "date" and el.text:
            m = re.search(r"(1[5-9]\d{2}|20\d{2})", el.text)
            if m:
                return m.group(1)
    return None


def year_from_pdf(path: str) -> str | None:
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        try:
            from PyPDF2 import PdfReader  # type: ignore
        except ImportError:
            return None
    try:
        meta = PdfReader(path).metadata
    except Exception:
        return None
    if not meta:
        return None
    for key in ("/CreationDate", "/ModDate"):
        value = meta.get(key)
        if value:
            m = re.search(r"(1[5-9]\d{2}|20\d{2})", str(value))
            if m:
                return m.group(1)
    return None


def extract_year(path: str) -> str | None:
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".epub":
            return year_from_epub(path)
        if ext == ".pdf":
            return year_from_pdf(path)
        if ext in (".azw3", ".mobi"):
            return year_from_mobi(path)
    except Exception:
        return None
    return None


# --------------------------------------------------------------------------- #
# cover-image OCR (via a reusable Docker image)
# --------------------------------------------------------------------------- #
IMAGE_EXTS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


def _resolve_opf(zf: zipfile.ZipFile) -> str | None:
    container = "META-INF/container.xml"
    if container in zf.namelist():
        try:
            root = ET.fromstring(zf.read(container))
            for el in root.iter():
                if _localname(el.tag) == "rootfile":
                    return el.attrib.get("full-path")
        except ET.ParseError:
            pass
    candidates = [n for n in zf.namelist() if n.lower().endswith(".opf")]
    return candidates[0] if candidates else None


def epub_cover_image(path: str) -> tuple[bytes, str] | None:
    """Return (image_bytes, extension) for the EPUB cover, if findable."""
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        opf_name = _resolve_opf(zf)
        if not opf_name:
            return None
        opf_dir = os.path.dirname(opf_name)
        try:
            root = ET.fromstring(zf.read(opf_name))
        except ET.ParseError:
            return None

        items: dict[str, tuple[str, str, str]] = {}
        cover_id = None
        for el in root.iter():
            lname = _localname(el.tag)
            if lname == "item":
                items[el.attrib.get("id", "")] = (
                    el.attrib.get("href", ""),
                    el.attrib.get("media-type", ""),
                    el.attrib.get("properties", ""),
                )
            elif lname == "meta" and el.attrib.get("name") == "cover":
                cover_id = el.attrib.get("content")

        if not cover_id:
            for iid, (_h, _m, props) in items.items():
                if "cover-image" in props:
                    cover_id = iid
                    break

        href = ""
        media = ""
        if cover_id and cover_id in items:
            href, media, _ = items[cover_id]

        def load(rel: str, mt: str) -> tuple[bytes, str] | None:
            full = os.path.normpath(os.path.join(opf_dir, rel)).replace("\\", "/")
            if full not in names:
                return None
            ext = os.path.splitext(rel)[1].lower()
            if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                ext = {".jpeg": ".jpg"}.get(ext) or {
                    "image/png": ".png", "image/gif": ".gif",
                    "image/webp": ".webp"}.get(mt, ".jpg")
            return zf.read(full), ext

        if href:
            got = load(href, media)
            if got:
                return got

        for name in names:
            low = name.lower()
            if "cover" in low and low.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
                got = load(name, "")
                if got:
                    return got
        for iid, (h, m, _p) in items.items():
            if m.startswith("image/"):
                got = load(h, m)
                if got:
                    return got
        for name in names:
            if name.lower().endswith((".jpg", ".jpeg", ".png")):
                return zf.read(name), os.path.splitext(name)[1].lower()
    return None


def docker_available() -> bool:
    return shutil.which("docker") is not None


def ensure_ocr_image() -> bool:
    """Make sure the OCR docker image exists, building it once if needed."""
    if not docker_available():
        return False
    have = subprocess.run(
        ["docker", "image", "inspect", OCR_IMAGE],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if have.returncode == 0:
        return True
    print("  (preparing OCR image, first run may take a minute...)",
          file=sys.stderr)
    with tempfile.TemporaryDirectory() as td:
        with open(os.path.join(td, "Dockerfile"), "w") as fh:
            fh.write(OCR_DOCKERFILE)
        built = subprocess.run(
            ["docker", "build", "-q", "-t", OCR_IMAGE, td],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    return built.returncode == 0


def _ocr_backend() -> str | None:
    """Return 'local', 'docker', or None depending on what is available."""
    if shutil.which("tesseract") and shutil.which("convert"):
        return "local"
    if shutil.which("docker"):
        return "docker"
    return None


def _ocr_lines(image_bytes: bytes, ext: str) -> list[dict]:
    backend = _ocr_backend()
    if backend is None:
        return []
    with tempfile.TemporaryDirectory() as td:
        img = os.path.join(td, "cover" + ext)
        with open(img, "wb") as fh:
            fh.write(image_bytes)
        if backend == "local":
            top = os.path.join(td, "top.png")
            subprocess.run(
                ["convert", img, "-crop", "100%x55%+0+0", "+repage",
                 "-resize", "300%", "-colorspace", "Gray", "-normalize", top],
                capture_output=True, text=True,
            )
            proc = subprocess.run(
                ["tesseract", top, "stdout", "--psm", "6", "tsv"],
                capture_output=True, text=True,
            )
        else:
            image = _helper_image()
            if not image:
                return []
            script = (
                "convert /work/cover" + ext +
                " -crop 100%x55%+0+0 +repage -resize 300% "
                "-colorspace Gray -normalize /tmp/top.png; "
                "tesseract /tmp/top.png stdout --psm 6 tsv 2>/dev/null"
            )
            proc = subprocess.run(
                ["docker", "run", "--rm",
                 "--user", f"{os.getuid()}:{os.getgid()}",
                 "-e", "HOME=/tmp",
                 "-v", f"{td}:/work", image, "bash", "-c", script],
                capture_output=True, text=True,
            )
    lines: dict[tuple, dict] = {}
    try:
        reader = csv.DictReader(proc.stdout.splitlines(), delimiter="\t")
        for row in reader:
            if row.get("level") != "5":
                continue
            try:
                conf = float(row["conf"])
            except (TypeError, ValueError):
                continue
            if conf < 0:
                continue
            key = (row["block_num"], row["par_num"], row["line_num"])
            top, height = int(row["top"]), int(row["height"])
            d = lines.setdefault(key, {"top": top, "bot": top + height,
                                       "words": [], "conf": []})
            d["top"] = min(d["top"], top)
            d["bot"] = max(d["bot"], top + height)
            if row["text"].strip():
                d["words"].append(row["text"].strip())
            d["conf"].append(conf)
    except Exception:
        return []
    out = []
    for d in lines.values():
        text = " ".join(d["words"]).strip()
        if not text:
            continue
        out.append({
            "text": text,
            "height": d["bot"] - d["top"],
            "top": d["top"],
            "conf": sum(d["conf"]) / len(d["conf"]),
        })
    return out


def _pick_title(lines: list[dict], creator: str | None) -> str | None:
    creator_norm = (creator or "").strip().lower()
    cand = []
    for ln in lines:
        if ln["conf"] < 45:
            continue
        text = ln["text"]
        if len(re.sub(r"\W", "", text)) < 3:
            continue
        if COVER_NOISE.search(text):
            continue
        if creator_norm and text.strip().lower() == creator_norm:
            continue
        cand.append(ln)
    if not cand:
        return None
    max_h = max(ln["height"] for ln in cand)
    picked = [ln for ln in cand if ln["height"] >= 0.55 * max_h]
    picked.sort(key=lambda ln: ln["top"])
    title = " ".join(ln["text"] for ln in picked)
    return title.strip() or None


def ocr_cover_title(path: str, creator: str | None = None) -> str | None:
    """OCR an ebook's cover image to recover a title. EPUB only for now."""
    if os.path.splitext(path)[1].lower() != ".epub":
        return None
    try:
        cover = epub_cover_image(path)
    except Exception:
        return None
    if not cover:
        return None
    if _ocr_backend() is None:
        return None
    if creator is None:
        creator = creator_from_epub(path)
    image_bytes, ext = cover
    lines = _ocr_lines(image_bytes, ext)
    return _pick_title(lines, creator)


# --------------------------------------------------------------------------- #
# name handling
# --------------------------------------------------------------------------- #
def strip_site_prefix(name: str) -> str:
    prev = None
    while prev != name:
        prev = name
        for pattern in SITE_PREFIXES:
            new = re.sub(pattern, "", name, count=1, flags=re.IGNORECASE)
            if new != name:
                name = new
                break
    return name


def sanitize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = ILLEGAL_CHARS.sub("_", text)
    text = re.sub(r"\s+", "_", text.strip())          # spaces -> underscores
    text = re.sub(r"_+", "_", text)                   # collapse repeats
    text = text.strip("._ ")
    if not text:
        return ""
    if text.split(".")[0].lower() in WINDOWS_RESERVED:
        text = "_" + text
    return text[:200]


def usable_title(title: str | None) -> bool:
    if not title:
        return False
    norm = title.strip().lower()
    if norm in PLACEHOLDER_TITLES:
        return False
    if re.fullmatch(r"[\d\W]+", norm):                # only digits/punctuation
        return False
    return True


def fallback_name(filename: str) -> str:
    stem = os.path.splitext(filename)[0]
    stem = strip_site_prefix(stem)
    stem = re.sub(r"[_\-]+", "_", stem).strip("_-.")
    return stem


def looks_weird(filename: str) -> bool:
    stem = os.path.splitext(filename)[0]
    if any(re.match(p, stem, flags=re.IGNORECASE) for p in SITE_PREFIXES):
        return True
    tokens = re.split(r"[_\-\s.]+", stem) if "_" in stem else [stem]
    long_digits = sum(1 for t in tokens if t.isdigit() and len(t) >= 6)
    hashish = sum(1 for t in tokens if re.fullmatch(r"[0-9a-fA-F]{8,}", t))
    return bool(long_digits or hashish)


def is_correct_already(filename: str, title: str | None) -> bool:
    if not usable_title(title):
        return False
    return filename == sanitize(title) + os.path.splitext(filename)[1]


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def gather_files(directory: str, recursive: bool,
                 include_archives: bool = False,
                 skip_dirs: tuple[str, ...] = ()) -> list[str]:
    wanted = SUPPORTED_EXTS + (ARCHIVE_EXTS if include_archives else ())
    skip = {d.lower() for d in skip_dirs}
    found: list[str] = []
    if recursive:
        for root, dirs, files in os.walk(directory):
            dirs[:] = [d for d in dirs if d.lower() not in skip]
            for f in files:
                if f.lower().endswith(wanted):
                    found.append(os.path.join(root, f))
    else:
        for f in sorted(os.listdir(directory)):
            if f.lower().endswith(wanted):
                found.append(os.path.join(directory, f))
    return found


def resolve_target(directory: str, base: str, ext: str, src_path: str,
                   taken: dict[str, int | None]) -> tuple[str, str]:
    """Pick a target path for ``base+ext`` applying the duplicate rule.

    ``taken`` maps lowercase absolute paths to their size (or None if
    unknown). Returns ``(path, status)``:

    - ``"self"``      the candidate is the source file itself (nothing to do)
    - ``"duplicate"`` an existing file of the *same size* already has the name
    - ``"ok"``        a free name was found and reserved in ``taken``

    A name held by a file of a *different* size is skipped over with a
    ``_1``, ``_2``, ... suffix.
    """
    src_key = os.path.abspath(src_path).lower()
    try:
        src_size = os.path.getsize(src_path)
    except OSError:
        src_size = None
    i = 0
    while True:
        candidate = base + ("" if i == 0 else f"_{i}") + ext
        path = os.path.join(directory, candidate)
        key = os.path.abspath(path).lower()
        if key == src_key:
            return path, "self"
        if key not in taken:
            taken[key] = src_size
            return path, "ok"
        if src_size is not None and taken[key] == src_size:
            return path, "duplicate"
        i += 1


def _unique_in(directory: str, name: str, existing: set[str]) -> str:
    """Return a free path for ``name`` in ``directory`` (``_1`` on clashes)."""
    stem, ext = os.path.splitext(name)
    candidate = name
    i = 1
    while candidate.lower() in existing:
        candidate = f"{stem}_{i}{ext}"
        i += 1
    return os.path.join(directory, candidate)



# --------------------------------------------------------------------------- #
# archives (rar / zip)
# --------------------------------------------------------------------------- #
def _docker_image_exists(name: str) -> bool:
    proc = subprocess.run(
        ["docker", "image", "inspect", name],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return proc.returncode == 0


def _helper_image() -> str | None:
    """Pick a usable Docker image for helper tasks (OCR / extraction)."""
    if shutil.which("docker") is None:
        return None
    if _docker_image_exists(APP_IMAGE):
        return APP_IMAGE
    if _docker_image_exists(OCR_IMAGE):
        return OCR_IMAGE
    if ensure_ocr_image():
        return OCR_IMAGE
    return None


def _archive_tool() -> str | None:
    """Return a local extractor command, 'docker', or None.

    Only unrar (RARLAB) and unar are trusted: bsdtar/7z silently or noisily
    mishandle modern RAR5 archives.
    """
    for tool in ("unrar", "unar"):
        if shutil.which(tool):
            return tool
    if shutil.which("docker"):
        return "docker"
    return None


def _last_error(proc: subprocess.CompletedProcess) -> None:
    msg = (proc.stderr or proc.stdout or "").strip().splitlines()
    if msg:
        print(f"    {msg[-1]}", file=sys.stderr)


def _extract_zip_stdlib(archive: str, dest_dir: str,
                        password: str | None) -> tuple[bool, bool]:
    """Extract a zip with the stdlib. Returns (ok, needs_7z).

    ``needs_7z`` is True for AES-encrypted entries (method 99), which the
    stdlib cannot decrypt and 7z must handle.
    """
    root = os.path.realpath(dest_dir)
    pwd = password.encode() if password else None
    try:
        with zipfile.ZipFile(archive) as zf:
            for member in zf.infolist():
                name = member.filename
                if not name or name.endswith("/"):
                    continue
                target = os.path.realpath(os.path.join(root, name))
                if target != root and not target.startswith(root + os.sep):
                    continue  # skip entries escaping the destination
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(member, pwd=pwd) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        return True, False
    except NotImplementedError:
        return False, True  # AES (WinZip method 99) -> needs 7z
    except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
        print(f"    {exc}", file=sys.stderr)
        return False, False


def _extract_with_7z(archive: str, dest_dir: str,
                     password: str | None) -> bool:
    pwd_arg = "-p" + password if password else "-p"
    if shutil.which("7z"):
        proc = subprocess.run(
            ["7z", "x", "-y", pwd_arg, "-o" + dest_dir, archive],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        if proc.returncode != 0:
            _last_error(proc)
        return proc.returncode == 0
    image = _helper_image()
    if not image or shutil.which("docker") is None:
        return False
    archive_dir = os.path.dirname(os.path.abspath(archive)) or "."
    name = os.path.basename(archive)
    script = f'7z x -y -p"$ARCHPW" -o/out "/in/{name}"'
    proc = subprocess.run(
        ["docker", "run", "--rm",
         "--user", f"{os.getuid()}:{os.getgid()}",
         "-e", "HOME=/tmp", "-e", f"ARCHPW={password or ''}",
         "-v", f"{archive_dir}:/in:ro",
         "-v", f"{dest_dir}:/out",
         "--entrypoint", "bash", image, "-c", script],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        _last_error(proc)
    return proc.returncode == 0


def _extract_rar(archive: str, dest_dir: str, password: str | None) -> bool:
    tool = _archive_tool()
    if tool is None:
        return False
    if tool == "unrar":
        cmd = ["unrar", "x", "-o+", "-y",
               f"-p{password}" if password else "-p-", archive, dest_dir + "/"]
    elif tool == "unar":
        cmd = ["unar", "-q", "-f", "-o", dest_dir]
        if password:
            cmd += ["-p", password]
        cmd += [archive]
    else:  # docker
        image = _helper_image()
        if not image:
            return False
        archive_dir = os.path.dirname(os.path.abspath(archive)) or "."
        rar_name = os.path.basename(archive)
        unrar_pw = '-p"$ARCHPW"' if password else "-p-"
        unar_pw = '-p "$ARCHPW"' if password else ""
        script = (f'unrar x -o+ -y {unrar_pw} "/in/{rar_name}" /out/ '
                  f'|| unar -q -f {unar_pw} -o /out "/in/{rar_name}"')
        cmd = ["docker", "run", "--rm",
               "--user", f"{os.getuid()}:{os.getgid()}",
               "-e", "HOME=/tmp", "-e", f"ARCHPW={password or ''}",
               "-v", f"{archive_dir}:/in:ro",
               "-v", f"{dest_dir}:/out",
               "--entrypoint", "bash", image, "-c", script]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        _last_error(proc)
    return proc.returncode == 0


def extract_archive(archive: str, dest_dir: str,
                    password: str | None = None) -> bool:
    if archive.lower().endswith(".zip"):
        ok, needs_7z = _extract_zip_stdlib(archive, dest_dir, password)
        if ok:
            return True
        if needs_7z or password:
            return _extract_with_7z(archive, dest_dir, password)
        return False
    return _extract_rar(archive, dest_dir, password)


def walk_ebook_files(root: str) -> list[str]:
    out: list[str] = []
    for dirpath, _dirs, files in os.walk(root):
        for f in sorted(files):
            if f.lower().endswith(SUPPORTED_EXTS):
                out.append(os.path.join(dirpath, f))
    return out


def _title_pref(path: str) -> int:
    return {".epub": 0, ".pdf": 1, ".azw3": 2, ".mobi": 3}.get(
        os.path.splitext(path)[1].lower(), 9)


def _best_base(cands: list[str], use_ocr: bool, add_year: bool,
               year_sep: str, archive_stem: str) -> tuple[str, str | None]:
    """Pick one shared base name (+year) for all files in an archive."""
    ordered = sorted(cands, key=_title_pref)
    title = None
    for p in ordered:
        try:
            t = extract_title(p)
        except Exception:
            t = None
        if usable_title(t):
            title = t
            break
    if title is None and use_ocr:
        for p in ordered:
            if os.path.splitext(p)[1].lower() != ".epub":
                continue
            try:
                t = ocr_cover_title(p)
            except Exception:
                t = None
            if usable_title(t):
                title = t
                break

    if title is not None:
        base = sanitize(title)
    else:
        base = ""
        for p in ordered:
            base = sanitize(fallback_name(os.path.basename(p)))
            if base:
                break
        if not base:
            base = sanitize(fallback_name(archive_stem))

    year = None
    if add_year and base:
        for p in ordered:
            year = extract_year(p) or year_from_name(os.path.basename(p))
            if year:
                break
        base = _yearless(base)
        if year and not base.endswith(year_sep + year):
            base = base + year_sep + year
    return base, year


def _move_archive(archive: str, directory: str, archive_dir: str,
                  dest_taken: set[str], dry_run: bool) -> str:
    """Move ``archive`` into ``<directory>/<archive_dir>/``. Returns the
    destination path (not moved when ``dry_run``)."""
    dest_dir = os.path.join(directory, archive_dir)
    target = _unique_in(dest_dir, os.path.basename(archive), dest_taken)
    dest_taken.add(os.path.basename(target).lower())
    if not dry_run:
        os.makedirs(dest_dir, exist_ok=True)
        shutil.move(archive, target)
    return target


def expand_archives(archives: list[str], directory: str, use_ocr: bool = True,
                    add_year: bool = False, year_sep: str = "_",
                    dry_run: bool = False, password: str | None = None,
                    archive_dir: str = ARCHIVE_DIR) -> list[str]:
    """Extract ebook files from archives into ``directory`` with one shared
    base name per archive. Processed archives are moved into
    ``<directory>/<archive_dir>``. Returns the list of created paths (empty
    when ``dry_run`` is True)."""
    taken: dict[str, int | None] = {}
    for entry in os.listdir(directory):
        p = os.path.join(directory, entry)
        if os.path.isfile(p):
            try:
                taken[os.path.abspath(p).lower()] = os.path.getsize(p)
            except OSError:
                taken[os.path.abspath(p).lower()] = None

    dest_dir = os.path.join(directory, archive_dir)
    dest_taken = (set(n.lower() for n in os.listdir(dest_dir))
                  if os.path.isdir(dest_dir) else set())

    extracted: list[str] = []
    for archive in archives:
        name = os.path.basename(archive)
        kind = os.path.splitext(name)[1].lstrip(".").upper() or "ARC"
        print(f"  {kind}: {name} ...", file=sys.stderr, end="")
        with tempfile.TemporaryDirectory() as stage:
            if not extract_archive(archive, stage, password):
                print(" extraction failed (archive kept)", file=sys.stderr)
                continue
            cands = walk_ebook_files(stage)
            if not cands:
                print(" no epub/pdf/azw3 inside", file=sys.stderr)
                continue
            stem = os.path.splitext(name)[0]
            base, _year = _best_base(cands, use_ocr, add_year, year_sep, stem)
            if not base:
                print(" no usable name", file=sys.stderr)
                continue

            targets: list[tuple[str, str]] = []
            dup = 0
            for cand in cands:
                ext = os.path.splitext(cand)[1]
                target, status = resolve_target(directory, base, ext, cand, taken)
                if status == "duplicate":
                    print(f"          = {os.path.basename(target)} "
                          f"(duplicate, same size, skipped)", file=sys.stderr)
                    dup += 1
                    continue
                targets.append((cand, target))

            if not targets and not dup:
                print(" nothing to place", file=sys.stderr)
                continue

            moved = _move_archive(archive, directory, archive_dir,
                                  dest_taken, dry_run)
            extra = f" ({dup} duplicate skipped)" if dup else ""
            if dry_run:
                names = ", ".join(os.path.basename(t) for _c, t in targets)
                print(f" would extract -> {names or '(all duplicates)'}",
                      file=sys.stderr)
                print(f"          would move archive -> "
                      f"{archive_dir}/{os.path.basename(moved)}",
                      file=sys.stderr)
                continue
            for cand, target in targets:
                shutil.move(cand, target)
                extracted.append(target)
            print(f" -> {base}{extra}  (archive -> {archive_dir}/"
                  f"{os.path.basename(moved)})", file=sys.stderr)
    return extracted



_YEAR_TAIL = re.compile(r"(?:[_\-.\s])((?:19|20)\d{2})$")


def year_from_name(filename: str) -> str | None:
    m = _YEAR_TAIL.search(os.path.splitext(filename)[0])
    return m.group(1) if m else None


def _yearless(base: str) -> str:
    m = _YEAR_TAIL.search(base)
    return base[:m.start()] if m else base


def build_plan(files: list[str], use_ocr: bool = True,
               add_year: bool = False, year_sep: str = "_",
               archive_path: str | None = None) -> list[dict]:
    items: list[dict] = []
    for path in files:
        directory = os.path.dirname(path) or "."
        filename = os.path.basename(path)
        ext = os.path.splitext(filename)[1]
        try:
            title = extract_title(path)
            error = None
        except MissingDependency as exc:
            title, error = None, str(exc)
        except Exception as exc:  # corrupt file, unreadable metadata, ...
            title, error = None, f"could not read metadata ({exc})"

        source = "title"
        if not usable_title(title) and use_ocr and ext.lower() == ".epub":
            print(f"  OCR: {filename} ...", file=sys.stderr, end="")
            try:
                ocr_title = ocr_cover_title(path)
            except Exception:
                ocr_title = None
            if usable_title(ocr_title):
                title, source = ocr_title, "ocr"
                print(f" found '{title}'")
            else:
                print(" no title found")

        if usable_title(title):
            base = sanitize(title)
        else:
            base = sanitize(fallback_name(filename))
            source = "filename"

        own_year = None
        if add_year and base:
            try:
                own_year = extract_year(path) or year_from_name(filename)
            except Exception:
                own_year = None

        items.append({
            "path": path, "directory": directory, "filename": filename,
            "ext": ext, "title": title, "source": source, "base": base,
            "own_year": own_year, "error": error, "weird": looks_weird(filename),
        })

    # One year per book: files in the same folder that share a (yearless) base
    # name are the same title (e.g. the epub + pdf of one book) and must agree.
    groups: dict[tuple[str, str], list[dict]] = {}
    for it in items:
        groups.setdefault((it["directory"], _yearless(it["base"]).lower()),
                          []).append(it)
    for members in groups.values():
        year = None
        for m in sorted(members, key=lambda m: _title_pref(m["path"])):
            if m["own_year"]:
                year = m["own_year"]
                break
        for m in members:
            m["year"] = year

    plan: list[dict] = []
    taken: dict[str, int | None] = {}
    for f in files:
        key = os.path.abspath(f).lower()
        try:
            taken[key] = os.path.getsize(f)
        except OSError:
            taken[key] = None
    dest_dir = None
    dest_taken: set[str] = set()
    if archive_path is not None:
        dest_dir = archive_path
        if os.path.isdir(dest_dir):
            dest_taken = {n.lower() for n in os.listdir(dest_dir)}
    for it in items:
        base = it["base"]
        year = it["year"]
        if add_year and base:
            base = _yearless(base)
            if year and not base.endswith(year_sep + year):
                base = base + year_sep + year
        entry = {
            "path": it["path"], "filename": it["filename"],
            "title": it["title"], "source": it["source"], "year": year,
            "weird": it["weird"], "error": it["error"],
        }
        if not base:
            entry.update(target=it["path"], action="skip", reason="no usable name")
        elif it["filename"] == base + it["ext"]:
            entry.update(target=it["path"], action="skip", reason="already correct")
        else:
            target, status = resolve_target(it["directory"], base, it["ext"],
                                            it["path"], taken)
            if status == "self":
                entry.update(target=it["path"], action="skip",
                             reason="already correct")
            elif status == "duplicate":
                if dest_dir is not None:
                    target = _unique_in(dest_dir, it["filename"], dest_taken)
                    dest_taken.add(os.path.basename(target).lower())
                    taken.pop(os.path.abspath(it["path"]).lower(), None)
                    entry.update(target=target, action="archive",
                                 reason="duplicate, same size")
                else:
                    entry.update(target=it["path"], action="skip",
                                 reason="duplicate, same size")
            else:
                taken.pop(os.path.abspath(it["path"]).lower(), None)
                entry.update(target=target, action="rename", reason="")
        plan.append(entry)
    return plan



def print_plan(plan: list[dict], ignore_errors: bool) -> None:
    print()
    tags = {"rename": "RENAME", "archive": "ARCHIVE", "skip": "skip  "}
    for e in plan:
        tag = tags.get(e["action"], e["action"])
        note = f"  [{e['source']}]"
        if e.get("year"):
            note += f" year={e['year']}"
        if e["action"] in ("skip", "archive"):
            note += f" ({e['reason']})"
        if e["weird"]:
            note += " weird"
        print(f"  {tag}  {e['filename']}")
        if e["action"] == "archive":
            parent = os.path.basename(os.path.dirname(e["target"]) or ".")
            print(f"          -> {parent}/{os.path.basename(e['target'])}{note}")
        elif e["action"] == "rename":
            print(f"          -> {os.path.basename(e['target'])}{note}")
        elif note.strip():
            print(f"          {note.strip()}")
    print()


def write_report(path: str, plan: list[dict]) -> None:
    try:
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["action", "old", "new", "source", "year", "error"])
            for e in plan:
                action = e["action"]
                if action in ("archive", "archived"):
                    new = os.path.join(
                        os.path.basename(os.path.dirname(e["target"]) or "."),
                        os.path.basename(e["target"]))
                else:
                    new = os.path.basename(e["target"])
                writer.writerow([
                    action,
                    e["filename"],
                    new,
                    e["source"],
                    e.get("year") or "",
                    e.get("error") or "",
                ])
        print(f"Report written to {path}")
    except OSError as exc:
        print(f"warning: could not write report: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rename ebooks to their embedded title.",
    )
    parser.add_argument("-d", "--dir", help="source directory to scan "
                        "(falls back to $BOOKS_DIR, then a prompt)")
    parser.add_argument("-r", "--recursive", action="store_true",
                        help="scan subdirectories too")
    parser.add_argument("--dry-run", action="store_true",
                        help="only preview, never rename")
    parser.add_argument("--apply", action="store_true", default=_env_flag("EBOOK_APPLY"),
                        help="rename without the confirmation prompt")
    parser.add_argument("--weird-only", action="store_true",
                        help="only consider files with weird names")
    parser.add_argument("--ext", action="append", metavar=".EXT",
                        help="limit to extension(s), repeatable")
    parser.add_argument("--no-ocr", action="store_true", default=_env_flag("EBOOK_NO_OCR"),
                        help="disable cover-OCR fallback for bad titles")
    parser.add_argument("--no-archives", "--no-rar", action="store_true",
                        dest="no_archives",
                        default=_env_flag("EBOOK_NO_ARCHIVES") or _env_flag("EBOOK_NO_RAR"),
                        help="do not expand .rar/.zip archives")
    parser.add_argument("--archive-password",
                        default=os.environ.get("EBOOK_ARCHIVE_PASSWORD")
                        or DEFAULT_ARCHIVE_PASSWORD,
                        metavar="PW",
                        help="password for encrypted .rar/.zip archives "
                             "(default: built-in site password; use '' to disable)")
    parser.add_argument("--archive-dir",
                        default=os.environ.get("EBOOK_ARCHIVE_DIR", ARCHIVE_DIR),
                        metavar="NAME",
                        help="subfolder to move processed archives into "
                             "(default: 'archive')")
    parser.add_argument("--year", action="store_true", default=_env_flag("EBOOK_YEAR"),
                        help="append the publishing year to the title")
    parser.add_argument("--year-sep", default=os.environ.get("EBOOK_YEAR_SEP", "_"),
                        metavar="SEP",
                        help="separator before the year (default: '_')")
    parser.add_argument("--report", metavar="FILE",
                        help="write a CSV report (relative to the scanned dir)")
    parser.add_argument("--ocr-image", metavar="IMAGE",
                        help="OCR a standalone cover image and print the title")
    args = parser.parse_args(argv)

    if args.ocr_image:
        with open(args.ocr_image, "rb") as fh:
            data = fh.read()
        ext = os.path.splitext(args.ocr_image)[1].lower() or ".jpg"
        if _ocr_backend() is None:
            print("error: tesseract (or Docker) is required for OCR",
                  file=sys.stderr)
            return 2
        title = _pick_title(_ocr_lines(data, ext), None)
        print(title if title else "(no title found)")
        return 0

    if args.dir:
        directory = os.path.expanduser(args.dir)
    elif os.environ.get("BOOKS_DIR"):
        directory = os.path.expanduser(os.environ["BOOKS_DIR"])
    else:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        try:
            entered = input(f"Source directory to scan [{script_dir}]: ").strip()
        except EOFError:
            entered = ""
        directory = os.path.expanduser(entered) if entered else script_dir
    if not os.path.isdir(directory):
        print(f"error: not a directory: {directory}", file=sys.stderr)
        return 2

    items = gather_files(directory, args.recursive,
                         include_archives=not args.no_archives,
                         skip_dirs=(os.path.basename(args.archive_dir.rstrip("/"))
                                    or ARCHIVE_DIR,))
    archives = [f for f in items if f.lower().endswith(ARCHIVE_EXTS)]
    files = [f for f in items if not f.lower().endswith(ARCHIVE_EXTS)]
    if args.ext:
        wanted = tuple(e if e.startswith(".") else "." + e for e in args.ext)
        files = [f for f in files if f.lower().endswith(wanted)]
    if not files and not archives:
        print(f"No ebook files found in {directory}")
        return 0

    if archives:
        extracted = expand_archives(
            archives, directory, use_ocr=not args.no_ocr,
            add_year=args.year, year_sep=args.year_sep, dry_run=args.dry_run,
            password=args.archive_password, archive_dir=args.archive_dir)
        files += extracted
    if not files:
        if args.report:
            write_report(args.report, [])
        return 0

    archive_path = (None if args.no_archives
                    else os.path.join(directory, args.archive_dir))
    plan = build_plan(files, use_ocr=not args.no_ocr,
                      add_year=args.year, year_sep=args.year_sep,
                      archive_path=archive_path)
    if args.weird_only:
        plan = [e for e in plan
                if e["weird"] or e["action"] in ("rename", "archive")]

    renames = [e for e in plan if e["action"] == "rename"]
    dupes = [e for e in plan if e["action"] == "archive"]
    skipped = len(plan) - len(renames) - len(dupes)
    print(f"Scanned {len(files)} file(s); {len(renames)} to rename, "
          f"{len(dupes)} duplicate(s) to archive, {skipped} skipped.")
    print_plan(plan, ignore_errors=False)

    actions = renames + dupes
    if not actions:
        if args.report:
            write_report(args.report, plan)
        return 0
    if args.dry_run:
        print("Dry run: no files were changed.")
        if args.report:
            write_report(args.report, plan)
        return 0

    if not args.apply:
        try:
            answer = input("Proceed with renaming? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("Aborted.")
            if args.report:
                write_report(args.report, plan)
            return 0

    renamed = 0
    archived = 0
    for e in actions:
        try:
            if e["action"] == "archive":
                os.makedirs(os.path.dirname(e["target"]), exist_ok=True)
                shutil.move(e["path"], e["target"])
                parent = os.path.basename(os.path.dirname(e["target"]) or ".")
                print(f"  archived {e['filename']} -> "
                      f"{parent}/{os.path.basename(e['target'])}")
                e["action"] = "archived"
                archived += 1
            else:
                os.rename(e["path"], e["target"])
                print(f"  renamed  {e['filename']} -> "
                      f"{os.path.basename(e['target'])}")
                e["action"] = "renamed"
                renamed += 1
        except OSError as exc:
            e["action"] = "failed"
            e["error"] = str(exc)
            print(f"  FAILED   {e['filename']}: {exc}", file=sys.stderr)
    if args.report:
        write_report(args.report, plan)
    print(f"\nDone. {renamed} file(s) renamed, {archived} duplicate(s) archived.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
