# AGENTS.md

Guidance for AI agents (and humans) working on this repository. Keep this file
up to date when behavior, layout, or commands change.

## What this project is

`ebook_rename.py` renames ebook files (`.epub`/`.pdf`/`.azw3`/`.mobi`) to their
embedded title, with a filename fallback, OCR fallback, optional year suffix,
RAR/ZIP expansion, archive cleanup, and size-based duplicate handling.

It is a **single-file Python tool** plus a Docker image and a small bash
wrapper. `index.html` is an unrelated, self-hosted CSV dashboard that ships in
the same repo.

## Repository layout

| Path | Notes |
| ---- | ----- |
| `ebook_rename.py` | The whole tool (~1300 lines). Target of nearly all changes. |
| `Dockerfile` | App image: python:3.12-slim + `unrar`/`unar`/`p7zip-full`/`tesseract-ocr`/`imagemagick` + `pypdf`. |
| `ebook-rename` | Bash wrapper: builds the image if missing, mounts a dir at `/books`, runs as the host uid/gid, forwards `EBOOK_*` envs. |
| `index.html` | Self-contained CSV dashboard (no build step). Do not touch unless asked. |
| `README.md` | User-facing docs. Update when CLI/flags/env/behavior change. |
| `.dockerignore` | Keeps ebooks, `*.md`, `*.csv`, the wrapper, and `__pycache__` out of the image. |
| `archive/` | Created at runtime by the tool (moved archives + loose duplicates). Not source. |

## Environment

- Host is WSL (Ubuntu). No host `pip`/`tesseract`/`unrar`/`unar`/`7z` — use Docker.
- Docker is available. Host user is uid/gid `1000:1000`.
- The wrapper mounts the target directory at `/books`; anything outside it is
  invisible to the container (e.g. a `--report` path must be **relative** to
  the scanned directory, not an absolute host path).

## Commands

```bash
# Syntax / static checks (fast, run after every edit)
python3 -m py_compile ebook_rename.py
bash -n ebook-rename

# Rebuild the image — REQUIRED after editing ebook_rename.py, because the
# wrapper only auto-builds when the image is missing and copies the script in.
docker build -t ebook-rename .

# Run against a directory
EBOOK_RENAME_DIR=/path/to/books ./ebook-rename --no-ocr --year --dry-run
EBOOK_RENAME_DIR=/path/to/books ./ebook-rename --no-ocr --year --apply
```

There is no test suite. Verify with `py_compile`, `bash -n`, and a manual
integration test (below) before declaring a change done.

## Verification recipe

Always test on **copies under `/tmp/opencode/`**, never the real library.

1. **Unit-test pure helpers** by importing the file:
   ```python
   import importlib.util, tempfile, os
   spec = importlib.util.spec_from_file_location("er", "ebook_rename.py")
   er = importlib.util.module_from_spec(spec); spec.loader.exec_module(er)
   # call er.resolve_target(...), er.build_plan([...]), etc.
   ```
2. **Archive/dedup scenarios** (needs the image): build a temp dir with
   `zipfile`, then run the wrapper with `--no-ocr` first `--dry-run` then
   `--apply` and inspect the tree. Cover:
   - two identical archives → one extracted, the other skipped as a duplicate,
     both moved to `archive/`;
   - same book, different size → `_1` suffix;
   - a **loose** file that duplicates an existing file → moved to `archive/`;
   - an archive with no ebook → left in place;
   - re-run → `archive/` is ignored.
3. Always confirm a **dry-run makes no filesystem changes** (no `archive/`
   dir created, no renames).

## Architecture map (verify line numbers, they drift)

- Metadata: `extract_title` / `extract_year` (EPUB `dc:title`/`dc:date`, PDF
  `/Title`/`/CreationDate` via `pypdf`, MOBI EXTH 503/106).
- OCR: `_ocr_backend` (`local` if `tesseract`+`convert`, else `docker`),
  `_ocr_lines`, `_pick_title`, `ocr_cover_title`. EPUB covers only.
- Names: `strip_site_prefix`, `sanitize`, `usable_title`, `fallback_name`,
  `looks_weird`, `is_correct_already`.
- Planning: `gather_files` → (archives) `expand_archives` → `build_plan` →
  `print_plan` / `write_report` → apply loop in `main`.
