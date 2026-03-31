#!/usr/bin/env python3
"""
Media Crawler & Downloader — GUI Edition
Crawls a URL for images/videos, optionally follows child links,
shows thumbnails, lets you select and download.

Supports authenticated sessions via:
  - browser-cookie3 (reads cookies from Firefox/Chrome automatically)
  - Manual cookie string paste
  - No auth (default)

Usage:
    python3 gui_downloader.py
    python3 gui_downloader.py https://example.com
"""

import io
import os
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

try:
    import requests
    from bs4 import BeautifulSoup
    from PIL import Image, ImageTk
except ImportError:
    print("Installing required packages...")
    os.system("pip install requests beautifulsoup4 Pillow --break-system-packages")
    import requests
    from bs4 import BeautifulSoup
    from PIL import Image, ImageTk

try:
    import browser_cookie3
    BROWSER_COOKIE3_AVAILABLE = True
except ImportError:
    BROWSER_COOKIE3_AVAILABLE = False

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# ── Config ─────────────────────────────────────────────────────────────────────

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"}
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".avi", ".mkv", ".flv", ".m4v", ".ts"}
ALL_EXTENSIONS   = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS

# Content-Type prefixes that identify media even without a clean file extension
IMAGE_MIME_PREFIXES = ("image/",)
VIDEO_MIME_PREFIXES = ("video/",)

# URL pattern hints that strongly suggest a video link even without an extension
VIDEO_URL_PATTERNS = re.compile(
    r"(\.mp4|\.webm|\.m3u8|\.ts|\.mov|\.avi|\.mkv|\.flv"
    r"|/video/|/videos/|/media/|/stream/|/watch\?|/embed/"
    r"|\.mp4\?|\.webm\?)",
    re.IGNORECASE,
)
IMAGE_URL_PATTERNS = re.compile(
    r"(\.jpg|\.jpeg|\.png|\.gif|\.webp|\.bmp|\.tiff"
    r"|/images?/|/photos?/|/gallery/|/pic/|/thumb/|/thumbnail/)",
    re.IGNORECASE,
)

DEFAULT_OUTPUT   = Path.home() / "Downloads" / "crawler_downloads"
CHUNK_SIZE       = 1024 * 64
THUMB_SIZE       = (160, 160)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# Dark theme
BG       = "#1e1e2e"
BG2      = "#313244"
BG3      = "#45475a"
ACCENT   = "#89b4fa"
GREEN    = "#a6e3a1"
RED      = "#f38ba8"
TEXT     = "#cdd6f4"
SUBTEXT  = "#a6adc8"
SELECTED = "#89dceb"


# ── Session / Auth helpers ─────────────────────────────────────────────────────

def build_session(auth_mode: str, browser: str, cookie_string: str) -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)

    if auth_mode == "browser":
        if not BROWSER_COOKIE3_AVAILABLE:
            raise RuntimeError(
                "browser-cookie3 is not installed.\n"
                "Run:  pip install browser-cookie3 --break-system-packages"
            )
        try:
            cookies = browser_cookie3.firefox() if browser == "Firefox" \
                      else browser_cookie3.chrome()
            session.cookies.update(cookies)
        except Exception as e:
            raise RuntimeError(f"Could not read browser cookies: {e}")

    elif auth_mode == "manual":
        if not cookie_string.strip():
            raise RuntimeError("No cookie string provided.")
        for part in cookie_string.split(";"):
            part = part.strip()
            if "=" in part:
                name, _, value = part.partition("=")
                session.cookies.set(name.strip(), value.strip())

    return session


# ── Media detection ────────────────────────────────────────────────────────────

def classify_url(url: str) -> str | None:
    """
    Try to classify a URL as 'image' or 'video' using:
      1. File extension
      2. URL pattern matching
    Returns 'image', 'video', or None if unrecognised.
    """
    path = urlparse(url).path.lower()
    ext  = Path(path).suffix.lower()

    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if VIDEO_URL_PATTERNS.search(url):
        return "video"
    if IMAGE_URL_PATTERNS.search(url):
        return "image"
    return None


