FROM python:3.12-slim

ARG OCR_EXTRA_PKGS=""

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    BOOKS_DIR=/books

# unrar (RARLAB) lives in non-free; enable it so modern RAR5 archives work.
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
      sed -i 's/^Components: .*/Components: main contrib non-free non-free-firmware/' \
        /etc/apt/sources.list.d/debian.sources; \
    else \
      sed -i 's/ main$/ main contrib non-free/' /etc/apt/sources.list; \
    fi; \
    apt-get update -qq; \
    apt-get install -y -qq --no-install-recommends \
      tesseract-ocr imagemagick unrar unar p7zip-full ca-certificates ${OCR_EXTRA_PKGS}; \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir pypdf

COPY ebook_rename.py /usr/local/bin/ebook_rename.py

WORKDIR /books

ENTRYPOINT ["python3", "/usr/local/bin/ebook_rename.py"]
CMD ["--help"]