- Archives: `extract_archive` → `_extract_rar` (RARLAB `unrar`) /
  `_extract_zip_stdlib` (normal + ZipCrypto) / `_extract_with_7z` (AES zip,
  encrypted rar fallback); `walk_ebook_files`, `_best_base`, `_move_archive`.
- Targets: `resolve_target` (size-aware) + `_unique_in`.
- Year logic: `year_from_name`, `_yearless`, `_YEAR_TAIL`, and the per-book
  grouping inside `build_plan`.

## Core invariants

- **Size-based dedup** is central. `resolve_target(directory, base, ext,
  src_path, taken)` returns `(path, status)` where `taken` maps
  `lower(abs path) -> size`:
  - `"ok"` name free (reserved); `"self"` the candidate is the source file;
  - `"duplicate"` an existing file with the **same size** already has the name;
  - a name held by a **different size** is skipped with `_1`, `_2`, ...
- **Loose duplicate → `archive/`** (`build_plan(..., archive_path=...)` sets
  `action="archive"`); **duplicate extracted from an archive → skipped**.
- **One base per archive**: all books inside one archive share one name.
- **One year per book**: files sharing a (yearless) base in the same folder
  agree on a single year, preferring the EPUB's.
- **Never overwrite**: collisions get a `_N` suffix or become duplicates.
- **Archives are moved, not deleted**: processed archives go to
  `<scanned-dir>/<archive-dir>/` (`archive` by default, `_1` on name clash);
  failed / no-ebook archives stay put.
- **`archive/` (the configured `--archive-dir` name) is skipped** on recursive
  scans so moved archives are not reprocessed.
- **Dry-run must not mutate anything** — no moves, no `mkdir`, no renames.
- **Files must stay owned by the invoking uid/gid** (`1000:1000`); Docker
  helper invocations use `--user` and `HOME=/tmp`.

## Conventions

- Standard library only, plus optional `pypdf` for PDFs. Do **not** add
  dependencies without a strong reason.
- **Do not add comments** unless they explain non-obvious intent; the code style
  is intentionally sparse.
- Keep the tool a single file; prefer small helpers and `type | None` hints
  (target Python 3.10+).
- Human/progress output goes to **stderr**; the plan/report goes to stdout.
- Boolean env flags accept `1/true/yes/on` via `_env_flag`.
- When you change CLI flags, env vars, defaults, or archive behavior, update
  `README.md` and the wrapper's env-forwarding loop in `ebook-rename`.

## Environment variables

`BOOKS_DIR`, `EBOOK_YEAR`, `EBOOK_YEAR_SEP`, `EBOOK_NO_OCR`,
`EBOOK_NO_ARCHIVES`/`EBOOK_NO_RAR`, `EBOOK_APPLY`, `EBOOK_ARCHIVE_PASSWORD`,
`EBOOK_ARCHIVE_DIR`. Wrapper-only: `EBOOK_RENAME_IMAGE`, `EBOOK_RENAME_DIR`.

## Gotchas

- **RARLAB `unrar` is the only reliable RAR extractor.** `bsdtar` may silently
  extract inner EPUB/ZIP contents from RAR5 (corrupt output); `7z` 16.02 and
  `unar` fail on modern RAR5. `extract_archive` prefers `unrar`.
- **AES-encrypted ZIP** (method 99) needs `7z`; Python's `zipfile` cannot
  decrypt it. ZipCrypto works with the stdlib.
- Default archive password is `sanet.st@Mercurybooks`; override with
  `--archive-password` / `EBOOK_ARCHIVE_PASSWORD`, or `''` to disable.
- **Rebuild the image after editing `ebook_rename.py`** — otherwise the wrapper
  runs the stale copy baked into the image.
- Report paths are relative to the scanned directory (the `/books` mount).
- The real sample library may contain deliberately corrupt archives; a failed
  extraction is not necessarily a tool bug.

## Recent changes (context for future work)

- Size-aware dedup (`resolve_target`) replaced the old name-only
  `unique_target`, applied in both `build_plan` and `expand_archives`.
- Processed archives are moved into `<archive-dir>/` (configurable via
  `--archive-dir`/`EBOOK_ARCHIVE_DIR`); the archive folder is skipped on
  recursive scans.
- Loose files that duplicate an existing same-size file are moved to the
  archive folder (`action="archive"`); the report shows the `archive/<name>`
  destination.
- `index.html` (self-hosted CSV dashboard) and `README.md` were added.
