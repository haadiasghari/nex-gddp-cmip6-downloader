#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
NEX-GDDP-CMIP6 Downloader - GUI Edition (UX v1.1)

Implements agreed UX roadmap:
- Wizard-lite: single primary "Next Step" button + Step x/5
- Build Queue in Download Queue header, disabled until ready + tooltip via info icon
- Selection Summary (right side) always visible
- Human-readable Status bar
- Left Catalog panel fully scrollable (small screens / Windows scaling safe)
- Log line limit + adaptive UI pump
- Clean shutdown + close sessions

Dependencies:
- requests
- tkinter (usually bundled)
"""

from __future__ import annotations

import os
import re
import time
import math
import random
import queue
import threading
import requests
import xml.etree.ElementTree as ET

from urllib.parse import urljoin
from typing import Optional, List, Dict, Tuple
from concurrent.futures import ThreadPoolExecutor

import tkinter as tk
from tkinter import ttk, filedialog, messagebox


# ============================================================
# CONFIG
# ============================================================

BASE_CATALOG = "https://ds.nccs.nasa.gov/thredds/catalog/AMES/NEX/GDDP-CMIP6"
BASE_CATALOG_XML = BASE_CATALOG + "/catalog.xml"
FILES_BASE = "https://ds.nccs.nasa.gov/thredds/fileServer/"

HEADERS = {"User-Agent": "Mozilla/5.0 (NEX-GDDP-CMIP6-downloader GUI UX)"}

REQUEST_TIMEOUT = 30
CATALOG_REQUEST_DELAY = 0.12

DEFAULT_WORKERS = 4
DEFAULT_SEGMENTS = 8

MAX_RETRIES_PER_CATALOG = 5
MAX_RETRIES_PER_FILE = 10
MAX_RETRIES_PER_PART = 6

RETRY_BACKOFF_BASE = 1.6
RETRY_BACKOFF_MAX = 60.0

DEFAULT_VERSION_FILTER = "_v2.0.nc"
CHUNK_SIZE = 1024 * 64

# UI
MAX_LOG_LINES = 5000
TRIM_LOG_TO_LINES = 4500
QUEUE_CONFIRM_THRESHOLD = 1000
QUEUE_TABLE_SHOW_LIMIT = 5000


# ============================================================
# GLOBAL EVENTS
# ============================================================

resume_event = threading.Event()   # pause/resume
stop_event = threading.Event()     # stop ASAP

_thread_local = threading.local()
_sessions_lock = threading.Lock()
_all_sessions: "set[requests.Session]" = set()


def get_session() -> requests.Session:
    s = getattr(_thread_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update(HEADERS)
        _thread_local.session = s
        with _sessions_lock:
            _all_sessions.add(s)
    return s


def close_all_sessions():
    with _sessions_lock:
        sessions = list(_all_sessions)
        _all_sessions.clear()
    for s in sessions:
        try:
            s.close()
        except Exception:
            pass


# ============================================================
# RETRY HELPERS
# ============================================================

def _sleep_backoff(attempt: int, base: float = RETRY_BACKOFF_BASE, cap: float = RETRY_BACKOFF_MAX):
    backoff = min(cap, base ** attempt)
    jitter = random.uniform(0.0, backoff * 0.15)
    time.sleep(backoff + jitter)


def _retry_after_seconds(resp: requests.Response) -> Optional[float]:
    ra = resp.headers.get("Retry-After")
    if not ra:
        return None
    try:
        return float(ra)
    except Exception:
        return None


# ============================================================
# XML / THREDDS HELPERS
# ============================================================

def parse_xml(text: str) -> Optional[ET.Element]:
    try:
        return ET.fromstring(text.encode("utf-8"))
    except Exception:
        return None


def local_tag(elem: ET.Element) -> str:
    if "}" in elem.tag:
        return elem.tag.split("}", 1)[1]
    return elem.tag


def fetch_text(url: str, max_retries: int, post_log) -> Optional[str]:
    session = get_session()
    for attempt in range(1, max_retries + 1):
        if stop_event.is_set():
            return None
        try:
            r = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)

            if r.status_code in (429, 500, 502, 503, 504):
                ra = _retry_after_seconds(r)
                if ra is not None:
                    post_log(f"[WARN] {r.status_code} retry-after {ra}s: {url}")
                    time.sleep(min(ra, RETRY_BACKOFF_MAX))
                else:
                    post_log(f"[WARN] {r.status_code} transient: {url} (attempt {attempt}/{max_retries})")
                    _sleep_backoff(attempt)
                continue

            r.raise_for_status()
            time.sleep(CATALOG_REQUEST_DELAY)
            return r.text

        except Exception as e:
            if attempt == max_retries:
                post_log(f"[WARN] fetch_text failed: {url} -> {e}")
                return None
            post_log(f"[WARN] fetch_text error: {url} -> {e} (attempt {attempt}/{max_retries})")
            _sleep_backoff(attempt)
    return None


def parse_catalog_refs(xml_text: str) -> List[Dict[str, Optional[str]]]:
    root = parse_xml(xml_text)
    if root is None:
        return []
    refs: List[Dict[str, Optional[str]]] = []
    for el in root.iter():
        if local_tag(el).lower() == "catalogref":
            title = None
            href = None
            for k, v in el.attrib.items():
                lk = k.lower()
                if lk.endswith("title"):
                    title = v
                elif lk.endswith("href"):
                    href = v
            for ch in el:
                if local_tag(ch).lower() == "title" and ch.text:
                    title = ch.text.strip()
            refs.append({"title": title, "href": href})

    seen = set()
    out: List[Dict[str, Optional[str]]] = []
    for r in refs:
        key = (r.get("title"), r.get("href"))
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def parse_urlpaths(xml_text: str) -> List[str]:
    root = parse_xml(xml_text)
    if root is None:
        return []
    paths: List[str] = []
    for el in root.iter():
        tag = local_tag(el).lower()
        if tag == "urlpath" and el.text:
            paths.append(el.text.strip())
        elif tag == "dataset" and "urlPath" in el.attrib:
            paths.append(el.attrib["urlPath"].strip())
    seen = set()
    out: List[str] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def href_to_catalog_xml(parent_catalog: str, href: Optional[str], name_guess: Optional[str]) -> Optional[str]:
    if href:
        base = parent_catalog.rsplit("/", 1)[0] + "/"
        url = urljoin(base, href)
        if url.endswith(".html"):
            return url.replace(".html", ".xml")
        if url.endswith(".xml"):
            return url
        if url.endswith("/"):
            return url + "catalog.xml"
        return url + "/catalog.xml"
    if name_guess:
        return f"{BASE_CATALOG}/{name_guess}/catalog.xml"
    return None


# ============================================================
# CATALOG TRAVERSAL
# ============================================================

def scan_models(post_log) -> List[Dict[str, Optional[str]]]:
    post_log(f"[INFO] Fetching top catalog: {BASE_CATALOG_XML}")
    txt = fetch_text(BASE_CATALOG_XML, MAX_RETRIES_PER_CATALOG, post_log)
    if not txt:
        return []
    refs = parse_catalog_refs(txt)
    if not refs:
        return []

    models: List[Dict[str, Optional[str]]] = []

    if len(refs) == 1:
        container = refs[0]
        cont_title = container.get("title")
        cont_href = container.get("href")
        cont_cat = href_to_catalog_xml(BASE_CATALOG_XML, cont_href, cont_title)
        if not cont_cat:
            return []
        post_log(f"[INFO] Descending into container: {cont_title}")
        txt2 = fetch_text(cont_cat, MAX_RETRIES_PER_CATALOG, post_log)
        if not txt2:
            return []
        refs2 = parse_catalog_refs(txt2)
        for r in refs2:
            title = r.get("title")
            href = r.get("href")
            if not title:
                continue
            cat = href_to_catalog_xml(cont_cat, href, title)
            models.append({"title": title.rstrip("/"), "href": href, "catalog_xml": cat})
        return models

    for r in refs:
        title = r.get("title")
        href = r.get("href")
        if not title:
            continue
        cat = href_to_catalog_xml(BASE_CATALOG_XML, href, title)
        models.append({"title": title.rstrip("/"), "href": href, "catalog_xml": cat})
    return models


def list_scenarios_for_model(model_entry: Dict[str, Optional[str]], post_log) -> List[Dict[str, Optional[str]]]:
    model_cat = model_entry.get("catalog_xml")
    if not model_cat:
        return []
    post_log(f"[INFO] Fetching model catalog: {model_cat}")
    txt = fetch_text(model_cat, MAX_RETRIES_PER_CATALOG, post_log)
    if not txt:
        return []
    refs = parse_catalog_refs(txt)
    out: List[Dict[str, Optional[str]]] = []
    for r in refs:
        title = r.get("title")
        href = r.get("href")
        if not title:
            continue
        cat = href_to_catalog_xml(model_cat, href, title)
        out.append({"title": title.rstrip("/"), "href": href, "catalog_xml": cat})
    return out


def list_ensembles_for_scenario(scenario_entry: Dict[str, Optional[str]], post_log) -> List[Dict[str, Optional[str]]]:
    scen_cat = scenario_entry.get("catalog_xml")
    if not scen_cat:
        return []
    post_log(f"[INFO] Fetching scenario catalog: {scen_cat}")
    txt = fetch_text(scen_cat, MAX_RETRIES_PER_CATALOG, post_log)
    if not txt:
        return []
    refs = parse_catalog_refs(txt)
    ensembles: List[Dict[str, Optional[str]]] = []
    for r in refs:
        title = r.get("title")
        href = r.get("href")
        if not title:
            continue
        cat = href_to_catalog_xml(scen_cat, href, title)
        ensembles.append({"title": title.rstrip("/"), "href": href, "catalog_xml": cat})

    regex = re.compile(r"^r\d+i\d+p\d+f\d+$", re.IGNORECASE)
    filtered = [e for e in ensembles if regex.match(e.get("title") or "")]
    return filtered if filtered else ensembles


def list_parameters_for_ensemble(ensemble_entry: Dict[str, Optional[str]], post_log) -> List[Dict[str, Optional[str]]]:
    ens_cat = ensemble_entry.get("catalog_xml")
    if not ens_cat:
        return []
    post_log(f"[INFO] Fetching ensemble catalog: {ens_cat}")
    txt = fetch_text(ens_cat, MAX_RETRIES_PER_CATALOG, post_log)
    if not txt:
        return []
    refs = parse_catalog_refs(txt)
    params: List[Dict[str, Optional[str]]] = []
    for r in refs:
        title = r.get("title")
        href = r.get("href")
        if not title:
            continue
        cat = href_to_catalog_xml(ens_cat, href, title)
        params.append({"title": title.rstrip("/"), "href": href, "catalog_xml": cat})
    return params


def list_urlpaths_for_parameter(parameter_entry: Dict[str, Optional[str]], post_log) -> List[str]:
    param_cat = parameter_entry.get("catalog_xml")
    if not param_cat:
        return []
    post_log(f"[INFO] Fetching parameter catalog: {param_cat}")
    txt = fetch_text(param_cat, MAX_RETRIES_PER_CATALOG, post_log)
    if not txt:
        return []
    return parse_urlpaths(txt)


# ============================================================
# NAME / YEAR / PATH HELPERS
# ============================================================

def build_fileserver_url(urlpath: str) -> str:
    if urlpath.startswith("/"):
        urlpath = urlpath[1:]
    return urljoin(FILES_BASE, urlpath)


def sanitize_folder_name(name: str) -> str:
    name = name.strip()
    name = re.sub(r"[\\/:\*\?\"<>\|]", "_", name)
    name = name.replace(" ", "_")
    return name


def file_years_from_filename(fname: str) -> Tuple[Optional[int], Optional[int]]:
    m = re.search(r"_(\d{8})-(\d{8})\.(?:nc|NC)$", fname)
    if m:
        return int(m.group(1)[:4]), int(m.group(2)[:4])
    m = re.search(r"_(\d{4})-(\d{4})\.(?:nc|NC)$", fname)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"(\d{4})", fname)
    if m:
        y = int(m.group(1))
        if 1800 <= y <= 2300:
            return y, y
    return None, None


def years_overlap(file_y0: Optional[int], file_y1: Optional[int], req_y0: int, req_y1: int) -> bool:
    if file_y0 is None and file_y1 is None:
        return False
    y0 = file_y0 if file_y0 is not None else file_y1
    y1 = file_y1 if file_y1 is not None else file_y0
    if y0 is None or y1 is None:
        return False
    return not (y1 < req_y0 or y0 > req_y1)


# ============================================================
# RANGE / SIZE DETECTION
# ============================================================

def _head_only(url: str) -> Tuple[Optional[int], bool, bool]:
    session = get_session()
    try:
        r = session.head(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        if r.status_code == 200:
            total_h = r.headers.get("Content-Length")
            accept = (r.headers.get("Accept-Ranges", "").lower() == "bytes")
            total_i = int(total_h) if total_h and total_h.isdigit() else None
            return total_i, accept, True
    except Exception:
        pass
    return None, False, False


def probe_range_support(url: str) -> Tuple[Optional[int], bool]:
    session = get_session()
    try:
        headers = dict(HEADERS)
        headers["Range"] = "bytes=0-0"
        r = session.get(url, headers=headers, timeout=REQUEST_TIMEOUT, stream=True, allow_redirects=True)
        if r.status_code == 206:
            cr = r.headers.get("Content-Range", "")
            m = re.match(r"bytes\s+\d+-\d+/(\d+|\*)", cr, re.IGNORECASE)
            if m:
                total_str = m.group(1)
                total = int(total_str) if total_str.isdigit() else None
                r.close()
                return total, True
        r.close()
    except Exception:
        pass
    return None, False


def head_info(url: str) -> Tuple[Optional[int], bool]:
    total, _accept, head_ok = _head_only(url)
    if head_ok:
        t2, ok = probe_range_support(url)
        if t2 is not None:
            total = t2
        return total, ok

    t2, ok = probe_range_support(url)
    if t2 is not None:
        return t2, ok

    session = get_session()
    try:
        r = session.get(url, timeout=REQUEST_TIMEOUT, stream=True, allow_redirects=True)
        r.raise_for_status()
        total_h = r.headers.get("Content-Length")
        total_i = int(total_h) if total_h and total_h.isdigit() else None
        r.close()
        return total_i, False
    except Exception:
        return None, False


# ============================================================
# DOWNLOAD CORE (GUI callbacks)
# ============================================================

def cleanup_partials(dest_dir: str, fname: str):
    try:
        for f in os.listdir(dest_dir):
            if (
                f == fname + ".part"
                or f.startswith("." + fname + ".part")
                or f.endswith(".partmerge")
            ):
                try:
                    os.remove(os.path.join(dest_dir, f))
                except Exception:
                    pass
    except Exception:
        pass


def merge_parts(part_paths: List[str], final_path: str):
    tmp = final_path + ".partmerge"
    with open(tmp, "wb") as fw:
        for p in part_paths:
            with open(p, "rb") as fr:
                while True:
                    buf = fr.read(CHUNK_SIZE)
                    if not buf:
                        break
                    fw.write(buf)
    os.replace(tmp, final_path)
    for p in part_paths:
        try:
            os.remove(p)
        except Exception:
            pass


def _stream_to_file(resp: requests.Response, fp, progress_cb) -> int:
    wrote = 0
    for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
        resume_event.wait()
        if stop_event.is_set():
            break
        if chunk:
            fp.write(chunk)
            wrote += len(chunk)
            progress_cb(len(chunk))
    return wrote


def download_part(url: str, start: int, end: int, part_path: str,
                  progress_cb, post_log, max_retries: int = MAX_RETRIES_PER_PART) -> bool:
    session = get_session()

    for attempt in range(1, max_retries + 1):
        if stop_event.is_set():
            return False

        try:
            existing = os.path.getsize(part_path) if os.path.exists(part_path) else 0
            real_start = start + existing
            if real_start > end:
                return True

            headers = dict(HEADERS)
            headers["Range"] = f"bytes={real_start}-{end}"

            with session.get(url, headers=headers, stream=True, timeout=REQUEST_TIMEOUT, allow_redirects=True) as r:
                if r.status_code != 206:
                    raise RuntimeError(f"Server ignored Range (status={r.status_code})")

                cr = r.headers.get("Content-Range", "")
                m = re.match(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", cr, re.IGNORECASE)
                if m:
                    got_start = int(m.group(1))
                    if got_start != real_start:
                        raise RuntimeError(f"Content-Range mismatch: got {got_start}, expected {real_start}")

                mode = "ab" if existing > 0 else "wb"
                with open(part_path, mode) as f:
                    _stream_to_file(r, f, progress_cb)

            if stop_event.is_set():
                return False
            return True

        except Exception as e:
            if attempt == max_retries:
                post_log(f"[WARN] part failed ({os.path.basename(part_path)}): {e}")
                return False
            post_log(f"[WARN] part error ({os.path.basename(part_path)}): {e} retry {attempt}/{max_retries}")
            _sleep_backoff(attempt)

    return False


def download_single_stream(url: str, out_path: str, total_known: bool,
                           progress_cb, set_restart_cb, post_log,
                           max_retries: int = MAX_RETRIES_PER_PART) -> bool:
    session = get_session()
    tmp = out_path + ".part"

    for attempt in range(1, max_retries + 1):
        if stop_event.is_set():
            return False

        try:
            existing = os.path.getsize(tmp) if os.path.exists(tmp) else 0
            headers = dict(HEADERS)
            wants_resume = existing > 0
            if wants_resume:
                headers["Range"] = f"bytes={existing}-"

            with session.get(url, headers=headers, stream=True, timeout=REQUEST_TIMEOUT, allow_redirects=True) as r:
                if wants_resume and r.status_code != 206:
                    post_log(f"[WARN] Server ignored Range for resume; restarting {os.path.basename(out_path)}")
                    try:
                        os.remove(tmp)
                    except Exception:
                        pass
                    set_restart_cb(total_known)
                    continue

                if not wants_resume and r.status_code not in (200, 206):
                    r.raise_for_status()

                mode = "ab" if wants_resume else "wb"
                with open(tmp, mode) as f:
                    _stream_to_file(r, f, progress_cb)

            if stop_event.is_set():
                return False

            os.replace(tmp, out_path)
            return True

        except Exception as e:
            if attempt == max_retries:
                post_log(f"[WARN] single-stream failed ({os.path.basename(out_path)}): {e}")
                return False
            post_log(f"[WARN] single-stream error ({os.path.basename(out_path)}): {e} retry {attempt}/{max_retries}")
            _sleep_backoff(attempt)

    return False


def download_file_robust(url: str, dest_dir: str,
                         segments: int, workers: int,
                         max_retries: int,
                         progress_set_total_cb, progress_add_cb, progress_set_abs_cb,
                         post_log, set_restart_cb,
                         cleanup_on_stop: bool = True) -> Tuple[bool, str]:
    os.makedirs(dest_dir, exist_ok=True)
    fname = os.path.basename(url)
    final_path = os.path.join(dest_dir, fname)

    if os.path.exists(final_path):
        return True, final_path

    total, supports_range = head_info(url)
    total_known = bool(total and total > 0)
    progress_set_total_cb(total if total_known else None)

    existing_bytes = 0
    part_paths: List[str] = []

    if supports_range and total_known and segments > 1:
        for i in range(segments):
            p = os.path.join(dest_dir, f".{fname}.part{i}")
            if os.path.exists(p):
                existing_bytes += os.path.getsize(p)
        progress_set_abs_cb(existing_bytes)
    else:
        tmp = final_path + ".part"
        if os.path.exists(tmp):
            existing_bytes = os.path.getsize(tmp)
        progress_set_abs_cb(existing_bytes)

    for attempt in range(1, max_retries + 1):
        if stop_event.is_set():
            if cleanup_on_stop:
                cleanup_partials(dest_dir, fname)
                post_log(f"[INFO] Removed partial files for {fname}")
            return False, "Stopped by user"

        try:
            if not total_known:
                t2, sr2 = head_info(url)
                if t2:
                    total = t2
                    supports_range = sr2
                    total_known = True
                    progress_set_total_cb(total)

            if supports_range and total_known and segments > 1 and total is not None:
                part_size = math.ceil(total / segments)
                ranges: List[Tuple[int, int]] = []
                for i in range(segments):
                    s = i * part_size
                    e = min(total - 1, (i + 1) * part_size - 1)
                    if s <= e:
                        ranges.append((s, e))

                part_paths = [os.path.join(dest_dir, f".{fname}.part{i}") for i in range(len(ranges))]
                results: List[bool] = []

                max_workers = max(1, min(workers, len(ranges)))
                with ThreadPoolExecutor(max_workers=max_workers) as pool:
                    futs = []
                    for idx, (s, e) in enumerate(ranges):
                        if stop_event.is_set():
                            break
                        futs.append(pool.submit(
                            download_part, url, s, e, part_paths[idx],
                            progress_add_cb, post_log
                        ))
                    for f in futs:
                        try:
                            results.append(f.result())
                        except Exception:
                            results.append(False)

                if stop_event.is_set():
                    if cleanup_on_stop:
                        cleanup_partials(dest_dir, fname)
                        post_log(f"[INFO] Removed partial files for {fname}")
                    return False, "Stopped by user"

                if all(results):
                    merge_parts(part_paths, final_path)
                    return True, final_path

                existing_bytes = 0
                for p in part_paths:
                    if os.path.exists(p):
                        existing_bytes += os.path.getsize(p)
                progress_set_abs_cb(existing_bytes)

                post_log(f"[WARN] Incomplete segmented download for {fname}, retrying (attempt {attempt}/{max_retries})")
                _sleep_backoff(attempt)
                continue

            ok = download_single_stream(
                url, final_path, total_known,
                progress_add_cb, set_restart_cb, post_log
            )
            if ok:
                return True, final_path

            if stop_event.is_set():
                if cleanup_on_stop:
                    cleanup_partials(dest_dir, fname)
                    post_log(f"[INFO] Removed partial files for {fname}")
                return False, "Stopped by user"

            tmp = final_path + ".part"
            existing_bytes = os.path.getsize(tmp) if os.path.exists(tmp) else 0
            progress_set_abs_cb(existing_bytes)

            post_log(f"[WARN] download failed for {fname}, retrying (attempt {attempt}/{max_retries})")
            _sleep_backoff(attempt)

        except Exception as e:
            post_log(f"[WARN] File-level error for {fname}: {e} (attempt {attempt}/{max_retries})")
            _sleep_backoff(attempt)

    return False, f"Failed after {max_retries} attempts"


# ============================================================
# UI HELPERS: Tooltip + ScrollableFrame
# ============================================================

class ToolTip:
    def __init__(self, widget: tk.Widget, text: str, delay_ms: int = 400):
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self._after_id = None
        self._tip = None

        widget.bind("<Enter>", self._schedule)
        widget.bind("<Leave>", self._hide)
        widget.bind("<ButtonPress>", self._hide)

    def set_text(self, text: str):
        self.text = text

    def _schedule(self, _e=None):
        self._cancel()
        self._after_id = self.widget.after(self.delay_ms, self._show)

    def _cancel(self):
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _show(self):
        if self._tip is not None:
            return
        if not self.text:
            return

        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6

        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")

        lbl = ttk.Label(self._tip, text=self.text, justify="left",
                        padding=(8, 5), relief="solid", borderwidth=1)
        lbl.pack()

    def _hide(self, _e=None):
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None


class ScrollableFrame(ttk.Frame):
    """Canvas + inner frame: scrolls entire left panel content."""
    def __init__(self, parent, *args, **kwargs):
        super().__init__(parent, *args, **kwargs)

        self.canvas = tk.Canvas(self, highlightthickness=0)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vsb.set)

        self.vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.inner = ttk.Frame(self.canvas)
        self._window_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")

        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)

        self.canvas.bind("<Enter>", self._bind_mousewheel)
        self.canvas.bind("<Leave>", self._unbind_mousewheel)

    def _on_inner_configure(self, _event):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self.canvas.itemconfigure(self._window_id, width=event.width)

    def _on_mousewheel(self, event):
        if hasattr(event, "delta") and event.delta:
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _on_linux_scroll_up(self, _event):
        self.canvas.yview_scroll(-1, "units")

    def _on_linux_scroll_down(self, _event):
        self.canvas.yview_scroll(1, "units")

    def _bind_mousewheel(self, _event):
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind_all("<Button-4>", self._on_linux_scroll_up)
        self.canvas.bind_all("<Button-5>", self._on_linux_scroll_down)

    def _unbind_mousewheel(self, _event):
        self.canvas.unbind_all("<MouseWheel>")
        self.canvas.unbind_all("<Button-4>")
        self.canvas.unbind_all("<Button-5>")


# ============================================================
# GUI APP
# ============================================================

class App(tk.Tk):
    STAGES = ["models", "scenarios", "ensembles", "params", "queue"]  # 5 steps

    def __init__(self):
        super().__init__()
        self.title("NEX-GDDP-CMIP6 Downloader")
        self.geometry("1260x860")
        self.minsize(1100, 700)

        # UI queue (thread -> UI)
        self.uiq: "queue.Queue[dict]" = queue.Queue()

        # Data caches
        self.models: List[Dict[str, Optional[str]]] = []
        self.scenarios: List[Dict[str, Optional[str]]] = []
        self.ensembles: List[Dict[str, Optional[str]]] = []
        self.params_by_ens: Dict[str, Dict[str, Dict[str, Optional[str]]]] = {}
        self.param_titles: List[str] = []
        self.download_items: List[Dict] = []

        # Threads
        self.download_thread: Optional[threading.Thread] = None
        self.catalog_thread: Optional[threading.Thread] = None

        # State
        self.is_downloading = False
        self.is_paused = False
        self.closing = False

        self.stage = "models"  # wizard stage

        # events init
        resume_event.set()
        stop_event.clear()

        self._build_ui()
        self._bind_events()

        self.after(60, self._pump_ui_queue)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self._log("[INFO] Ready.")
        self._set_status("Ready.")
        self._refresh_selection_summary()
        self._refresh_wizard()

    # ---------------- UI layout ----------------

    def _build_ui(self):
        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)

        # Settings
        settings = ttk.LabelFrame(root, text="Settings", padding=10)
        settings.pack(fill="x")

        self.out_dir_var = tk.StringVar(value=os.path.abspath("cmip6_downloads"))
        self.version_filter_var = tk.StringVar(value=DEFAULT_VERSION_FILTER)
        self.workers_var = tk.IntVar(value=DEFAULT_WORKERS)
        self.segments_var = tk.IntVar(value=DEFAULT_SEGMENTS)
        self.y0_var = tk.StringVar(value="1950")
        self.y1_var = tk.StringVar(value="2100")
        self.common_params_only_var = tk.BooleanVar(value=True)
        self.auto_advance_var = tk.BooleanVar(value=False)

        ttk.Label(settings, text="Output dir:").grid(row=0, column=0, sticky="w")
        ttk.Entry(settings, textvariable=self.out_dir_var, width=70).grid(row=0, column=1, sticky="we", padx=(6, 6))
        ttk.Button(settings, text="Browse...", command=self.browse_dir).grid(row=0, column=2, sticky="e")

        ttk.Label(settings, text="Version filter:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(settings, textvariable=self.version_filter_var, width=24).grid(row=1, column=1, sticky="w", pady=(8, 0), padx=(6, 0))

        ttk.Label(settings, text="Workers:").grid(row=1, column=1, sticky="e", pady=(8, 0), padx=(0, 190))
        ttk.Spinbox(settings, from_=1, to=32, textvariable=self.workers_var, width=5).grid(row=1, column=1, sticky="e", pady=(8, 0), padx=(0, 135))

        ttk.Label(settings, text="Segments:").grid(row=1, column=1, sticky="e", pady=(8, 0), padx=(0, 70))
        ttk.Spinbox(settings, from_=1, to=32, textvariable=self.segments_var, width=5).grid(row=1, column=1, sticky="e", pady=(8, 0), padx=(0, 15))

        yrs = ttk.Frame(settings)
        yrs.grid(row=1, column=2, sticky="e", pady=(8, 0))
        ttk.Label(yrs, text="Years:").pack(side="left")
        ttk.Entry(yrs, textvariable=self.y0_var, width=6).pack(side="left", padx=(6, 4))
        ttk.Label(yrs, text="to").pack(side="left")
        ttk.Entry(yrs, textvariable=self.y1_var, width=6).pack(side="left", padx=(4, 0))

        ttk.Checkbutton(
            settings,
            text="Only common parameters across selected ensembles",
            variable=self.common_params_only_var,
            command=self._on_settings_changed,
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 0))

        ttk.Checkbutton(
            settings,
            text="Auto advance steps (Wizard)",
            variable=self.auto_advance_var
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))

        settings.columnconfigure(1, weight=1)

        # Middle: paned
        mid = ttk.PanedWindow(root, orient="horizontal")
        mid.pack(fill="both", expand=True, pady=(10, 10))

        # Left: scrollable catalog selection
        left_container = ttk.Frame(mid)
        mid.add(left_container, weight=0)

        left_scroll = ScrollableFrame(left_container)
        left_scroll.pack(fill="both", expand=True)

        sel = ttk.LabelFrame(left_scroll.inner, text="Catalog Selection", padding=10)
        sel.pack(fill="both", expand=True)

        # Wizard block
        wiz = ttk.LabelFrame(sel, text="Wizard", padding=10)
        wiz.pack(fill="x", pady=(0, 10))

        top = ttk.Frame(wiz)
        top.pack(fill="x")

        self.wiz_step_var = tk.StringVar(value="Step 1/5")
        self.wiz_label_var = tk.StringVar(value="Load Models")

        ttk.Label(top, textvariable=self.wiz_step_var).pack(side="left")
        ttk.Label(top, text="•").pack(side="left", padx=6)
        ttk.Label(top, textvariable=self.wiz_label_var).pack(side="left")

        btns = ttk.Frame(wiz)
        btns.pack(fill="x", pady=(8, 0))

        self.btn_wizard = ttk.Button(btns, text="Load Models", command=self.on_wizard_next)
        self.btn_wizard.pack(side="left")

        self.lbl_wizard_info = ttk.Label(btns, text="ⓘ")
        self.lbl_wizard_info.pack(side="left", padx=8)
        self.tt_wizard = ToolTip(self.lbl_wizard_info, "Wizard guidance.")

        # Power-user buttons (still available)
        adv = ttk.LabelFrame(sel, text="Quick Actions (Power users)", padding=10)
        adv.pack(fill="x", pady=(0, 10))

        row = ttk.Frame(adv)
        row.pack(fill="x")
        self.btn_load_models = ttk.Button(row, text="Load Models", command=self.load_models)
        self.btn_load_scen = ttk.Button(row, text="Load Scenarios", command=self.load_scenarios)
        self.btn_load_ens = ttk.Button(row, text="Load Ensembles", command=self.load_ensembles)
        self.btn_load_params = ttk.Button(row, text="Load Parameters", command=self.load_parameters)
        self.btn_load_models.pack(side="left")
        self.btn_load_scen.pack(side="left", padx=6)
        self.btn_load_ens.pack(side="left", padx=6)
        self.btn_load_params.pack(side="left", padx=6)

        # Lists
        self.lb_models = self._make_listbox(sel, "Models (select one)")
        self.lb_scenarios = self._make_listbox(sel, "Scenarios (select one)")
        self.lb_ensembles = self._make_listbox(sel, "Ensembles (multi-select)")
        self.lb_params = self._make_listbox(sel, "Parameters (multi-select)")

        # Right side
        right = ttk.Frame(mid)
        mid.add(right, weight=1)

        # Selection summary (UX-gold)
        summ = ttk.LabelFrame(right, text="Selection Summary", padding=10)
        summ.pack(fill="x")

        self.sum_model = tk.StringVar(value="Model: -")
        self.sum_scen = tk.StringVar(value="Scenario: -")
        self.sum_ens = tk.StringVar(value="Ensembles: 0 selected")
        self.sum_params = tk.StringVar(value="Parameters: 0 selected")
        self.sum_years = tk.StringVar(value="Years: -")
        self.sum_out = tk.StringVar(value="Output: -")
        self.sum_vf = tk.StringVar(value="Version filter: -")

        grid = ttk.Frame(summ)
        grid.pack(fill="x")
        ttk.Label(grid, textvariable=self.sum_model).grid(row=0, column=0, sticky="w")
        ttk.Label(grid, textvariable=self.sum_scen).grid(row=0, column=1, sticky="w", padx=(18, 0))
        ttk.Label(grid, textvariable=self.sum_ens).grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Label(grid, textvariable=self.sum_params).grid(row=1, column=1, sticky="w", padx=(18, 0), pady=(4, 0))
        ttk.Label(grid, textvariable=self.sum_years).grid(row=2, column=0, sticky="w", pady=(4, 0))
        ttk.Label(grid, textvariable=self.sum_vf).grid(row=2, column=1, sticky="w", padx=(18, 0), pady=(4, 0))
        ttk.Label(grid, textvariable=self.sum_out).grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)

        # Download Queue
        qframe = ttk.LabelFrame(right, text="Download Queue", padding=10)
        qframe.pack(fill="both", expand=True, pady=(10, 0))

        header = ttk.Frame(qframe)
        header.pack(fill="x")

        self.btn_build_queue = ttk.Button(header, text="Build Queue", command=self.build_queue)
        self.btn_clear_queue = ttk.Button(header, text="Clear Queue", command=self.clear_queue)
        self.queue_info_var = tk.StringVar(value="Queue: 0 files")

        self.btn_build_queue.pack(side="left")
        self.btn_clear_queue.pack(side="left", padx=6)
        ttk.Label(header, textvariable=self.queue_info_var).pack(side="left", padx=(12, 0))

        self.lbl_bq_info = ttk.Label(header, text="ⓘ")
        self.lbl_bq_info.pack(side="left", padx=8)
        self.tt_bq = ToolTip(self.lbl_bq_info, "")

        columns = ("year", "filename", "out_dir")
        self.tree = ttk.Treeview(qframe, columns=columns, show="headings", height=12)
        self.tree.heading("year", text="Year(s)")
        self.tree.heading("filename", text="Filename")
        self.tree.heading("out_dir", text="Output folder")
        self.tree.column("year", width=90, anchor="w")
        self.tree.column("filename", width=380, anchor="w")
        self.tree.column("out_dir", width=520, anchor="w")
        self.tree.pack(fill="both", expand=True, pady=(8, 6))

        tsb = ttk.Scrollbar(qframe, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tsb.set)
        tsb.place(in_=self.tree, relx=1.0, rely=0, relheight=1.0, anchor="ne")

        # Download controls
        dctrl = ttk.Frame(qframe)
        dctrl.pack(fill="x", pady=(4, 0))

        self.btn_start = ttk.Button(dctrl, text="Start", command=self.start_download)
        self.btn_pause = ttk.Button(dctrl, text="Pause", command=self.pause_download, state="disabled")
        self.btn_resume = ttk.Button(dctrl, text="Resume", command=self.resume_download, state="disabled")
        self.btn_stop = ttk.Button(dctrl, text="Stop", command=self.stop_download, state="disabled")

        self.btn_start.pack(side="left")
        self.btn_pause.pack(side="left", padx=6)
        self.btn_resume.pack(side="left", padx=6)
        self.btn_stop.pack(side="left", padx=6)

        # Progress
        pframe = ttk.LabelFrame(right, text="Progress", padding=10)
        pframe.pack(fill="x", pady=(10, 0))

        self.cur_file_var = tk.StringVar(value="Current file: -")
        self.overall_var = tk.StringVar(value="Overall: 0/0")

        ttk.Label(pframe, textvariable=self.cur_file_var).pack(anchor="w")
        ttk.Label(pframe, textvariable=self.overall_var).pack(anchor="w", pady=(2, 0))

        self.pb_file = ttk.Progressbar(pframe, orient="horizontal", mode="determinate")
        self.pb_file.pack(fill="x", pady=(6, 4))
        self.pb_overall = ttk.Progressbar(pframe, orient="horizontal", mode="determinate")
        self.pb_overall.pack(fill="x")

        # Log
        lframe = ttk.LabelFrame(root, text="Log", padding=10)
        lframe.pack(fill="both", expand=False, pady=(10, 0))

        self.log_text = tk.Text(lframe, height=10, wrap="word")
        self.log_text.pack(side="left", fill="both", expand=True)

        lsb = ttk.Scrollbar(lframe, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=lsb.set)
        lsb.pack(side="right", fill="y")

        # Status bar (human)
        self.status_var = tk.StringVar(value="Ready.")
        status = ttk.Label(root, textvariable=self.status_var, relief="sunken", anchor="w", padding=(8, 4))
        status.pack(fill="x", pady=(8, 0))

    def _make_listbox(self, parent, title: str) -> tk.Listbox:
        frame = ttk.LabelFrame(parent, text=title, padding=6)
        frame.pack(fill="both", expand=True, pady=(0, 8))
        lb = tk.Listbox(frame, selectmode="extended", exportselection=False)
        lb.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(frame, orient="vertical", command=lb.yview)
        lb.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        return lb

    def _bind_events(self):
        # Double click shortcuts
        self.lb_models.bind("<Double-Button-1>", lambda _e: self.load_scenarios())
        self.lb_scenarios.bind("<Double-Button-1>", lambda _e: self.load_ensembles())
        self.lb_ensembles.bind("<Double-Button-1>", lambda _e: self.load_parameters())

        # Selection change updates summary and wizard readiness
        for lb in (self.lb_models, self.lb_scenarios, self.lb_ensembles, self.lb_params):
            lb.bind("<<ListboxSelect>>", lambda _e: self._on_selection_changed())

        # Year/filter/out changes update summary + readiness
        for var in (self.y0_var, self.y1_var, self.version_filter_var, self.out_dir_var):
            var.trace_add("write", lambda *_: self._on_settings_changed())

    # ---------------- Status / Log ----------------

    def _set_status(self, msg: str):
        self.status_var.set(msg)

    def _trim_log_if_needed(self):
        try:
            lines = int(self.log_text.index("end-1c").split(".")[0])
        except Exception:
            return
        if lines <= MAX_LOG_LINES:
            return
        delete_lines = max(1, lines - TRIM_LOG_TO_LINES)
        self.log_text.delete("1.0", f"{delete_lines + 1}.0")

    def _log(self, msg: str):
        self.log_text.insert("end", msg + "\n")
        self._trim_log_if_needed()
        self.log_text.see("end")

    def post_ui(self, event: dict):
        self.uiq.put(event)

    def post_log(self, msg: str):
        self.post_ui({"type": "log", "msg": msg})

    # ---------------- UI queue pump (adaptive) ----------------

    def _pump_ui_queue(self):
        try:
            while True:
                ev = self.uiq.get_nowait()
                et = ev.get("type")

                if et == "log":
                    self._log(ev["msg"])

                elif et == "status":
                    self._set_status(ev["msg"])

                elif et == "models_loaded":
                    self.models = ev["models"]
                    self._set_listbox(self.lb_models, [m["title"] for m in self.models])
                    self._set_status(f"Models loaded ({len(self.models)}).")
                    self._log(f"[INFO] Loaded models: {len(self.models)}")
                    if self.auto_advance_var.get() and self.models:
                        self.lb_models.selection_clear(0, "end")
                        self.lb_models.selection_set(0)
                        self.lb_models.see(0)
                        self._on_selection_changed()
                        self.load_scenarios()

                elif et == "scenarios_loaded":
                    self.scenarios = ev["scenarios"]
                    self._set_listbox(self.lb_scenarios, [s["title"] for s in self.scenarios])
                    self._set_status(f"Scenarios loaded ({len(self.scenarios)}).")
                    self._log(f"[INFO] Loaded scenarios: {len(self.scenarios)}")
                    if self.auto_advance_var.get() and self.scenarios:
                        self.lb_scenarios.selection_clear(0, "end")
                        self.lb_scenarios.selection_set(0)
                        self.lb_scenarios.see(0)
                        self._on_selection_changed()
                        self.load_ensembles()

                elif et == "ensembles_loaded":
                    self.ensembles = ev["ensembles"]
                    self._set_listbox(self.lb_ensembles, [e["title"] for e in self.ensembles])
                    self._set_status(f"Ensembles loaded ({len(self.ensembles)}).")
                    self._log(f"[INFO] Loaded ensembles: {len(self.ensembles)}")
                    if self.auto_advance_var.get() and self.ensembles:
                        self.lb_ensembles.selection_clear(0, "end")
                        self.lb_ensembles.selection_set(0)
                        self.lb_ensembles.see(0)
                        self._on_selection_changed()
                        self.load_parameters()

                elif et == "params_loaded":
                    self.params_by_ens = ev["params_by_ens"]
                    self.param_titles = ev["param_titles"]
                    self._set_listbox(self.lb_params, self.param_titles)
                    self._set_status(f"Parameters loaded ({len(self.param_titles)}).")
                    self._log(f"[INFO] Loaded parameters: {len(self.param_titles)}")
                    if self.auto_advance_var.get() and self.param_titles:
                        self.lb_params.selection_clear(0, "end")
                        self.lb_params.selection_set(0)
                        self.lb_params.see(0)
                        self._on_selection_changed()

                elif et == "queue_built":
                    self.download_items = ev["items"]
                    self._refresh_queue_tree()
                    self.queue_info_var.set(f"Queue: {len(self.download_items)} files")
                    self._set_status(f"Queue built ({len(self.download_items)} files).")
                    self._log(f"[INFO] Queue built: {len(self.download_items)} files")

                # download progress events
                elif et == "dl_state":
                    self._apply_dl_state(ev)

                elif et == "dl_progress_total":
                    total = ev["total"]
                    if total is None:
                        self.pb_file.configure(mode="indeterminate")
                        self.pb_file.start(10)
                    else:
                        self.pb_file.stop()
                        self.pb_file.configure(mode="determinate", maximum=total, value=0)

                elif et == "dl_progress_abs":
                    if self.pb_file["mode"] == "determinate":
                        self.pb_file["value"] = ev["value"]

                elif et == "dl_progress_add":
                    if self.pb_file["mode"] == "determinate":
                        self.pb_file["value"] = float(self.pb_file["value"]) + ev["delta"]

                elif et == "dl_file_start":
                    self.cur_file_var.set(f"Current file: {ev['filename']}")
                    self.overall_var.set(f"Overall: {ev['index']}/{ev['total_files']}")
                    self.pb_overall.configure(mode="determinate", maximum=ev["total_files"], value=ev["index"] - 1)
                    self._set_status(f"Downloading {ev['index']}/{ev['total_files']} …")

                elif et == "dl_file_done":
                    self.pb_overall["value"] = ev["index"]

                elif et == "dl_restart_progress":
                    self.pb_file.stop()
                    self.pb_file["value"] = 0

                elif et == "done":
                    self._log(ev["msg"])
                    self._set_status("Done.")
                    self._apply_dl_state({"running": False, "paused": False})

        except queue.Empty:
            pass

        interval = 50 if (self.is_downloading and not self.is_paused) else (90 if self.is_downloading else 140)
        self.after(interval, self._pump_ui_queue)

    def _set_listbox(self, lb: tk.Listbox, items: List[str]):
        lb.delete(0, "end")
        for it in items:
            lb.insert("end", it)

    # ---------------- Selection / Settings updates ----------------

    def _on_selection_changed(self):
        self._refresh_selection_summary()
        self._recompute_stage()
        self._refresh_wizard()
        self._refresh_build_queue_enabled()

    def _on_settings_changed(self, *_):
        self._refresh_selection_summary()
        self._refresh_wizard()
        self._refresh_build_queue_enabled()

    def _refresh_selection_summary(self):
        model = self._selected_one(self.lb_models)
        scen = self._selected_one(self.lb_scenarios)
        ens = self._selected_multi(self.lb_ensembles)
        params = self._selected_multi(self.lb_params)

        self.sum_model.set(f"Model: {model or '-'}")
        self.sum_scen.set(f"Scenario: {scen or '-'}")
        self.sum_ens.set(f"Ensembles: {len(ens)} selected")
        self.sum_params.set(f"Parameters: {len(params)} selected")

        ytxt = "-"
        try:
            y0 = int(self.y0_var.get().strip())
            y1 = int(self.y1_var.get().strip())
            ytxt = f"{y0}–{y1}" if y0 <= y1 else "Invalid"
        except Exception:
            ytxt = "Invalid"
        self.sum_years.set(f"Years: {ytxt}")

        vf = self.version_filter_var.get().strip()
        self.sum_vf.set(f"Version filter: {vf if vf else '(none)'}")

        outd = self.out_dir_var.get().strip()
        self.sum_out.set(f"Output: {outd if outd else '-'}")

    # ---------------- Wizard logic ----------------

    def _recompute_stage(self):
        """Compute current stage based on what is loaded/selected."""
        if not self.models:
            self.stage = "models"
            return

        model_sel = self._selected_one(self.lb_models)
        if not model_sel:
            self.stage = "models"
            return

        if not self.scenarios:
            self.stage = "scenarios"
            return

        scen_sel = self._selected_one(self.lb_scenarios)
        if not scen_sel:
            self.stage = "scenarios"
            return

        if not self.ensembles:
            self.stage = "ensembles"
            return

        ens_sel = self._selected_multi(self.lb_ensembles)
        if not ens_sel:
            self.stage = "ensembles"
            return

        if not self.param_titles:
            self.stage = "params"
            return

        params_sel = self._selected_multi(self.lb_params)
        if not params_sel:
            self.stage = "params"
            return

        self.stage = "queue"

    def _wizard_step_index(self) -> int:
        return self.STAGES.index(self.stage) + 1

    def _wizard_label(self) -> str:
        return {
            "models": "Load Models",
            "scenarios": "Load Scenarios",
            "ensembles": "Load Ensembles",
            "params": "Load Parameters",
            "queue": "Build Queue",
        }[self.stage]

    def _wizard_can_run(self) -> Tuple[bool, str]:
        """Whether wizard next action is currently valid."""
        if self._catalog_busy():
            return False, "Catalog operation is running…"
        if self.is_downloading:
            return False, "Downloading in progress…"

        if self.stage == "models":
            return True, "Fetch top-level models list."
        if self.stage == "scenarios":
            if not self._selected_one(self.lb_models):
                return False, "Select a model first."
            return True, "Load scenarios for selected model."
        if self.stage == "ensembles":
            if not self._selected_one(self.lb_scenarios):
                return False, "Select a scenario first."
            return True, "Load ensembles for selected scenario."
        if self.stage == "params":
            if len(self._selected_multi(self.lb_ensembles)) == 0:
                return False, "Select one or more ensembles first."
            return True, "Load parameters (intersection/union) for selected ensembles."
        if self.stage == "queue":
            ok, why = self._can_build_queue()
            return ok, why
        return False, "Not ready."

    def _refresh_wizard(self):
        idx = self._wizard_step_index()
        self.wiz_step_var.set(f"Step {idx}/5")
        label = self._wizard_label()
        self.wiz_label_var.set(label)
        self.btn_wizard.configure(text=label)

        ok, hint = self._wizard_can_run()
        self.btn_wizard.configure(state="normal" if ok else "disabled")
        self.tt_wizard.set_text(hint)

    def on_wizard_next(self):
        # This button is disabled when not ok; still guard:
        ok, hint = self._wizard_can_run()
        if not ok:
            self._set_status(hint)
            return

        if self.stage == "models":
            self.load_models()
        elif self.stage == "scenarios":
            self.load_scenarios()
        elif self.stage == "ensembles":
            self.load_ensembles()
        elif self.stage == "params":
            self.load_parameters()
        elif self.stage == "queue":
            self.build_queue()

    # ---------------- Build Queue readiness ----------------

    def _can_build_queue(self) -> Tuple[bool, str]:
        if self._catalog_busy():
            return False, "Busy: catalog is working."
        if self.is_downloading:
            return False, "Busy: downloading."

        out_dir = self.out_dir_var.get().strip()
        if not out_dir:
            return False, "Set Output directory."
        try:
            y0 = int(self.y0_var.get().strip())
            y1 = int(self.y1_var.get().strip())
            if y0 > y1:
                return False, "Invalid years range."
        except Exception:
            return False, "Invalid years input."

        if not self._selected_one(self.lb_models):
            return False, "Select Model."
        if not self._selected_one(self.lb_scenarios):
            return False, "Select Scenario."
        if len(self._selected_multi(self.lb_ensembles)) == 0:
            return False, "Select one or more Ensembles."
        if len(self._selected_multi(self.lb_params)) == 0:
            return False, "Select one or more Parameters."

        return True, "Build download queue from current selections."

    def _refresh_build_queue_enabled(self):
        ok, why = self._can_build_queue()
        self.btn_build_queue.configure(state="normal" if ok else "disabled")
        # tooltip via info icon (works even when button disabled)
        if ok:
            self.tt_bq.set_text("Ready: click Build Queue.")
        else:
            self.tt_bq.set_text(f"Not ready: {why}")
        # status hint (human)
        if not ok and self.stage == "queue":
            self._set_status(f"Build Queue: {why}")

    # ---------------- Convenience selection helpers ----------------

    def _selected_one(self, lb: tk.Listbox) -> Optional[str]:
        sel = lb.curselection()
        if not sel:
            return None
        return lb.get(sel[0])

    def _selected_multi(self, lb: tk.Listbox) -> List[str]:
        return [lb.get(i) for i in lb.curselection()]

    def _find_entry_by_title(self, entries: List[Dict[str, Optional[str]]], title: str) -> Optional[Dict[str, Optional[str]]]:
        for e in entries:
            if e.get("title") == title:
                return e
        return None

    def _catalog_busy(self) -> bool:
        return bool(self.catalog_thread and self.catalog_thread.is_alive())

    # ---------------- Settings actions ----------------

    def browse_dir(self):
        d = filedialog.askdirectory(title="Select output directory")
        if d:
            self.out_dir_var.set(d)

    # ---------------- Catalog actions (background) ----------------

    def load_models(self):
        if self._catalog_busy():
            messagebox.showinfo("Busy", "Catalog operation already running.")
            return

        def worker():
            stop_event.clear()
            self.post_ui({"type": "status", "msg": "Loading models…"})
            self.post_log("[INFO] Loading models…")
            models = scan_models(self.post_log)
            self.post_ui({"type": "models_loaded", "models": models})

        self.catalog_thread = threading.Thread(target=worker, daemon=True)
        self.catalog_thread.start()

    def load_scenarios(self):
        if not self.models:
            messagebox.showwarning("No models", "Load models first.")
            return
        model_title = self._selected_one(self.lb_models)
        if not model_title:
            messagebox.showwarning("Select model", "Select a model first.")
            return
        model = self._find_entry_by_title(self.models, model_title)
        if not model:
            return
        if self._catalog_busy():
            messagebox.showinfo("Busy", "Catalog operation already running.")
            return

        def worker():
            stop_event.clear()
            self.post_ui({"type": "status", "msg": f"Loading scenarios for {model_title}…"})
            self.post_log(f"[INFO] Loading scenarios for model: {model_title}")
            scens = list_scenarios_for_model(model, self.post_log)
            self.post_ui({"type": "scenarios_loaded", "scenarios": scens})

        self.catalog_thread = threading.Thread(target=worker, daemon=True)
        self.catalog_thread.start()

    def load_ensembles(self):
        if not self.scenarios:
            messagebox.showwarning("No scenarios", "Load scenarios first.")
            return
        scen_title = self._selected_one(self.lb_scenarios)
        if not scen_title:
            messagebox.showwarning("Select scenario", "Select a scenario first.")
            return
        scen = self._find_entry_by_title(self.scenarios, scen_title)
        if not scen:
            return
        if self._catalog_busy():
            messagebox.showinfo("Busy", "Catalog operation already running.")
            return

        def worker():
            stop_event.clear()
            self.post_ui({"type": "status", "msg": f"Loading ensembles for {scen_title}…"})
            self.post_log(f"[INFO] Loading ensembles for scenario: {scen_title}")
            ens = list_ensembles_for_scenario(scen, self.post_log)
            self.post_ui({"type": "ensembles_loaded", "ensembles": ens})

        self.catalog_thread = threading.Thread(target=worker, daemon=True)
        self.catalog_thread.start()

    def load_parameters(self):
        if not self.ensembles:
            messagebox.showwarning("No ensembles", "Load ensembles first.")
            return
        ens_titles = self._selected_multi(self.lb_ensembles)
        if not ens_titles:
            messagebox.showwarning("Select ensembles", "Select one or more ensembles.")
            return

        selected_ens_entries = []
        for t in ens_titles:
            e = self._find_entry_by_title(self.ensembles, t)
            if e:
                selected_ens_entries.append(e)

        if not selected_ens_entries:
            return

        if self._catalog_busy():
            messagebox.showinfo("Busy", "Catalog operation already running.")
            return

        common_only = self.common_params_only_var.get()

        def worker():
            stop_event.clear()
            self.post_ui({"type": "status", "msg": "Loading parameters…"})
            self.post_log("[INFO] Loading parameters (per-ensemble)…")
            params_by_ens: Dict[str, Dict[str, Dict[str, Optional[str]]]] = {}

            for e in selected_ens_entries:
                ename = e.get("title") or "UNKNOWN_ENS"
                plist = list_parameters_for_ensemble(e, self.post_log)
                per: Dict[str, Dict[str, Optional[str]]] = {}
                for p in plist:
                    pt = p.get("title")
                    if pt:
                        per[pt] = p
                params_by_ens[ename] = per

            sets = [set(d.keys()) for d in params_by_ens.values() if d]
            if not sets:
                param_titles = []
            else:
                param_titles = sorted(set.intersection(*sets)) if common_only else sorted(set().union(*sets))
                if common_only and not param_titles:
                    param_titles = sorted(set().union(*sets))
                    self.post_log("[WARN] No common parameters across selected ensembles; showing union.")

            self.post_ui({"type": "params_loaded", "params_by_ens": params_by_ens, "param_titles": param_titles})

        self.catalog_thread = threading.Thread(target=worker, daemon=True)
        self.catalog_thread.start()

    # ---------------- Queue actions ----------------

    def clear_queue(self):
        if self.download_thread and self.download_thread.is_alive():
            messagebox.showwarning("Busy", "Stop download before clearing queue.")
            return
        self.download_items = []
        for i in self.tree.get_children():
            self.tree.delete(i)
        self.queue_info_var.set("Queue: 0 files")
        self._log("[INFO] Queue cleared.")
        self._set_status("Queue cleared.")

    def build_queue(self):
        ok, why = self._can_build_queue()
        if not ok:
            self._set_status(f"Build Queue: {why}")
            messagebox.showwarning("Not ready", why)
            return

        out_dir = self.out_dir_var.get().strip()
        model_name = self._selected_one(self.lb_models) or ""
        scen_name = self._selected_one(self.lb_scenarios) or ""
        ens_titles = self._selected_multi(self.lb_ensembles)
        param_titles = self._selected_multi(self.lb_params)

        y0 = int(self.y0_var.get().strip())
        y1 = int(self.y1_var.get().strip())
        version_filter = self.version_filter_var.get().strip()

        if self._catalog_busy():
            messagebox.showinfo("Busy", "Catalog operation already running.")
            return

        def worker():
            stop_event.clear()
            self.post_ui({"type": "status", "msg": "Building queue…"})
            self.post_log("[INFO] Building queue (this may take a while)…")
            items: List[Dict] = []

            for ens_name in ens_titles:
                per_params = self.params_by_ens.get(ens_name, {})
                for p_name in param_titles:
                    p_entry = per_params.get(p_name)
                    if not p_entry:
                        continue

                    urlpaths = list_urlpaths_for_parameter(p_entry, self.post_log)
                    for up in urlpaths:
                        if not up.lower().endswith(".nc"):
                            continue
                        if version_filter and version_filter not in up:
                            continue

                        fname = os.path.basename(up)
                        fy0, fy1 = file_years_from_filename(fname)
                        if not years_overlap(fy0, fy1, y0, y1):
                            continue

                        fs_url = build_fileserver_url(up)
                        out_folder = os.path.join(
                            out_dir,
                            sanitize_folder_name(model_name),
                            sanitize_folder_name(scen_name),
                            sanitize_folder_name(ens_name),
                            sanitize_folder_name(p_name),
                        )

                        sort_year = fy0 if fy0 is not None else (fy1 if fy1 is not None else 0)
                        yr_text = f"{fy0}" if fy0 == fy1 else f"{fy0}-{fy1}"

                        items.append({
                            "url": fs_url,
                            "out_dir": out_folder,
                            "filename": fname,
                            "year_sort": sort_year,
                            "year_text": yr_text,
                        })

            items.sort(key=lambda x: (x["year_sort"], x["filename"]))
            self.post_ui({"type": "queue_built", "items": items})

        self.catalog_thread = threading.Thread(target=worker, daemon=True)
        self.catalog_thread.start()

    def _refresh_queue_tree(self):
        for i in self.tree.get_children():
            self.tree.delete(i)

        show = self.download_items[:QUEUE_TABLE_SHOW_LIMIT]
        for it in show:
            self.tree.insert("", "end", values=(it["year_text"], it["filename"], it["out_dir"]))

        if len(self.download_items) > QUEUE_TABLE_SHOW_LIMIT:
            self._log(f"[INFO] Queue is large; showing first {QUEUE_TABLE_SHOW_LIMIT} rows in table (still downloads all).")

    # ---------------- Download controls ----------------

    def _apply_dl_state(self, st: dict):
        running = bool(st.get("running", False))
        paused = bool(st.get("paused", False))
        self.is_downloading = running
        self.is_paused = paused

        self.btn_start.configure(state="disabled" if running else "normal")
        self.btn_stop.configure(state="normal" if running else "disabled")
        if running and not paused:
            self.btn_pause.configure(state="normal")
            self.btn_resume.configure(state="disabled")
        elif running and paused:
            self.btn_pause.configure(state="disabled")
            self.btn_resume.configure(state="normal")
        else:
            self.btn_pause.configure(state="disabled")
            self.btn_resume.configure(state="disabled")

        # disable catalog actions while downloading
        cat_state = "disabled" if running else "normal"
        for w in (self.btn_load_models, self.btn_load_scen, self.btn_load_ens, self.btn_load_params, self.btn_wizard):
            w.configure(state=cat_state)

        # Build Queue depends on readiness + not downloading
        self._refresh_build_queue_enabled()
        self._refresh_wizard()

    def start_download(self):
        if not self.download_items:
            messagebox.showwarning("Queue empty", "Build a queue first.")
            return
        if self.download_thread and self.download_thread.is_alive():
            messagebox.showinfo("Busy", "Download already running.")
            return

        if len(self.download_items) >= QUEUE_CONFIRM_THRESHOLD:
            if not messagebox.askyesno("Confirm", f"Queue has {len(self.download_items)} files.\nStart download?"):
                return

        out_dir = self.out_dir_var.get().strip()
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception as e:
            messagebox.showerror("Output dir", f"Cannot create output directory:\n{e}")
            return

        workers = max(1, int(self.workers_var.get()))
        segments = max(1, int(self.segments_var.get()))

        stop_event.clear()
        resume_event.set()

        self.post_ui({"type": "dl_state", "running": True, "paused": False})
        self.pb_overall.configure(mode="determinate", maximum=len(self.download_items), value=0)

        def progress_set_total(total: Optional[int]):
            self.post_ui({"type": "dl_progress_total", "total": total})

        def progress_set_abs(value: int):
            self.post_ui({"type": "dl_progress_abs", "value": value})

        def progress_add(delta: int):
            self.post_ui({"type": "dl_progress_add", "delta": delta})

        def set_restart_cb(_total_known: bool):
            self.post_ui({"type": "dl_restart_progress"})

        def worker_thread():
            ok_count = 0
            err_count = 0
            total_files = len(self.download_items)

            for idx, it in enumerate(self.download_items, 1):
                if stop_event.is_set():
                    self.post_log("[INFO] Stop requested. Ending downloads.")
                    break

                self.post_ui({"type": "dl_file_start", "filename": it["filename"], "index": idx, "total_files": total_files})
                progress_set_total(None)
                progress_set_abs(0)

                ok, msg = download_file_robust(
                    it["url"], it["out_dir"],
                    segments=segments, workers=workers,
                    max_retries=MAX_RETRIES_PER_FILE,
                    progress_set_total_cb=progress_set_total,
                    progress_add_cb=progress_add,
                    progress_set_abs_cb=progress_set_abs,
                    post_log=self.post_log,
                    set_restart_cb=set_restart_cb,
                    cleanup_on_stop=True
                )

                if ok:
                    ok_count += 1
                    self.post_log(f"[OK] {msg}")
                else:
                    err_count += 1
                    self.post_log(f"[ERR] {it['filename']}: {msg}")

                self.post_ui({"type": "dl_file_done", "index": idx})

            self.post_ui({"type": "done", "msg": f"[INFO] Done. ok={ok_count}, err={err_count}"})

        self.download_thread = threading.Thread(target=worker_thread, daemon=True)
        self.download_thread.start()

    def pause_download(self):
        if self.download_thread and self.download_thread.is_alive():
            resume_event.clear()
            self.post_ui({"type": "dl_state", "running": True, "paused": True})
            self._set_status("Paused.")
            self._log("[INFO] Paused.")

    def resume_download(self):
        if self.download_thread and self.download_thread.is_alive():
            resume_event.set()
            self.post_ui({"type": "dl_state", "running": True, "paused": False})
            self._set_status("Resumed.")
            self._log("[INFO] Resumed.")

    def stop_download(self):
        stop_event.set()
        resume_event.set()
        self._set_status("Stop requested…")
        self._log("[INFO] Stop requested…")

    # ---------------- Close ----------------

    def on_close(self):
        if self.closing:
            return
        self.closing = True

        # disable controls to avoid re-entrancy
        try:
            for w in (
                self.btn_start, self.btn_pause, self.btn_resume, self.btn_stop,
                self.btn_load_models, self.btn_load_scen, self.btn_load_ens, self.btn_load_params,
                self.btn_build_queue, self.btn_clear_queue, self.btn_wizard
            ):
                w.configure(state="disabled")
        except Exception:
            pass

        stop_event.set()
        resume_event.set()
        self._set_status("Closing…")
        self._log("[INFO] Closing…")

        def join_timeout(t: Optional[threading.Thread], timeout: float):
            if t and t.is_alive():
                try:
                    t.join(timeout=timeout)
                except Exception:
                    pass

        join_timeout(self.catalog_thread, 1.0)
        join_timeout(self.download_thread, 2.0)

        try:
            close_all_sessions()
        finally:
            self.destroy()


# ============================================================
# ENTRY POINT
# ============================================================

def main():
    try:
        app = App()
        app.mainloop()
    except KeyboardInterrupt:
        stop_event.set()
        resume_event.set()
    finally:
        close_all_sessions()


if __name__ == "__main__":
    main()