def probe_content_type(url: str, session: requests.Session) -> str | None:
    """
    Do a HEAD request to check Content-Type.
    Returns 'image', 'video', or None.
    Used as a fallback when the URL gives no clues.
    """
    try:
        resp = session.head(url, timeout=8, allow_redirects=True)
        ct   = resp.headers.get("Content-Type", "")
        if ct.startswith(IMAGE_MIME_PREFIXES):
            return "image"
        if ct.startswith(VIDEO_MIME_PREFIXES):
            return "video"
    except Exception:
        pass
    return None


def make_filename_from_url(url: str, media_type: str) -> str:
    """Best-effort filename from URL, with sensible fallback."""
    name = Path(urlparse(url).path).name
    # Strip query strings that may have crept into the name
    name = name.split("?")[0].split("&")[0]
    if not name or "." not in name:
        ext  = ".jpg" if media_type == "image" else ".mp4"
        name = f"media_{abs(hash(url)) % 100000}{ext}"
    return name


# ── Crawling ───────────────────────────────────────────────────────────────────

def extract_media_from_soup(
    soup,
    page_url: str,
    found: dict,
    session: requests.Session,
    probe_unknown: bool = False,
):
    """
    Extract all media from a parsed page into found{}.
    Looks at:
      - <img src / data-src / data-lazy-src / srcset>
      - <video src> and <source src>
      - <a href> — uses URL pattern matching + optional HEAD probe
      - og:image / og:video meta tags
      - JSON-LD script blocks (basic scan for image/video URLs)
    """
    candidates: list[tuple[str, str | None]] = []  # (abs_url, hint)

    def add_candidate(raw_url: str, hint: str | None = None):
        if not raw_url or raw_url.startswith("data:"):
            return
        abs_url = urljoin(page_url, raw_url.strip().split(" ")[0])
        if abs_url and abs_url not in found:
            candidates.append((abs_url, hint))

    # ── <img> tags ──
    for t in soup.find_all("img"):
        for attr in ("src", "data-src", "data-lazy-src", "data-original"):
            add_candidate(t.get(attr, ""), "image")
        # srcset="url1 1x, url2 2x"
        srcset = t.get("srcset", "")
        for part in srcset.split(","):
            url_part = part.strip().split(" ")[0]
            if url_part:
                add_candidate(url_part, "image")

    # ── <video> and <source> tags ──
    for t in soup.find_all(["video", "source"]):
        add_candidate(t.get("src", ""), "video")
        add_candidate(t.get("data-src", ""), "video")

    # ── <a href> ──
    for t in soup.find_all("a", href=True):
        href = t["href"].strip()
        add_candidate(href, None)   # classify by URL pattern

    # ── Open Graph meta tags ──
    for t in soup.find_all("meta"):
        prop = t.get("property", "") or t.get("name", "")
        if prop in ("og:image", "twitter:image"):
            add_candidate(t.get("content", ""), "image")
        elif prop in ("og:video", "og:video:url", "twitter:player:stream"):
            add_candidate(t.get("content", ""), "video")

    # ── JSON-LD blocks: scan raw text for http(s) URLs ending in media ext ──
    for script in soup.find_all("script", type="application/ld+json"):
        text = script.string or ""
        for url in re.findall(r'https?://[^\s"\'<>]+', text):
            add_candidate(url, None)

    # ── Classify and add to found ──
    for abs_url, hint in candidates:
        if abs_url in found:
            continue

        media_type = hint or classify_url(abs_url)

        # If still unknown and probing is enabled, do a HEAD request
        if media_type is None and probe_unknown:
            media_type = probe_content_type(abs_url, session)

        if media_type in ("image", "video"):
            found[abs_url] = {
                "url":      abs_url,
                "filename": make_filename_from_url(abs_url, media_type),
                "ext":      Path(urlparse(abs_url).path).suffix.lower()
                            or (".jpg" if media_type == "image" else ".mp4"),
                "type":     media_type,
                "thumb":    None,
                "page":     page_url,
            }


