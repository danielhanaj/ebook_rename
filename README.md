# ebook-rename

A small, dependency-light tool that renames ebook files to their **embedded
title** and tidies up the mess left behind by download sites.

It scans a folder (optionally recursively), reads the metadata of every
`.epub` / `.pdf` / `.azw3` / `.mobi` file, and renames each one to a clean,
filesystem-safe title. It also unpacks `.rar` / `.zip` archives, gives all the
books inside one archive a single shared name, and moves the processed archives
out of the way.

## Features

- **Title-based renaming** from embedded metadata, with a clean filename
  fallback when the title is missing or a placeholder.
- **Placeholder & junk handling** — rejects generic titles like
  `Untitled`, `Microsoft Word`, etc., and strips download-site prefixes
  (`sanet.st_`, `z-lib`, `libgen`, `annas-archive`, ...).
- **OCR fallback** — when an EPUB has no usable title, the cover image is
  OCR'd to recover the title.
- **Optional year** — appends the publishing year (`Title_2023.epub`).
- **Archive expansion** — unpacks `.rar`/`.zip` (including nested folders),
  supports encrypted archives, and assigns one shared base name per archive
  (so a book's `.epub` and `.pdf` always match).
- **Archive cleanup** — processed archives are moved into an `archive/`
  subfolder (kept, never deleted).
- **Duplicate detection** — a file whose target name already exists with the
  **same size** is treated as a duplicate.
- **Safe by default** — dry-run preview plus a confirmation prompt; never
  overwrites an existing file.
- **CSV report** — optionally write what was (or would be) done.

## How it works

1. **Scan** the target directory (recursively with `-r`).
2. **Expand archives** first: each `.rar`/`.zip` is extracted to a temp dir,
   and every ebook inside is renamed to one shared base name derived from the
   best title found among them.
3. **Read metadata** for every ebook:
   | Format | Title | Year |
   | ------ | ----- | ---- |
   | EPUB   | OPF `dc:title` | OPF `dc:date` |
   | PDF    | `/Title` (via `pypdf`) | `/CreationDate` |
   | AZW3/MOBI | full-name / EXTH 503 | EXTH 106 |
4. **Fallback** to the cleaned-up original filename if the title is missing,
   unusable, or a placeholder. For EPUBs, OCR of the cover is tried first.
5. **Sanitize** the title: spaces become underscores and illegal characters
   (`/ \ : * ? " < > |`) are removed. On Windows, reserved names are avoided.
6. **Plan & apply** the renames. If a target name is taken:
   - by a file of the **same size** → duplicate (see below);
   - by a file of a **different size** → a `_1`, `_2`, ... suffix is added.

### Duplicates

A "duplicate" is a file that would land on an existing name **and** has the
same byte size (a strong signal it is the same book):

- A **loose file** that is a duplicate is **moved into the archive folder**
  so you can review or delete it later.
- A file **extracted from an archive** that is a duplicate is simply skipped
  (the archive is still moved to the archive folder).

### Years

With `--year`, exactly one year is chosen per book. Files that share a
(yearless) base name in the same folder — e.g. the `.epub` and `.pdf` of one
title — are forced to agree, preferring the year from the EPUB (then PDF,
then filename).

## Requirements

- **Python 3.10+** (uses `X | Y` type hints).
- **PDF:** [`pypdf`](https://pypi.org/project/pypdf/) (`pip install pypdf`).
  EPUB and MOBI/AZW3 need **no** third-party packages.
- **OCR (optional):** `tesseract` and ImageMagick's `convert`, or Docker (a
  helper image is built on demand).
- **RAR (optional):** `unrar` (RARLAB). `unar` and `7z` are used as fallbacks.
- **ZIP:** standard library. AES-encrypted ZIPs additionally need `7z`.

Everything above is bundled in the provided Docker image.

## Usage

### With Docker (recommended)

The `ebook-rename` wrapper builds the image on first use, mounts a directory,
and forwards the relevant environment variables. It operates on the current
directory by default:

```bash
./ebook-rename                 # scan $PWD
./ebook-rename --year --apply  # scan $PWD, append year, no prompt

EBOOK_RENAME_DIR=/path/to/books ./ebook-rename --year
```

Build the image yourself instead:

```bash
docker build -t ebook-rename .
docker run --rm -v /path/to/books:/books ebook-rename --year
```

### Without Docker

```bash
pip install pypdf
python3 ebook_rename.py -d /path/to/books --year
```

OCR and RAR support require the tools listed above to be on your `PATH`.

### Examples

```bash
# Preview what would happen (nothing is changed)
python3 ebook_rename.py -d ./books --year --dry-run

# Rename everything, recursively, without the confirmation prompt
python3 ebook_rename.py -d ./books -r --year --apply

# Only touch files with weird names, EPUBs only
python3 ebook_rename.py -d ./books --weird-only --ext .epub

# Disable archive expansion and OCR
python3 ebook_rename.py -d ./books --no-archives --no-ocr

# Custom archive password, custom archive folder
python3 ebook_rename.py -d ./books --archive-password 's3cret' --archive-dir old

# Write a CSV report
python3 ebook_rename.py -d ./books --apply --report results.csv
```

## Command-line options

| Option | Description |
| ------ | ----------- |
| `-d, --dir DIR` | Source directory to scan (falls back to `$BOOKS_DIR`, then prompts). |
| `-r, --recursive` | Scan subdirectories too. |
| `--dry-run` | Only preview; never rename or move anything. |
| `--apply` | Rename without the confirmation prompt. |
| `--weird-only` | Only process files with weird names. |
| `--ext .EXT` | Only include the given extension (repeatable). |
| `--no-ocr` | Disable the cover-OCR fallback. |
| `--no-archives`, `--no-rar` | Do not expand `.rar`/`.zip` archives. |
| `--archive-password PW` | Password for encrypted archives (default: built-in site password; `''` disables). |
| `--archive-dir NAME` | Subfolder to move processed archives into (default: `archive`). |
| `--year` | Append the publishing year to the title. |
| `--year-sep SEP` | Separator before the year (default: `_`). |
| `--report FILE` | Write a CSV report (relative to the scanned directory). |
| `--ocr-image IMAGE` | OCR a standalone cover image and print the detected title. |

## Environment variables

All boolean variables accept `1`, `true`, `yes`, or `on`.

| Variable | Equivalent |
| -------- | ---------- |
| `BOOKS_DIR` | `--dir` |
| `EBOOK_YEAR` | `--year` |
| `EBOOK_YEAR_SEP` | `--year-sep` |
| `EBOOK_NO_OCR` | `--no-ocr` |
| `EBOOK_NO_ARCHIVES` / `EBOOK_NO_RAR` | `--no-archives` |
| `EBOOK_APPLY` | `--apply` |
| `EBOOK_ARCHIVE_PASSWORD` | `--archive-password` |
| `EBOOK_ARCHIVE_DIR` | `--archive-dir` |
| `EBOOK_RENAME_IMAGE` | Docker image name used by the wrapper (default `ebook-rename`). |
| `EBOOK_RENAME_DIR` | Directory the wrapper mounts into the container (default `$PWD`). |

## Archive handling

- Each archive is extracted into a temporary directory, then its books are
  moved next to the archive with a single shared base name.
- After a successful extraction the archive is **moved** into
  `<scanned-dir>/<archive-dir>/` (default `archive/`). Archives that fail to
  extract or contain no ebook are **left in place**.
- The archive folder is ignored on subsequent recursive scans.
- Encrypted archives are tried with the default site password
  (`sanet.st@Mercurybooks`). Override with `--archive-password` /
  `EBOOK_ARCHIVE_PASSWORD`, or pass an empty string to disable.
- RAR5 archives require RARLAB's `unrar`; other extractors may silently
  produce broken output, so `unrar` is preferred when available.

## Files

| File | Purpose |
| ---- | ------- |
| `ebook_rename.py` | The tool (single file, standard library + `pypdf`). |
| `Dockerfile` | Container image with all optional tools bundled. |
| `ebook-rename` | Bash wrapper that builds/runs the Docker image. |
| `.dockerignore` | Keeps ebooks and build artifacts out of the image. |

## Notes & limitations

- Renaming never overwrites: an existing different file forces a `_N` suffix.
- PDF OCR is not performed; OCR applies to EPUB cover images only.
- The tool modifies filenames and moves files. Always start with `--dry-run`
  (the default) before using `--apply`.

## License

Add the license of your choice before publishing (e.g. MIT).
