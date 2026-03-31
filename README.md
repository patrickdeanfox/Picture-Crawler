# Picture-Crawler

A GUI-based media crawler and downloader built with Python and tkinter. Crawls web pages for images and videos, displays thumbnails, and lets you select and download what you want.

## Features

- Crawls a URL for images and videos
- Optionally follows child links (configurable depth and page limit)
- Displays image thumbnails in a dark-themed GUI
- Select individual or all media before downloading
- Supports authenticated sessions:
  - **Browser cookies** — reads cookies automatically from Firefox or Chrome via `browser-cookie3`
  - **Manual cookie string** — paste a cookie header directly
  - **No auth** (default)
- Detects media via file extensions, URL patterns, Open Graph meta tags, and JSON-LD blocks
- Falls back to HTTP HEAD probes for ambiguous URLs

## Requirements

- Python 3.10+
- `requests`
- `beautifulsoup4`
- `Pillow`
- `browser-cookie3` *(optional, for browser cookie support)*

Install dependencies:

```bash
pip install requests beautifulsoup4 Pillow browser-cookie3
```

## Usage

```bash
python3 gui_downloader.py
# or pass a URL to pre-fill the input:
python3 gui_downloader.py https://example.com
```

Missing dependencies are installed automatically on first run.

## Supported Formats

**Images:** `.jpg`, `.jpeg`, `.png`, `.gif`, `.webp`, `.bmp`, `.tiff`

**Videos:** `.mp4`, `.webm`, `.mov`, `.avi`, `.mkv`, `.flv`, `.m4v`, `.ts`

## Output

Downloaded files are saved to `~/Downloads/crawler_downloads/` by default. The output directory can be changed from within the GUI.