def get_child_links(soup, page_url: str, base_domain: str) -> list:
    links = []
    seen  = set()
    for tag in soup.find_all("a", href=True):
        href    = tag["href"].strip()
        abs_url = urljoin(page_url, href)
        parsed  = urlparse(abs_url)
        if (parsed.scheme in ("http", "https")
                and parsed.netloc == base_domain
                and abs_url not in seen
                and not abs_url.endswith(tuple(ALL_EXTENSIONS))
                and "#" not in abs_url):
            links.append(abs_url)
            seen.add(abs_url)
    return links


def crawl_for_media(
    start_url,
    session,
    deep=False,
    max_depth=1,
    max_pages=20,
    probe_unknown=False,
    status_cb=None,
    stop_flag=None,
):
    found   = {}
    visited = set()
    base_domain = urlparse(start_url).netloc
    queue = [(start_url, 0)]

    while queue:
        if stop_flag and stop_flag[0]:
            break
        if len(visited) >= max_pages:
            if status_cb:
                status_cb(f"Reached max pages limit ({max_pages}).")
            break

        url, depth = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)

        if status_cb:
            status_cb(
                f"Crawling page {len(visited)}/{max_pages}  "
                f"(depth {depth})  —  {url[:70]}"
            )

        try:
            resp = session.get(url, timeout=20)
            resp.raise_for_status()
        except Exception as e:
            if status_cb:
                status_cb(f"  Skipped: {url}  — {e}")
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        before = len(found)
        extract_media_from_soup(soup, url, found, session, probe_unknown)
        new_items = len(found) - before

        if status_cb:
            status_cb(
                f"  Page {len(visited)}: +{new_items} new items "
                f"({len(found)} total)"
            )

        if deep and depth < max_depth:
            for child in get_child_links(soup, url, base_domain):
                if child not in visited:
                    queue.append((child, depth + 1))

    return list(found.values())


# ── Thumbnails ─────────────────────────────────────────────────────────────────

def fetch_thumbnail(url, session):
    try:
        resp = session.get(url, timeout=10, stream=True)
        resp.raise_for_status()
        data = b"".join(resp.iter_content(CHUNK_SIZE))
        img  = Image.open(io.BytesIO(data)).convert("RGBA")
        img.thumbnail(THUMB_SIZE, Image.LANCZOS)
        padded = Image.new("RGBA", THUMB_SIZE, (30, 30, 46, 255))
        x = (THUMB_SIZE[0] - img.width)  // 2
        y = (THUMB_SIZE[1] - img.height) // 2
        padded.paste(img, (x, y), img)
        return ImageTk.PhotoImage(padded)
    except Exception:
        return None


# ── Naming ─────────────────────────────────────────────────────────────────────

def make_filename(item, index, prefix, scheme):
    ext  = item["ext"]
    orig = item["filename"]
    if scheme == "original":
        return orig
    elif scheme == "numbered":
        return f"{prefix}_{index:04d}{ext}"
    elif scheme == "prefix_original":
        return f"{prefix}_{orig}"
    elif scheme == "timestamp":
        return f"{prefix}_{int(time.time()*1000) + index}{ext}"
    return orig


# ── Downloading ────────────────────────────────────────────────────────────────

def download_file(item, output_dir, session, filename):
    filepath = output_dir / filename
    if filepath.exists():
        return True, f"Skipped (exists): {filename}"
    try:
        resp = session.get(item["url"], stream=True, timeout=60)
        resp.raise_for_status()
        with open(filepath, "wb") as f:
            for chunk in resp.iter_content(CHUNK_SIZE):
                if chunk:
                    f.write(chunk)
        kb = filepath.stat().st_size / 1024
        return True, f"✓ {filename} ({kb:.1f} KB)"
    except Exception as e:
        return False, f"✗ {filename}: {e}"


# ── GUI ────────────────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self, initial_url=""):
        super().__init__()
        self.title("Media Crawler & Downloader")
        self.geometry("1150x920")
        self.minsize(900, 650)
        self.configure(bg=BG)

        self.session     = requests.Session()
        self.session.headers.update(HEADERS)
        self.media_list  = []
        self.thumb_refs  = []
        self.card_frames = []
        self.selected    = set()
        self._stop_flag  = [False]

        self._build_ui(initial_url)

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self, initial_url):

        # ── Row 1: URL bar ──
        row1 = tk.Frame(self, bg=BG2, pady=8, padx=12)
        row1.pack(fill="x")

        tk.Label(row1, text="URL:", bg=BG2, fg=TEXT,
                 font=("Sans", 11, "bold")).pack(side="left")
        self.url_var = tk.StringVar(value=initial_url)
        url_entry = tk.Entry(
            row1, textvariable=self.url_var, bg=BG3, fg=TEXT,
            insertbackground=TEXT, relief="flat",
            font=("Monospace", 10), width=65
        )
        url_entry.pack(side="left", padx=8, ipady=4)
        url_entry.bind("<Return>", lambda e: self._start_crawl())

        self.crawl_btn = tk.Button(
            row1, text="🔍 Crawl", bg=ACCENT, fg=BG,
            font=("Sans", 10, "bold"), relief="flat", padx=12, pady=4,
            cursor="hand2", command=self._start_crawl
        )
        self.crawl_btn.pack(side="left", padx=4)

        self.stop_btn = tk.Button(
            row1, text="⏹ Stop", bg=RED, fg=BG,
            font=("Sans", 10, "bold"), relief="flat", padx=8, pady=4,
            cursor="hand2", command=self._stop_crawl, state="disabled"
        )
        self.stop_btn.pack(side="left", padx=2)

        # ── Row 2: Auth ──
        row2 = tk.Frame(self, bg=BG2, pady=6, padx=12)
        row2.pack(fill="x")

        tk.Label(row2, text="Auth:", bg=BG2, fg=TEXT,
                 font=("Sans", 9, "bold")).pack(side="left")
        self.auth_var = tk.StringVar(value="none")
        for label, val in [
            ("No auth",               "none"),
            ("Firefox cookies",       "browser_firefox"),
            ("Chrome cookies",        "browser_chrome"),
            ("Paste cookies manually","manual"),
        ]:
            tk.Radiobutton(
                row2, text=label, variable=self.auth_var, value=val,
                bg=BG2, fg=TEXT, selectcolor=BG3, activebackground=BG2,
                font=("Sans", 9), cursor="hand2",
                command=self._toggle_cookie_row
            ).pack(side="left", padx=6)

        # ── Row 3: Manual cookie entry (hidden by default) ──
        self.cookie_row = tk.Frame(self, bg=BG2, pady=4, padx=12)
        # packed/unpacked by _toggle_cookie_row

        tk.Label(
            self.cookie_row,
            text="Paste Cookie header value  "
                 "(F12 → Network tab → any request → Request Headers → Cookie:)",
            bg=BG2, fg=SUBTEXT, font=("Sans", 8)
        ).pack(anchor="w")

        cf = tk.Frame(self.cookie_row, bg=BG2)
        cf.pack(fill="x")
        self.cookie_var = tk.StringVar()
        self.cookie_entry = tk.Entry(
            cf, textvariable=self.cookie_var, bg=BG3, fg=TEXT,
            insertbackground=TEXT, relief="flat",
            font=("Monospace", 8), width=110, show="*"
        )
        self.cookie_entry.pack(side="left", ipady=3, fill="x", expand=True)
        self.show_ck = tk.BooleanVar(value=False)
        tk.Checkbutton(
            cf, text="Show", variable=self.show_ck,
            bg=BG2, fg=SUBTEXT, selectcolor=BG3, activebackground=BG2,
            font=("Sans", 8), cursor="hand2",
            command=lambda: self.cookie_entry.config(
                show="" if self.show_ck.get() else "*")
        ).pack(side="left", padx=6)
        tk.Button(cf, text="Clear", bg=BG3, fg=TEXT, relief="flat",
                  font=("Sans", 8), cursor="hand2",
                  command=lambda: self.cookie_var.set("")).pack(side="left")

        # ── Row 4: Crawl options ──
        row4 = tk.Frame(self, bg=BG2, pady=6, padx=12)
        row4.pack(fill="x")

        # Deep crawl
        self.deep_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            row4, text="Follow child links",
            variable=self.deep_var, bg=BG2, fg=TEXT,
            selectcolor=BG3, activebackground=BG2,
            font=("Sans", 9, "bold"), cursor="hand2",
            command=self._toggle_deep_controls
        ).pack(side="left")

        tk.Label(row4, text="Depth:", bg=BG2, fg=SUBTEXT,
                 font=("Sans", 9)).pack(side="left", padx=(10, 2))
        self.depth_var = tk.IntVar(value=1)
        self.depth_spin = tk.Spinbox(
            row4, from_=1, to=5, textvariable=self.depth_var,
            width=3, bg=BG3, fg=TEXT, buttonbackground=BG3,
            relief="flat", font=("Sans", 9), state="disabled"
        )
        self.depth_spin.pack(side="left", padx=(0, 12))

        tk.Label(row4, text="Max pages:", bg=BG2, fg=SUBTEXT,
                 font=("Sans", 9)).pack(side="left")
        self.maxpages_var = tk.IntVar(value=20)
        self.maxpages_spin = tk.Spinbox(
            row4, from_=1, to=500, textvariable=self.maxpages_var,
            width=5, bg=BG3, fg=TEXT, buttonbackground=BG3,
            relief="flat", font=("Sans", 9), state="disabled"
        )
        self.maxpages_spin.pack(side="left", padx=(4, 16))

        # Probe unknown URLs
        tk.Frame(row4, bg=BG3, width=1, height=20).pack(
            side="left", padx=8, fill="y")
        self.probe_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            row4, text="Probe unknown URLs (HEAD request — slower but finds more videos)",
            variable=self.probe_var,
            bg=BG2, fg=TEXT, selectcolor=BG3, activebackground=BG2,
            font=("Sans", 9), cursor="hand2"
        ).pack(side="left", padx=4)

        # ── Row 5: Output folder ──
        row5 = tk.Frame(self, bg=BG, pady=4, padx=12)
        row5.pack(fill="x")

        tk.Label(row5, text="Save to:", bg=BG, fg=SUBTEXT,
                 font=("Sans", 9)).pack(side="left")
        self.out_var = tk.StringVar(value=str(DEFAULT_OUTPUT))
        tk.Entry(
            row5, textvariable=self.out_var, bg=BG3, fg=TEXT,
            insertbackground=TEXT, relief="flat",
            font=("Monospace", 9), width=55
        ).pack(side="left", padx=6, ipady=3)
        tk.Button(
            row5, text="Browse…", bg=BG2, fg=TEXT, relief="flat",
            cursor="hand2", font=("Sans", 9),
            command=self._pick_folder
        ).pack(side="left")

        # ── Status bar ──
        self.status_var = tk.StringVar(value="Enter a URL and click Crawl.")
        tk.Label(
            self, textvariable=self.status_var, bg=BG, fg=SUBTEXT,
            font=("Sans", 9), anchor="w", padx=12
        ).pack(fill="x")

        # ── Filter / select / naming toolbar ──
        toolbar = tk.Frame(self, bg=BG, pady=6, padx=12)
        toolbar.pack(fill="x")

        tk.Label(toolbar, text="Filter:", bg=BG, fg=SUBTEXT,
                 font=("Sans", 9)).pack(side="left")
        self.filter_var = tk.StringVar(value="all")
        for label, val in [("All","all"),("Images","images"),("Videos","videos")]:
            tk.Radiobutton(
                toolbar, text=label, variable=self.filter_var, value=val,
                bg=BG, fg=TEXT, selectcolor=BG2, activebackground=BG,
                font=("Sans", 9), command=self._apply_filter
            ).pack(side="left", padx=4)

        tk.Frame(toolbar, bg=BG3, width=1, height=20).pack(
            side="left", padx=10, fill="y")

        tk.Button(toolbar, text="Select All", bg=BG2, fg=TEXT, relief="flat",
                  cursor="hand2", font=("Sans", 9),
                  command=self._select_all).pack(side="left", padx=3)
        tk.Button(toolbar, text="Select None", bg=BG2, fg=TEXT, relief="flat",
                  cursor="hand2", font=("Sans", 9),
                  command=self._select_none).pack(side="left", padx=3)

        self.sel_label = tk.Label(toolbar, text="0 selected", bg=BG, fg=SUBTEXT,
                                  font=("Sans", 9))
        self.sel_label.pack(side="left", padx=10)

        tk.Frame(toolbar, bg=BG3, width=1, height=20).pack(
            side="left", padx=10, fill="y")

        tk.Label(toolbar, text="Naming:", bg=BG, fg=SUBTEXT,
                 font=("Sans", 9)).pack(side="left")
        self.scheme_var = tk.StringVar(value="original")
        ttk.Combobox(
            toolbar, textvariable=self.scheme_var, width=16,
            values=["original", "numbered", "prefix_original", "timestamp"],
            state="readonly", font=("Sans", 9)
        ).pack(side="left", padx=4)

        tk.Label(toolbar, text="Prefix:", bg=BG, fg=SUBTEXT,
                 font=("Sans", 9)).pack(side="left", padx=(6, 2))
        self.prefix_var = tk.StringVar(value="media")
        tk.Entry(
            toolbar, textvariable=self.prefix_var, bg=BG3, fg=TEXT,
            insertbackground=TEXT, relief="flat",
            font=("Monospace", 9), width=12
        ).pack(side="left", ipady=2)

        # ── Scrollable thumbnail grid ──
        container = tk.Frame(self, bg=BG)
        container.pack(fill="both", expand=True, padx=8, pady=4)

        self.canvas  = tk.Canvas(container, bg=BG, highlightthickness=0)
        scrollbar    = ttk.Scrollbar(container, orient="vertical",
                                     command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.grid_frame    = tk.Frame(self.canvas, bg=BG)
        self.canvas_window = self.canvas.create_window(
            (0, 0), window=self.grid_frame, anchor="nw"
        )
        self.grid_frame.bind("<Configure>",
            lambda e: self.canvas.configure(
                scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.canvas.bind_all("<Button-4>",
            lambda e: self.canvas.yview_scroll(-1, "units"))
        self.canvas.bind_all("<Button-5>",
            lambda e: self.canvas.yview_scroll(1,  "units"))

        # ── Bottom bar ──
        bottom = tk.Frame(self, bg=BG2, pady=10, padx=12)
        bottom.pack(fill="x", side="bottom")

        self.dl_btn = tk.Button(
            bottom, text="⬇  Download Selected", bg=GREEN, fg=BG,
            font=("Sans", 11, "bold"), relief="flat", padx=18, pady=6,
            cursor="hand2", command=self._start_download
        )
        self.dl_btn.pack(side="right")

        self.progress_bar = ttk.Progressbar(bottom, length=300, mode="determinate")
        self.progress_bar.pack(side="right", padx=12)

        self.dl_label = tk.Label(bottom, text="", bg=BG2, fg=TEXT,
                                 font=("Sans", 9))
        self.dl_label.pack(side="right")

    # ── UI toggles ─────────────────────────────────────────────────────────────

    def _toggle_cookie_row(self):
        if self.auth_var.get() == "manual":
            self.cookie_row.pack(fill="x", before=self._find_row(4))
        else:
            self.cookie_row.pack_forget()

    def _find_row(self, n):
        """Return the nth packed child of self (1-indexed)."""
        slaves = self.pack_slaves()
        return slaves[n] if n < len(slaves) else slaves[-1]

    def _toggle_deep_controls(self):
        state = "normal" if self.deep_var.get() else "disabled"
        self.depth_spin.config(state=state)
        self.maxpages_spin.config(state=state)

    def _pick_folder(self):
        folder = filedialog.askdirectory(initialdir=self.out_var.get())
        if folder:
            self.out_var.set(folder)

    def _on_canvas_resize(self, event):
        self.canvas.itemconfig(self.canvas_window, width=event.width)
        self._redraw_grid()

    def _stop_crawl(self):
        self._stop_flag[0] = True
        self.status_var.set("Stopping after current page…")

    # ── Session ────────────────────────────────────────────────────────────────

    def _make_session(self):
        auth = self.auth_var.get()
        if auth == "browser_firefox":
            return build_session("browser", "Firefox", "")
        elif auth == "browser_chrome":
            return build_session("browser", "Chrome", "")
        elif auth == "manual":
            return build_session("manual", "", self.cookie_var.get())
        return build_session("none", "", "")

    # ── Crawl ──────────────────────────────────────────────────────────────────

    def _start_crawl(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("No URL", "Please enter a URL to crawl.")
            return
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
            self.url_var.set(url)

        try:
            self.session = self._make_session()
        except RuntimeError as e:
            messagebox.showerror("Auth error", str(e))
            return

        self._clear_grid()
        self._stop_flag[0] = False
        self.status_var.set("Starting crawl…")
        self.crawl_btn.config(state="disabled")
        self.stop_btn.config(state="normal")

        threading.Thread(target=self._crawl_thread, args=(url,), daemon=True).start()

    def _crawl_thread(self, url):
        items = crawl_for_media(
            start_url     = url,
            session       = self.session,
            deep          = self.deep_var.get(),
            max_depth     = self.depth_var.get(),
            max_pages     = self.maxpages_var.get() if self.deep_var.get() else 1,
            probe_unknown = self.probe_var.get(),
            status_cb     = lambda msg: self.after(0, lambda m=msg: self.status_var.set(m)),
            stop_flag     = self._stop_flag,
        )
        self.after(0, lambda: self._on_crawl_done(items))

    def _on_crawl_done(self, items):
        self.crawl_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.media_list = items
        self.selected   = set()

        if not items:
            self.status_var.set(
                "No media found. Try enabling 'Probe unknown URLs' or "
                "check auth settings if the page requires a login."
            )
            return

        imgs = sum(1 for i in items if i["type"] == "image")
        vids = sum(1 for i in items if i["type"] == "video")
        self.status_var.set(
            f"Found {len(items)} files — {imgs} images, {vids} videos. "
            "Loading thumbnails…"
        )
        self._apply_filter()
        threading.Thread(target=self._load_thumbs_thread, daemon=True).start()

    def _load_thumbs_thread(self):
        for i, item in enumerate(self.media_list):
            if self._stop_flag[0]:
                break
            if item["type"] == "image":
                thumb = fetch_thumbnail(item["url"], self.session)
                item["thumb"] = thumb
                self.after(0, lambda idx=i: self._update_thumb(idx))
        self.after(0, lambda: self.status_var.set(
            f"Ready — {len(self.media_list)} files. "
            "Click thumbnails to select, then Download."
        ))

    def _update_thumb(self, idx):
        for card in self.card_frames:
            if card.media_index == idx:
                thumb = self.media_list[idx].get("thumb")
                if thumb:
                    self.thumb_refs.append(thumb)
                    card.img_label.config(image=thumb, text="")
                break

    # ── Grid ───────────────────────────────────────────────────────────────────

    def _clear_grid(self):
        for w in self.grid_frame.winfo_children():
            w.destroy()
        self.card_frames.clear()
        self.thumb_refs.clear()
        self.selected.clear()
        self._update_sel_label()

    def _apply_filter(self):
        filt  = self.filter_var.get()
        shown = [
            (i, item) for i, item in enumerate(self.media_list)
            if filt == "all"
            or (filt == "images" and item["type"] == "image")
            or (filt == "videos" and item["type"] == "video")
        ]
        self._draw_grid(shown)

    def _draw_grid(self, items_with_index):
        for w in self.grid_frame.winfo_children():
            w.destroy()
        self.card_frames.clear()

        cols = max(1, self.canvas.winfo_width() // (THUMB_SIZE[0] + 20))
        for pos, (orig_idx, item) in enumerate(items_with_index):
            card = self._make_card(orig_idx, item)
            card.grid(row=pos // cols, column=pos % cols, padx=6, pady=6)
            self.card_frames.append(card)

    def _redraw_grid(self):
        if self.media_list:
            self._apply_filter()

    def _make_card(self, orig_idx, item):
        is_sel = orig_idx in self.selected
        card   = tk.Frame(self.grid_frame,
                          bg=SELECTED if is_sel else BG2,
                          padx=2, pady=2, cursor="hand2")
        card.media_index = orig_idx

        inner = tk.Frame(card, bg=BG2)
        inner.pack()

        thumb = item.get("thumb")
        if thumb:
            img_label = tk.Label(inner, image=thumb, bg=BG2, cursor="hand2")
            self.thumb_refs.append(thumb)
        else:
            placeholder = "🎬" if item["type"] == "video" else "⏳"
            img_label = tk.Label(
                inner, text=placeholder,
                width=THUMB_SIZE[0]//8, height=THUMB_SIZE[1]//16,
                bg=BG3, fg=SUBTEXT, font=("Sans", 28), cursor="hand2"
            )
        img_label.pack()
        card.img_label = img_label

        name = item["filename"]
        display = name if len(name) <= 20 else name[:18] + "…"
        tk.Label(inner, text=display, bg=BG2, fg=TEXT,
                 font=("Sans", 8), wraplength=THUMB_SIZE[0]).pack(pady=(2, 0))

        page_path = urlparse(item.get("page", "")).path[:30] or "/"
        tk.Label(inner, text=page_path, bg=BG2, fg=BG3,
                 font=("Sans", 7)).pack()

        badge_col = ACCENT if item["type"] == "image" else "#cba6f7"
        tk.Label(inner, text=item["type"].upper(), bg=badge_col, fg=BG,
                 font=("Sans", 7, "bold"), padx=4).pack(pady=(1, 4))

        toggle = lambda e, idx=orig_idx: self._toggle_select(idx)
        for w in (card, inner, img_label):
            w.bind("<Button-1>", toggle)

        return card

    def _toggle_select(self, idx):
        if idx in self.selected:
            self.selected.discard(idx)
        else:
            self.selected.add(idx)
        for card in self.card_frames:
            if card.media_index == idx:
                card.config(bg=SELECTED if idx in self.selected else BG2)
                break
        self._update_sel_label()

    def _select_all(self):
        for card in self.card_frames:
            self.selected.add(card.media_index)
            card.config(bg=SELECTED)
        self._update_sel_label()

    def _select_none(self):
        for card in self.card_frames:
            self.selected.discard(card.media_index)
            card.config(bg=BG2)
        self._update_sel_label()

    def _update_sel_label(self):
        self.sel_label.config(text=f"{len(self.selected)} selected")

    # ── Download ───────────────────────────────────────────────────────────────

    def _start_download(self):
        if not self.selected:
            messagebox.showinfo("Nothing selected",
                                "Click thumbnails to select files first.")
            return

        output_dir = Path(self.out_var.get()).expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        chosen = [self.media_list[i] for i in sorted(self.selected)]
        scheme = self.scheme_var.get()
        prefix = re.sub(r"[^\w\-]", "_", self.prefix_var.get().strip()) or "media"
        names  = [make_filename(item, i+1, prefix, scheme)
                  for i, item in enumerate(chosen)]

        self.dl_btn.config(state="disabled")
        self.progress_bar["value"]   = 0
        self.progress_bar["maximum"] = len(chosen)

        threading.Thread(
            target=self._download_thread,
            args=(chosen, names, output_dir),
            daemon=True,
        ).start()

    def _download_thread(self, chosen, names, output_dir):
        results = []
        for i, (item, name) in enumerate(zip(chosen, names)):
            ok, msg = download_file(item, output_dir, self.session, name)
            results.append((ok, msg))
            self.after(0, lambda v=i+1, m=msg: self._on_file_done(v, m))
        self.after(0, lambda: self._on_all_done(results, output_dir))

    def _on_file_done(self, count, msg):
        self.progress_bar["value"] = count
        self.dl_label.config(text=msg[:55])

    def _on_all_done(self, results, output_dir):
        self.dl_btn.config(state="normal")
        ok   = sum(1 for o, _ in results if o)
        fail = len(results) - ok
        messagebox.showinfo(
            "Download complete",
            f"Downloaded: {ok}\nFailed: {fail}\nSaved to: {output_dir}"
        )
        self.status_var.set(
            f"Done — {ok} downloaded, {fail} failed → {output_dir}"
        )


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="GUI Media Crawler & Downloader")
    parser.add_argument("url", nargs="?", default="", help="Optional starting URL")
    args = parser.parse_args()
    App(initial_url=args.url).mainloop()


if __name__ == "__main__":
    main()
