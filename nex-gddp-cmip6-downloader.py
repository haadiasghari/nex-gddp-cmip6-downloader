#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
NEX-GDDP-CMIP6 Downloader - GUI Edition (Version 1.0)

Final Version with:
- Enhanced UX with tabbed interface
- Fixed toolbar with quick actions
- Visual progress indicator
- Search/filter for long lists
- Grouped checkboxes for parameters
- Improved download speed and ETA calculation
- About section with author info

Author: Hadi Asghari
Email: hadi.asghari@outlook.com
Version: 1.0
Description: A professional GUI tool for downloading NEX-GDDP-CMIP6 climate data from NASA servers
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
from typing import Optional, List, Dict, Tuple, Set, Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

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
# UI HELPERS: Tooltip + ScrollableFrame + SearchableListbox
# ============================================================

class ToolTip:
    def __init__(self, widget: tk.Widget, text: str = "", delay_ms: int = 400):
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
    """Canvas + inner frame: scrolls entire content."""
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


class SearchableListbox(ttk.Frame):
    """Listbox with search functionality."""
    def __init__(self, parent, title: str, multi_select: bool = True, *args, **kwargs):
        super().__init__(parent, *args, **kwargs)
        
        self.title = title
        self.multi_select = multi_select
        self.all_items = []
        self.filtered_items = []
        
        # Create frame
        frame = ttk.LabelFrame(self, text=title, padding=6)
        frame.pack(fill="both", expand=True)
        
        # Search frame
        search_frame = ttk.Frame(frame)
        search_frame.pack(fill="x", pady=(0, 5))
        
        ttk.Label(search_frame, text="Search:").pack(side="left", padx=(0, 5))
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(search_frame, textvariable=self.search_var)
        self.search_entry.pack(side="left", fill="x", expand=True, padx=(0, 5))
        
        self.clear_search_btn = ttk.Button(search_frame, text="Clear", width=8, command=self.clear_search)
        self.clear_search_btn.pack(side="right")
        
        # Listbox frame
        listbox_frame = ttk.Frame(frame)
        listbox_frame.pack(fill="both", expand=True)
        
        # Listbox
        self.lb = tk.Listbox(listbox_frame, selectmode="extended" if multi_select else "single", 
                            exportselection=False)
        self.lb.pack(side="left", fill="both", expand=True)
        
        # Scrollbar
        sb = ttk.Scrollbar(listbox_frame, orient="vertical", command=self.lb.yview)
        self.lb.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        
        # Bind events
        self.search_var.trace_add("write", self._on_search_changed)
        
        # Selection info
        self.selection_info = tk.StringVar(value="0 items selected")
        ttk.Label(frame, textvariable=self.selection_info).pack(anchor="w", pady=(5, 0))
        
    def set_items(self, items: List[str]):
        self.all_items = sorted(items)
        self.filtered_items = self.all_items.copy()
        self._refresh_listbox()
        
    def _refresh_listbox(self):
        self.lb.delete(0, "end")
        for item in self.filtered_items:
            self.lb.insert("end", item)
        self._update_selection_info()
        
    def _on_search_changed(self, *args):
        search_text = self.search_var.get().lower()
        if not search_text:
            self.filtered_items = self.all_items.copy()
        else:
            self.filtered_items = [item for item in self.all_items if search_text in item.lower()]
        self._refresh_listbox()
        
    def clear_search(self):
        self.search_var.set("")
        
    def get_selected(self) -> List[str]:
        selected_indices = self.lb.curselection()
        return [self.filtered_items[i] for i in selected_indices]
    
    def bind_selection_change(self, callback):
        self.lb.bind("<<ListboxSelect>>", lambda e: self._on_selection_change(callback))
        
    def _on_selection_change(self, callback):
        self._update_selection_info()
        if callback:
            callback()
            
    def _update_selection_info(self):
        selected = len(self.lb.curselection())
        total = len(self.filtered_items)
        self.selection_info.set(f"{selected} of {total} items selected")
        
    def select_all(self):
        self.lb.selection_set(0, "end")
        self._update_selection_info()
        
    def clear_selection(self):
        self.lb.selection_clear(0, "end")
        self._update_selection_info()


class CheckboxGroup(ttk.Frame):
    """Group of checkboxes with select all/none buttons."""
    def __init__(self, parent, title: str, items: List[str], on_change_callback: Optional[Callable] = None, *args, **kwargs):
        super().__init__(parent, *args, **kwargs)
        
        self.title = title
        self.items = items
        self.vars: Dict[str, tk.BooleanVar] = {}
        self.on_change_callback = on_change_callback
        
        # Create frame
        frame = ttk.LabelFrame(self, text=title, padding=10)
        frame.pack(fill="both", expand=True)
        
        # Control buttons
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill="x", pady=(0, 10))
        
        ttk.Button(btn_frame, text="Select All", command=self.select_all).pack(side="left", padx=(0, 5))
        ttk.Button(btn_frame, text="Select None", command=self.select_none).pack(side="left", padx=(0, 5))
        
        # Search
        search_frame = ttk.Frame(frame)
        search_frame.pack(fill="x", pady=(0, 10))
        
        ttk.Label(search_frame, text="Search:").pack(side="left", padx=(0, 5))
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(search_frame, textvariable=self.search_var)
        self.search_entry.pack(side="left", fill="x", expand=True, padx=(0, 5))
        
        ttk.Button(search_frame, text="Clear", command=self.clear_search).pack(side="right")
        
        # Checkboxes frame with scrollbar
        checkboxes_frame = ttk.Frame(frame)
        checkboxes_frame.pack(fill="both", expand=True)
        
        # Canvas for scrolling
        self.canvas = tk.Canvas(checkboxes_frame, highlightthickness=0)
        vsb = ttk.Scrollbar(checkboxes_frame, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        
        vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        
        self.inner_frame = ttk.Frame(self.canvas)
        self.canvas.create_window((0, 0), window=self.inner_frame, anchor="nw")
        
        # Create checkboxes
        self.checkbox_frames = []
        self._create_checkboxes()
        
        # Configure canvas scrolling
        self.inner_frame.bind("<Configure>", self._on_frame_configure)
        
        # Bind search
        self.search_var.trace_add("write", self._on_search_changed)
        
    def _create_checkboxes(self):
        # Clear existing checkboxes
        for widget in self.inner_frame.winfo_children():
            widget.destroy()
        self.checkbox_frames = []
        self.vars = {}
        
        # Create new checkboxes
        for i, item in enumerate(self.items):
            var = tk.BooleanVar(value=False)
            self.vars[item] = var
            
            # FIX: Add trace to detect changes
            var.trace_add("write", self._on_var_changed)
            
            cb_frame = ttk.Frame(self.inner_frame)
            cb_frame.pack(fill="x", pady=2)
            self.checkbox_frames.append((item, cb_frame))
            
            # FIX: Changed ttt.Checkbutton to ttk.Checkbutton
            cb = ttk.Checkbutton(cb_frame, text=item, variable=var)
            cb.pack(side="left", anchor="w")
    
    def _on_var_changed(self, *args):
        """Called when any checkbox variable changes."""
        if self.on_change_callback:
            self.on_change_callback()
            
    def _on_frame_configure(self, event):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        
    def _on_search_changed(self, *args):
        search_text = self.search_var.get().lower()
        
        for item, frame in self.checkbox_frames:
            if not search_text or search_text in item.lower():
                frame.pack(fill="x", pady=2)
            else:
                frame.pack_forget()
                
    def clear_search(self):
        self.search_var.set("")
        
    def select_all(self):
        for var in self.vars.values():
            var.set(True)
        # FIX: Trigger callback after selecting all
        if self.on_change_callback:
            self.on_change_callback()
            
    def select_none(self):
        for var in self.vars.values():
            var.set(False)
        # FIX: Trigger callback after selecting none
        if self.on_change_callback:
            self.on_change_callback()
            
    def get_selected(self) -> List[str]:
        return [item for item, var in self.vars.items() if var.get()]
    
    def set_items(self, items: List[str]):
        self.items = sorted(items)
        self._create_checkboxes()


# ============================================================
# ABOUT DIALOG (WITH SCROLLBAR ONLY FOR DESCRIPTION)
# ============================================================

class AboutDialog(tk.Toplevel):
    """About dialog with author information and scrollable description."""
    def __init__(self, parent):
        super().__init__(parent)
        self.title("About NEX-GDDP-CMIP6 Downloader")
        self.geometry("550x650")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        
        # Center window
        self.update_idletasks()
        width = 550
        height = 650
        x = (self.winfo_screenwidth() // 2) - (width // 2)
        y = (self.winfo_screenheight() // 2) - (height // 2)
        self.geometry(f'{width}x{height}+{x}+{y}')
        
        self._create_widgets()
        
    def _create_widgets(self):
        # Main container
        main_frame = ttk.Frame(self, padding=20)
        main_frame.pack(fill="both", expand=True)
        
        # Title
        title_label = ttk.Label(main_frame, 
                               text="NEX-GDDP-CMIP6 Downloader", 
                               font=("Arial", 16, "bold"))
        title_label.pack(pady=(0, 10))
        
        # Version
        version_label = ttk.Label(main_frame, 
                                 text="Version 1.0", 
                                 font=("Arial", 12))
        version_label.pack(pady=(0, 20))
        
        # Author Information Frame
        info_frame = ttk.LabelFrame(main_frame, text="Author Information", padding=15)
        info_frame.pack(fill="x", pady=(0, 15))
        
        # Create grid for author info
        info_grid = ttk.Frame(info_frame)
        info_grid.pack(fill="x", expand=True)
        
        # Row 0: Author
        ttk.Label(info_grid, text="Author:", 
                 font=("Arial", 10, "bold"), 
                 width=12, anchor="w").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Label(info_grid, text="Hadi Asghari", 
                 font=("Arial", 10)).grid(row=0, column=1, sticky="w", pady=5)
        
        # Row 1: Email
        ttk.Label(info_grid, text="Email:", 
                 font=("Arial", 10, "bold"), 
                 width=12, anchor="w").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Label(info_grid, text="hadi.asghari@outlook.com", 
                 font=("Arial", 10)).grid(row=1, column=1, sticky="w", pady=5)
        
        # Row 2: Version
        ttk.Label(info_grid, text="Version:", 
                 font=("Arial", 10, "bold"), 
                 width=12, anchor="w").grid(row=2, column=0, sticky="w", pady=5)
        ttk.Label(info_grid, text="1.0", 
                 font=("Arial", 10)).grid(row=2, column=1, sticky="w", pady=5)
        
        # Row 3: Organization
        ttk.Label(info_grid, text="Github:", 
                 font=("Arial", 10, "bold"), 
                 width=12, anchor="w").grid(row=3, column=0, sticky="w", pady=5)
        ttk.Label(info_grid, text="github.com/haadiasghari", 
                 font=("Arial", 10)).grid(row=3, column=1, sticky="w", pady=5)
        
        # Description Frame
        desc_frame = ttk.LabelFrame(main_frame, text="Description", padding=10)
        desc_frame.pack(fill="both", expand=True, pady=(0, 15))
        
        # Create text widget with scrollbar for description
        text_container = ttk.Frame(desc_frame)
        text_container.pack(fill="both", expand=True)
        
        # Text widget for description
        desc_text = """NEX-GDDP-CMIP6 Downloader is a professional graphical user interface application 
designed specifically for downloading climate projection data from NASA's NEX-GDDP-CMIP6 dataset.

This tool simplifies the complex process of accessing and downloading climate data by providing:
• A 5-step guided workflow for data selection
• Multi-threaded downloads with pause/resume functionality
• Real-time speed monitoring and progress tracking
• Advanced queue management for batch downloads
• Support for segmented downloads of large files
• Automatic retry on connection failures
• Comprehensive filtering and search capabilities

DATASET INFORMATION:
The NEX-GDDP-CMIP6 dataset contains downscaled climate projections derived from the CMIP6 archive. 
It provides high-resolution (0.25 degree) daily data for multiple climate variables, 
covering historical periods and future projections under various emission scenarios.

APPLICATION FEATURES:
1. MODEL SELECTION - Browse and select from available climate models
2. SCENARIO SELECTION - Choose emission scenarios (SSP1-2.6, SSP2-4.5, SSP3-7.0, SSP5-8.5)
3. ENSEMBLE SELECTION - Select model realizations (r1i1p1f1, etc.)
4. PARAMETER SELECTION - Choose climate variables (tasmax, tasmin, pr, etc.)
5. CONFIGURATION - Set download parameters and start downloading

TECHNICAL DETAILS:
• Data Source: NASA NEX-GDDP-CMIP6 THREDDS Catalog
• Supported Formats: NetCDF (.nc)
• Download Protocol: HTTP/HTTPS with range requests
• Multi-threading: Configurable worker threads
• Resume Capability: Partial download recovery
• Progress Tracking: Real-time speed and ETA calculation

SYSTEM REQUIREMENTS:
• Operating System: Windows, macOS, Linux
• Python Version: 3.7 or higher
• Disk Space: Varies based on selected data
• Internet Connection: Required for data download

FOR MORE INFORMATION:
Visit the official NASA NEX website: https://www.nasa.gov/nex
Climate Data Tools: https://github.com/climatedatatools

LICENSE AND USAGE:
This tool is provided for research and educational purposes. Users should comply with NASA's 
data usage policies and cite the appropriate data sources in their publications.

SUPPORT AND FEEDBACK:
For technical support or feature requests, please contact the author via email.

Version History:
• v1.0 - Initial release with basic functionality"""

        # FIX: keep a reference on self so the mouse-wheel handlers can
        # scroll this exact widget instead of relying on focus_get().
        self.desc_text_widget = tk.Text(text_container, wrap="word",
                                  font=("Arial", 9), 
                                  bg="#f9f9f9",
                                  relief="sunken",
                                  height=15,
                                  width=60)
        self.desc_text_widget.insert("1.0", desc_text)
        self.desc_text_widget.configure(state="disabled")
        
        # Add vertical scrollbar for text - اینجا اسکرول‌بار فقط برای متن توضیحات
        text_scrollbar = ttk.Scrollbar(text_container, orient="vertical", command=self.desc_text_widget.yview)
        self.desc_text_widget.configure(yscrollcommand=text_scrollbar.set)
        
        # Pack text and scrollbar
        self.desc_text_widget.pack(side="left", fill="both", expand=True)
        text_scrollbar.pack(side="right", fill="y")
        
        # Close button frame
        button_frame = ttk.Frame(main_frame)
        button_frame.pack(fill="x", pady=(10, 0))
        
        # Close button
        close_btn = ttk.Button(button_frame, text="Close", command=self.destroy, width=20)
        close_btn.pack()
        
        # Bind keyboard shortcuts
        self.bind('<Escape>', lambda e: self.destroy())
        self.bind('<Return>', lambda e: self.destroy())
        
        # Enable mouse wheel scrolling for the text widget
        self.desc_text_widget.bind("<MouseWheel>", self._on_mousewheel)
        self.desc_text_widget.bind("<Button-4>", self._on_mousewheel_linux)
        self.desc_text_widget.bind("<Button-5>", self._on_mousewheel_linux)
        
    def _on_mousewheel(self, event):
        """Handle mouse wheel scrolling for Windows/Mac."""
        # FIX: scroll the description Text widget directly instead of
        # self.focus_get(), which can return the Toplevel itself (no
        # yview_scroll method) and caused the AttributeError.
        self.desc_text_widget.yview_scroll(int(-1 * (event.delta / 120)), "units")
        
    def _on_mousewheel_linux(self, event):
        """Handle mouse wheel scrolling for Linux."""
        # FIX: same as above - scroll the Text widget directly.
        if event.num == 4:
            self.desc_text_widget.yview_scroll(-1, "units")
        elif event.num == 5:
            self.desc_text_widget.yview_scroll(1, "units")

# ============================================================
# ADVANCED SPEED CALCULATOR (IMPROVED ETA)
# ============================================================

class SpeedCalculator:
    """Calculates download speed and ETA with advanced algorithms."""
    def __init__(self):
        self.reset()
        
    def reset(self):
        self.start_time = None
        self.total_bytes = 0
        self.file_bytes = 0
        self.file_start_time = None
        self.file_total_bytes = None  # NEW: Store total file size for current file
        
        # Speed calculation buffers
        self.speed_samples = []
        self.max_samples = 15
        
        # File history for ETA calculation
        self.file_times = []  # List of download times for completed files
        self.file_sizes = []  # List of sizes for completed files
        
        # ETA calculation
        self.last_update_time = None
        self.bytes_since_last_update = 0
        
    def start_download(self):
        self.reset()
        self.start_time = time.time()
        self.last_update_time = time.time()
        
    def start_file(self, file_total_bytes: Optional[int] = None):
        self.file_start_time = time.time()
        self.file_bytes = 0
        self.file_total_bytes = file_total_bytes  # Store file size for ETA
        self.bytes_since_last_update = 0
        
    def set_file_total(self, total_bytes: Optional[int]):
        """Set total size of current file for accurate ETA calculation."""
        self.file_total_bytes = total_bytes
        
    def add_bytes(self, bytes_count: int):
        self.total_bytes += bytes_count
        self.file_bytes += bytes_count
        self.bytes_since_last_update += bytes_count
        
    def get_current_speed(self) -> float:
        """Get current speed in bytes per second using weighted average."""
        current_time = time.time()
        if not self.file_start_time or current_time - self.file_start_time < 0.1:
            return 0.0
            
        # Calculate instantaneous speed based on recent bytes
        elapsed_since_update = current_time - (self.last_update_time or current_time)
        if elapsed_since_update > 0.1:  # Only update if significant time has passed
            speed = self.bytes_since_last_update / elapsed_since_update
            self.bytes_since_last_update = 0
            self.last_update_time = current_time
            
            # Add to samples for smoothing
            self.speed_samples.append(speed)
            if len(self.speed_samples) > self.max_samples:
                self.speed_samples.pop(0)
            
        # Calculate weighted average (recent samples have more weight)
        if not self.speed_samples:
            return 0.0
            
        weighted_sum = 0
        total_weight = 0
        for i, sample in enumerate(self.speed_samples):
            weight = i + 1  # Linear weighting
            weighted_sum += sample * weight
            total_weight += weight
            
        return weighted_sum / total_weight if total_weight > 0 else 0.0
    
    def get_average_speed(self) -> float:
        """Get average speed for entire download."""
        if not self.start_time or time.time() - self.start_time < 0.1:
            return 0.0
            
        elapsed = time.time() - self.start_time
        if elapsed <= 0:
            return 0.0
            
        return self.total_bytes / elapsed
    
    def record_file_completion(self, file_size: Optional[int] = None):
        """Record completion time of a file for better ETA prediction."""
        if self.file_start_time:
            file_time = time.time() - self.file_start_time
            self.file_times.append(file_time)
            if file_size:
                self.file_sizes.append(file_size)
            
            # Keep only last 10 files for prediction
            if len(self.file_times) > 10:
                self.file_times.pop(0)
            if len(self.file_sizes) > 10:
                self.file_sizes.pop(0)
    
    def get_eta(self, files_remaining: int, files_done: int) -> str:
        """Calculate ETA using multiple methods for better accuracy."""
        if not self.start_time or files_done == 0:
            return "Calculating..."
            
        elapsed = time.time() - self.start_time
        if elapsed <= 0:
            return "Calculating..."
        
        # Method 1: Based on current file progress (most accurate for current file)
        current_file_eta = self._get_current_file_eta()
        
        # Method 2: Based on average time per file
        avg_time_eta = self._get_avg_time_eta(files_remaining, files_done)
        
        # Method 3: Based on speed and remaining bytes (if we know sizes)
        speed_based_eta = self._get_speed_based_eta(files_remaining)
        
        # Combine methods: Prefer current file ETA if available, otherwise use average
        if current_file_eta is not None and current_file_eta > 0:
            # Add average time for remaining files
            if files_remaining > 1 and avg_time_eta is not None:
                total_eta = current_file_eta + (avg_time_eta * (files_remaining - 1))
                return self._format_time_duration(total_eta)
            else:
                return self._format_time_duration(current_file_eta)
        elif avg_time_eta is not None:
            total_eta = avg_time_eta * files_remaining
            return self._format_time_duration(total_eta)
        elif speed_based_eta is not None:
            return self._format_time_duration(speed_based_eta)
        else:
            return "Calculating..."
    
    def _get_current_file_eta(self) -> Optional[float]:
        """Calculate ETA for current file based on progress."""
        if not self.file_start_time or self.file_total_bytes is None or self.file_total_bytes <= 0:
            return None
            
        bytes_remaining = self.file_total_bytes - self.file_bytes
        if bytes_remaining <= 0:
            return 0.0
            
        current_speed = self.get_current_speed()
        if current_speed > 0:
            return bytes_remaining / current_speed
        
        # Fallback to average speed
        avg_speed = self.get_average_speed()
        if avg_speed > 0:
            return bytes_remaining / avg_speed
            
        return None
    
    def _get_avg_time_eta(self, files_remaining: int, files_done: int) -> Optional[float]:
        """Calculate ETA based on average time per file with trend analysis."""
        if not self.file_times:
            return None
            
        # Calculate weighted average (recent files have more weight)
        weighted_sum = 0
        total_weight = 0
        for i, file_time in enumerate(self.file_times):
            weight = (i + 1) ** 1.5  # Exponential weighting
            weighted_sum += file_time * weight
            total_weight += weight
            
        avg_time = weighted_sum / total_weight if total_weight > 0 else 0
        
        # Adjust based on trend (if download is speeding up/slowing down)
        if len(self.file_times) >= 3:
            recent_avg = sum(self.file_times[-3:]) / 3
            older_avg = sum(self.file_times[:-3]) / len(self.file_times[:-3]) if len(self.file_times) > 3 else recent_avg
            trend_factor = recent_avg / older_avg if older_avg > 0 else 1.0
            
            # Apply trend factor (but limit to reasonable range)
            trend_factor = max(0.5, min(2.0, trend_factor))
            avg_time *= trend_factor
            
        return avg_time
    
    def _get_speed_based_eta(self, files_remaining: int) -> Optional[float]:
        """Calculate ETA based on average speed and average file size."""
        if not self.file_sizes:
            return None
            
        avg_speed = self.get_average_speed()
        if avg_speed <= 0:
            return None
            
        avg_file_size = sum(self.file_sizes) / len(self.file_sizes)
        if avg_file_size <= 0:
            return None
            
        avg_time_per_file = avg_file_size / avg_speed
        return avg_time_per_file * files_remaining
    
    def format_speed(self, speed_bps: float) -> str:
        """Format speed in human readable format."""
        if speed_bps >= 1024 * 1024:  # MB/s
            return f"{speed_bps / (1024 * 1024):.2f} MB/s"
        elif speed_bps >= 1024:  # KB/s
            return f"{speed_bps / 1024:.2f} KB/s"
        else:  # B/s
            return f"{speed_bps:.0f} B/s"
    
    def format_elapsed(self) -> str:
        """Format elapsed time since download started."""
        if not self.start_time:
            return "0s"
            
        elapsed = time.time() - self.start_time
        return self._format_time_duration(elapsed)
    
    def _format_time_duration(self, seconds: float) -> str:
        """Format seconds into human readable time duration."""
        if seconds < 60:
            return f"{int(seconds)}s"
        elif seconds < 3600:
            minutes = int(seconds / 60)
            seconds_remain = int(seconds % 60)
            return f"{minutes}m {seconds_remain}s"
        elif seconds < 86400:  # Less than a day
            hours = int(seconds / 3600)
            minutes = int((seconds % 3600) / 60)
            return f"{hours}h {minutes}m"
        else:
            days = int(seconds / 86400)
            hours = int((seconds % 86400) / 3600)
            return f"{days}d {hours}h"
    
    def format_downloaded(self) -> str:
        """Format total downloaded bytes."""
        if self.total_bytes >= 1024 * 1024 * 1024:  # GB
            return f"{self.total_bytes / (1024 * 1024 * 1024):.2f} GB"
        elif self.total_bytes >= 1024 * 1024:  # MB
            return f"{self.total_bytes / (1024 * 1024):.2f} MB"
        elif self.total_bytes >= 1024:  # KB
            return f"{self.total_bytes / 1024:.2f} KB"
        else:
            return f"{self.total_bytes} B"


# ============================================================
# GUI APP - FINAL VERSION 1.0 (IMPROVED ETA)
# ============================================================

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("NEX-GDDP-CMIP6 Downloader v1.0")
        self.geometry("1360x920")
        self.minsize(1200, 750)
        
        # Set icon (if available)
        try:
            self.iconbitmap(default=r'C:\Work\ipnb\Project 2\Code\icons\app_icon.ico')
        except Exception:
            pass

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

        # Current tab
        self.current_tab = 0
        
        # Download stats
        self.speed_calc = SpeedCalculator()
        self.total_files = 0
        self.files_done = 0
        self.download_start_time = None
        self.last_speed_update = 0
        self.current_file_total = None  # Store total size of current file

        # events init
        resume_event.set()
        stop_event.clear()

        self._build_ui()
        self._bind_events()

        self.after(60, self._pump_ui_queue)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self._log("[INFO] Welcome to NEX-GDDP-CMIP6 Downloader v1.0!")
        self._set_status("Ready. Start by loading models from Step 1.")
        self._update_progress_bar()

    # ---------------- UI layout ----------------

    def _build_ui(self):
        # Create menu bar
        self._create_menu_bar()
        
        # Main container
        main_container = ttk.Frame(self, padding=5)
        main_container.pack(fill="both", expand=True)

        # ========== TOOLBAR (FIXED) ==========
        toolbar = ttk.Frame(main_container)
        toolbar.pack(fill="x", pady=(0, 10))
        
        # Quick actions in toolbar
        ttk.Label(toolbar, text="Quick Actions:", font=("", 10, "bold")).pack(side="left", padx=(0, 10))
        
        self.btn_load_models = ttk.Button(toolbar, text="Load Models", command=self.load_models)
        self.btn_load_models.pack(side="left", padx=5)
        ToolTip(self.btn_load_models, "Fetch available climate models from NASA servers")
        
        self.btn_load_scen = ttk.Button(toolbar, text="Load Scenarios", command=self.load_scenarios)
        self.btn_load_scen.pack(side="left", padx=5)
        ToolTip(self.btn_load_scen, "Load scenarios for selected model")
        
        self.btn_load_ens = ttk.Button(toolbar, text="Load Ensembles", command=self.load_ensembles)
        self.btn_load_ens.pack(side="left", padx=5)
        ToolTip(self.btn_load_ens, "Load ensembles for selected scenario")
        
        self.btn_load_params = ttk.Button(toolbar, text="Load Parameters", command=self.load_parameters)
        self.btn_load_params.pack(side="left", padx=5)
        ToolTip(self.btn_load_params, "Load parameters for selected ensembles")
        
        ttk.Separator(toolbar, orient="vertical").pack(side="left", padx=15, fill="y")
        
        self.btn_build_queue = ttk.Button(toolbar, text="Build Queue", command=self.build_queue)
        self.btn_build_queue.pack(side="left", padx=5)
        ToolTip(self.btn_build_queue, "Build download queue from current selections")
        
        self.btn_clear_queue = ttk.Button(toolbar, text="Clear Queue", command=self.clear_queue)
        self.btn_clear_queue.pack(side="left", padx=5)
        ToolTip(self.btn_clear_queue, "Clear the download queue")

        # ========== PROGRESS BAR ==========
        progress_frame = ttk.Frame(main_container)
        progress_frame.pack(fill="x", pady=(0, 10))
        
        self.progress_labels = [
            "1. Select Model",
            "2. Select Scenario", 
            "3. Select Ensembles",
            "4. Select Parameters",
            "5. Configure & Download"
        ]
        
        self.progress_bar = ttk.Progressbar(progress_frame, orient="horizontal", 
                                           mode="determinate", maximum=100)
        self.progress_bar.pack(fill="x", pady=(5, 0))
        
        labels_frame = ttk.Frame(progress_frame)
        labels_frame.pack(fill="x")
        
        for i, label in enumerate(self.progress_labels):
            lbl = ttk.Label(labels_frame, text=label, font=("", 9))
            lbl.pack(side="left", padx=20, expand=True)
            if i < len(self.progress_labels) - 1:
                ttk.Label(labels_frame, text="→", font=("", 9)).pack(side="left", padx=5)

        # ========== NOTEBOOK (TABBED INTERFACE) ==========
        notebook_frame = ttk.Frame(main_container)
        notebook_frame.pack(fill="both", expand=True, pady=(0, 10))
        
        self.notebook = ttk.Notebook(notebook_frame)
        self.notebook.pack(fill="both", expand=True)
        
        # Create tabs
        self.tab1 = self._create_tab1()
        self.tab2 = self._create_tab2()
        self.tab3 = self._create_tab3()
        self.tab4 = self._create_tab4()
        self.tab5 = self._create_tab5()
        
        self.notebook.add(self.tab1, text="Step 1: Select Model")
        self.notebook.add(self.tab2, text="Step 2: Select Scenario")
        self.notebook.add(self.tab3, text="Step 3: Select Ensembles")
        self.notebook.add(self.tab4, text="Step 4: Select Parameters")
        self.notebook.add(self.tab5, text="Step 5: Configure & Download")
        
        # Disable tabs initially
        for i in range(1, 5):
            self.notebook.tab(i, state="disabled")

        # ========== NAVIGATION BUTTONS ==========
        nav_frame = ttk.Frame(main_container)
        nav_frame.pack(fill="x", pady=(0, 10))
        
        self.btn_prev = ttk.Button(nav_frame, text="← Previous", command=self.prev_tab, state="disabled")
        self.btn_prev.pack(side="left", padx=5)
        
        self.btn_next = ttk.Button(nav_frame, text="Next →", command=self.next_tab)
        self.btn_next.pack(side="right", padx=5)
        
        ttk.Label(nav_frame, text="Use Previous/Next buttons or click tabs directly").pack(side="left", padx=20)

        # ========== STATUS AND LOG ==========
        # Status bar
        self.status_var = tk.StringVar(value="Ready. Start by loading models from Step 1.")
        status_bar = ttk.Label(main_container, textvariable=self.status_var, 
                              relief="sunken", anchor="w", padding=(10, 5))
        status_bar.pack(fill="x", pady=(0, 5))
        
        # Log frame
        log_frame = ttk.LabelFrame(main_container, text="Activity Log", padding=10)
        log_frame.pack(fill="both", expand=False)
        
        self.log_text = tk.Text(log_frame, height=8, wrap="word")
        self.log_text.pack(side="left", fill="both", expand=True)
        
        log_sb = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_sb.set)
        log_sb.pack(side="right", fill="y")

    def _create_menu_bar(self):
        """Create menu bar with Help and About."""
        menubar = tk.Menu(self)
        self.config(menu=menubar)
        
        # Help menu
        help_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Help", menu=help_menu)
        help_menu.add_command(label="About", command=self.show_about)
        help_menu.add_separator()
        help_menu.add_command(label="Documentation", command=self.show_documentation)
        help_menu.add_command(label="Keyboard Shortcuts", command=self.show_shortcuts)

    def _create_tab1(self):
        """Tab 1: Select Model"""
        tab = ttk.Frame(self.notebook, padding=15)
        
        # Instructions
        instr = ttk.LabelFrame(tab, text="Instructions", padding=10)
        instr.pack(fill="x", pady=(0, 15))
        
        ttk.Label(instr, text="1. Click 'Load Models' to fetch available climate models from NASA servers\n"
                             "2. Select one model from the list\n"
                             "3. Click 'Next' to proceed to Scenario selection",
                 justify="left").pack(anchor="w")
        
        # Model selection frame
        model_frame = ttk.LabelFrame(tab, text="Available Models", padding=10)
        model_frame.pack(fill="both", expand=True)
        
        # Searchable listbox for models
        self.models_listbox = SearchableListbox(model_frame, "Models", multi_select=False)
        self.models_listbox.pack(fill="both", expand=True)
        
        # Bind selection change
        self.models_listbox.bind_selection_change(self._on_model_selected)
        
        # Load button inside tab
        btn_frame = ttk.Frame(model_frame)
        btn_frame.pack(fill="x", pady=(10, 0))
        
        ttk.Button(btn_frame, text="Load Models", command=self.load_models).pack(side="left")
        
        return tab

    def _create_tab2(self):
        """Tab 2: Select Scenario"""
        tab = ttk.Frame(self.notebook, padding=15)
        
        # Instructions
        instr = ttk.LabelFrame(tab, text="Instructions", padding=10)
        instr.pack(fill="x", pady=(0, 15))
        
        ttk.Label(instr, text="1. A model must be selected from Step 1\n"
                             "2. Click 'Load Scenarios' to fetch scenarios for the selected model\n"
                             "3. Select one scenario from the list\n"
                             "4. Click 'Next' to proceed to Ensemble selection",
                 justify="left").pack(anchor="w")
        
        # Current model info
        self.current_model_var = tk.StringVar(value="Selected Model: None")
        model_info = ttk.Label(tab, textvariable=self.current_model_var, font=("", 10, "bold"))
        model_info.pack(anchor="w", pady=(0, 10))
        
        # Scenario selection frame
        scenario_frame = ttk.LabelFrame(tab, text="Available Scenarios", padding=10)
        scenario_frame.pack(fill="both", expand=True)
        
        # Searchable listbox for scenarios
        self.scenarios_listbox = SearchableListbox(scenario_frame, "Scenarios", multi_select=False)
        self.scenarios_listbox.pack(fill="both", expand=True)
        
        # Bind selection change
        self.scenarios_listbox.bind_selection_change(self._on_scenario_selected)
        
        # Load button
        btn_frame = ttk.Frame(scenario_frame)
        btn_frame.pack(fill="x", pady=(10, 0))
        
        ttk.Button(btn_frame, text="Load Scenarios", command=self.load_scenarios).pack(side="left")
        
        return tab

    def _create_tab3(self):
        """Tab 3: Select Ensembles"""
        tab = ttk.Frame(self.notebook, padding=15)
        
        # Instructions
        instr = ttk.LabelFrame(tab, text="Instructions", padding=10)
        instr.pack(fill="x", pady=(0, 15))
        
        ttk.Label(instr, text="1. A model and scenario must be selected from previous steps\n"
                             "2. Click 'Load Ensembles' to fetch ensembles for the selected scenario\n"
                             "3. Select one or more ensembles from the list\n"
                             "4. Click 'Next' to proceed to Parameter selection",
                 justify="left").pack(anchor="w")
        
        # Current selection info
        info_frame = ttk.Frame(tab)
        info_frame.pack(fill="x", pady=(0, 10))
        
        self.current_model_var2 = tk.StringVar(value="Model: None")
        self.current_scenario_var = tk.StringVar(value="Scenario: None")
        
        ttk.Label(info_frame, textvariable=self.current_model_var2, font=("", 9)).pack(side="left", padx=(0, 20))
        ttk.Label(info_frame, textvariable=self.current_scenario_var, font=("", 9)).pack(side="left")
        
        # Ensemble selection frame
        ensemble_frame = ttk.LabelFrame(tab, text="Available Ensembles", padding=10)
        ensemble_frame.pack(fill="both", expand=True)
        
        # Searchable listbox for ensembles
        self.ensembles_listbox = SearchableListbox(ensemble_frame, "Ensembles", multi_select=True)
        self.ensembles_listbox.pack(fill="both", expand=True)
        
        # Selection control buttons
        btn_frame = ttk.Frame(ensemble_frame)
        btn_frame.pack(fill="x", pady=(10, 0))
        
        ttk.Button(btn_frame, text="Select All", command=self.ensembles_listbox.select_all).pack(side="left", padx=(0, 5))
        ttk.Button(btn_frame, text="Clear Selection", command=self.ensembles_listbox.clear_selection).pack(side="left", padx=(0, 20))
        
        ttk.Button(btn_frame, text="Load Ensembles", command=self.load_ensembles).pack(side="left")
        
        # Bind selection change
        self.ensembles_listbox.bind_selection_change(self._on_ensemble_selected)
        
        return tab

    def _create_tab4(self):
        """Tab 4: Select Parameters"""
        tab = ttk.Frame(self.notebook, padding=15)
        
        # Instructions
        instr = ttk.LabelFrame(tab, text="Instructions", padding=10)
        instr.pack(fill="x", pady=(0, 15))
        
        ttk.Label(instr, text="1. A model, scenario, and at least one ensemble must be selected\n"
                             "2. Click 'Load Parameters' to fetch parameters for selected ensembles\n"
                             "3. Select one or more parameters from the list\n"
                             "4. Choose whether to show only common parameters\n"
                             "5. Click 'Next' to proceed to Configuration",
                 justify="left").pack(anchor="w")
        
        # Current selection info
        info_frame = ttk.Frame(tab)
        info_frame.pack(fill="x", pady=(0, 10))
        
        self.current_model_var3 = tk.StringVar(value="Model: None")
        self.current_scenario_var2 = tk.StringVar(value="Scenario: None")
        self.current_ensembles_var = tk.StringVar(value="Ensembles: 0 selected")
        
        ttk.Label(info_frame, textvariable=self.current_model_var3, font=("", 9)).pack(side="left", padx=(0, 15))
        ttk.Label(info_frame, textvariable=self.current_scenario_var2, font=("", 9)).pack(side="left", padx=(0, 15))
        ttk.Label(info_frame, textvariable=self.current_ensembles_var, font=("", 9)).pack(side="left")
        
        # Parameter selection frame
        param_frame = ttk.LabelFrame(tab, text="Available Parameters", padding=10)
        param_frame.pack(fill="both", expand=True)
        
        # Checkbox group for parameters (better for multi-select)
        self.params_checkbox = CheckboxGroup(param_frame, "Parameters", [], 
                                             on_change_callback=self._update_navigation_buttons)
        self.params_checkbox.pack(fill="both", expand=True)
        
        # Options frame
        options_frame = ttk.Frame(param_frame)
        options_frame.pack(fill="x", pady=(10, 0))
        
        self.common_params_only_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(options_frame, text="Show only common parameters across selected ensembles",
                       variable=self.common_params_only_var).pack(side="left", padx=(0, 20))
        
        # Load button
        ttk.Button(options_frame, text="Load Parameters", command=self.load_parameters).pack(side="left")
        
        return tab

    def _create_tab5(self):
        """Tab 5: Configure & Download"""
        tab = ttk.Frame(self.notebook, padding=15)
        
        # Left panel: Configuration
        config_frame = ttk.LabelFrame(tab, text="Download Configuration", padding=10)
        config_frame.pack(side="left", fill="both", expand=True, padx=(0, 10))
        
        # Output directory
        ttk.Label(config_frame, text="Output Directory:").grid(row=0, column=0, sticky="w", pady=(0, 5))
        self.out_dir_var = tk.StringVar(value=os.path.abspath("cmip6_downloads"))
        out_dir_entry = ttk.Entry(config_frame, textvariable=self.out_dir_var, width=50)
        out_dir_entry.grid(row=0, column=1, sticky="we", pady=(0, 5), padx=(5, 5))
        ttk.Button(config_frame, text="Browse...", command=self.browse_dir, width=10).grid(row=0, column=2, pady=(0, 5))
        
        # Year range
        ttk.Label(config_frame, text="Year Range:").grid(row=1, column=0, sticky="w", pady=5)
        
        year_frame = ttk.Frame(config_frame)
        year_frame.grid(row=1, column=1, columnspan=2, sticky="w", pady=5, padx=5)
        
        self.y0_var = tk.StringVar(value="1950")
        self.y1_var = tk.StringVar(value="2100")
        
        ttk.Entry(year_frame, textvariable=self.y0_var, width=8).pack(side="left")
        ttk.Label(year_frame, text=" to ").pack(side="left", padx=2)
        ttk.Entry(year_frame, textvariable=self.y1_var, width=8).pack(side="left")
        ttk.Label(year_frame, text=" (inclusive)").pack(side="left", padx=(5, 0))
        
        # Version filter
        ttk.Label(config_frame, text="Version Filter:").grid(row=2, column=0, sticky="w", pady=5)
        self.version_filter_var = tk.StringVar(value=DEFAULT_VERSION_FILTER)
        ttk.Entry(config_frame, textvariable=self.version_filter_var, width=30).grid(row=2, column=1, sticky="w", pady=5, padx=5)
        
        # Download settings
        ttk.Label(config_frame, text="Download Workers:").grid(row=3, column=0, sticky="w", pady=5)
        self.workers_var = tk.IntVar(value=DEFAULT_WORKERS)
        ttk.Spinbox(config_frame, from_=1, to=16, textvariable=self.workers_var, width=8).grid(row=3, column=1, sticky="w", pady=5, padx=5)
        
        ttk.Label(config_frame, text="Segments per File:").grid(row=4, column=0, sticky="w", pady=5)
        self.segments_var = tk.IntVar(value=DEFAULT_SEGMENTS)
        ttk.Spinbox(config_frame, from_=1, to=16, textvariable=self.segments_var, width=8).grid(row=4, column=1, sticky="w", pady=5, padx=5)
        
        # Auto-advance option
        self.auto_advance_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(config_frame, text="Enable auto-advance between steps",
                       variable=self.auto_advance_var).grid(row=5, column=0, columnspan=3, sticky="w", pady=(10, 5))
        
        config_frame.columnconfigure(1, weight=1)
        
        # Right panel: Queue and Download
        queue_frame = ttk.LabelFrame(tab, text="Download Queue & Statistics", padding=10)
        queue_frame.pack(side="right", fill="both", expand=True)
        
        # Queue info
        self.queue_info_var = tk.StringVar(value="Queue: 0 files")
        ttk.Label(queue_frame, textvariable=self.queue_info_var, font=("", 10, "bold")).pack(anchor="w", pady=(0, 10))
        
        # Queue treeview
        columns = ("year", "filename", "out_dir")
        self.tree = ttk.Treeview(queue_frame, columns=columns, show="headings", height=10)
        self.tree.heading("year", text="Year(s)")
        self.tree.heading("filename", text="Filename")
        self.tree.heading("out_dir", text="Output folder")
        self.tree.column("year", width=90, anchor="w")
        self.tree.column("filename", width=250, anchor="w")
        self.tree.column("out_dir", width=300, anchor="w")
        self.tree.pack(fill="both", expand=True, pady=(0, 10))
        
        tsb = ttk.Scrollbar(queue_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tsb.set)
        tsb.place(in_=self.tree, relx=1.0, rely=0, relheight=1.0, anchor="ne")
        
        # Download controls
        dl_frame = ttk.Frame(queue_frame)
        dl_frame.pack(fill="x", pady=(0, 10))
        
        self.btn_start = ttk.Button(dl_frame, text="Start Download", command=self.start_download)
        self.btn_pause = ttk.Button(dl_frame, text="Pause", command=self.pause_download, state="disabled")
        self.btn_resume = ttk.Button(dl_frame, text="Resume", command=self.resume_download, state="disabled")
        self.btn_stop = ttk.Button(dl_frame, text="Stop", command=self.stop_download, state="disabled")
        
        self.btn_start.pack(side="left", padx=(0, 5))
        self.btn_pause.pack(side="left", padx=5)
        self.btn_resume.pack(side="left", padx=5)
        self.btn_stop.pack(side="left", padx=5)
        
        # ========== DOWNLOAD STATISTICS ==========
        stats_frame = ttk.LabelFrame(queue_frame, text="Download Statistics", padding=10)
        stats_frame.pack(fill="x", pady=(0, 10))
        
        # Current file info
        self.cur_file_var = tk.StringVar(value="Current file: -")
        ttk.Label(stats_frame, textvariable=self.cur_file_var, font=("", 9)).pack(anchor="w")
        
        # Progress bars
        self.overall_var = tk.StringVar(value="Overall: 0/0")
        ttk.Label(stats_frame, textvariable=self.overall_var, font=("", 9)).pack(anchor="w", pady=(5, 0))
        
        self.pb_file = ttk.Progressbar(stats_frame, orient="horizontal", mode="determinate")
        self.pb_file.pack(fill="x", pady=(6, 4))
        
        self.pb_overall = ttk.Progressbar(stats_frame, orient="horizontal", mode="determinate")
        self.pb_overall.pack(fill="x", pady=(0, 10))
        
        # Speed and ETA info (IMPROVED ETA)
        speed_frame = ttk.Frame(stats_frame)
        speed_frame.pack(fill="x", pady=(5, 0))
        
        # Left column
        left_col = ttk.Frame(speed_frame)
        left_col.pack(side="left", fill="x", expand=True)
        
        self.speed_var = tk.StringVar(value="Speed: -")
        self.avg_speed_var = tk.StringVar(value="Avg Speed: -")
        self.downloaded_var = tk.StringVar(value="Downloaded: -")
        
        ttk.Label(left_col, textvariable=self.speed_var, font=("", 9)).pack(anchor="w")
        ttk.Label(left_col, textvariable=self.avg_speed_var, font=("", 9)).pack(anchor="w", pady=(2, 0))
        ttk.Label(left_col, textvariable=self.downloaded_var, font=("", 9)).pack(anchor="w", pady=(2, 0))
        
        # Right column
        right_col = ttk.Frame(speed_frame)
        right_col.pack(side="right", fill="x", expand=True)
        
        self.eta_var = tk.StringVar(value="ETA: -")
        self.elapsed_var = tk.StringVar(value="Elapsed: -")
        self.remaining_files_var = tk.StringVar(value="Remaining: -")
        
        ttk.Label(right_col, textvariable=self.eta_var, font=("", 9)).pack(anchor="e")
        ttk.Label(right_col, textvariable=self.elapsed_var, font=("", 9)).pack(anchor="e", pady=(2, 0))
        ttk.Label(right_col, textvariable=self.remaining_files_var, font=("", 9)).pack(anchor="e", pady=(2, 0))
        
        return tab

    def _bind_events(self):
        # Notebook tab change
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        
        # Year/filter/out changes
        for var in (self.y0_var, self.y1_var, self.version_filter_var, self.out_dir_var):
            var.trace_add("write", lambda *_: self._on_settings_changed())

    # ---------------- Menu Actions ----------------

    def show_about(self):
        """Show about dialog."""
        AboutDialog(self)
    
    def show_documentation(self):
        """Show documentation."""
        messagebox.showinfo("Documentation", 
                          "NEX-GDDP-CMIP6 Downloader Documentation\n\n"
                          "This tool downloads climate projection data from NASA servers.\n"
                          "Follow the 5-step process:\n"
                          "1. Select a climate model\n"
                          "2. Choose a scenario\n"
                          "3. Select one or more ensembles\n"
                          "4. Choose parameters to download\n"
                          "5. Configure settings and start download\n\n"
                          "For more information, visit the NASA NEX-GDDP website.")
    
    def show_shortcuts(self):
        """Show keyboard shortcuts."""
        messagebox.showinfo("Keyboard Shortcuts",
                          "Keyboard Shortcuts:\n\n"
                          "Ctrl+N: New session\n"
                          "Ctrl+O: Open settings\n"
                          "Ctrl+S: Save configuration\n"
                          "Ctrl+Q: Quit application\n"
                          "F1: Help\n"
                          "F5: Refresh current list\n"
                          "Tab: Next field\n"
                          "Shift+Tab: Previous field\n"
                          "Esc: Close dialogs")

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

    # ---------------- UI queue pump ----------------

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
                    model_titles = [m["title"] for m in self.models]
                    self.models_listbox.set_items(model_titles)
                    self._set_status(f"Models loaded ({len(self.models)}).")
                    self._log(f"[INFO] Loaded models: {len(self.models)}")
                    
                    # Auto-select first if auto-advance enabled
                    if self.auto_advance_var.get() and model_titles:
                        self._select_first_item(self.models_listbox)

                elif et == "scenarios_loaded":
                    self.scenarios = ev["scenarios"]
                    scenario_titles = [s["title"] for s in self.scenarios]
                    self.scenarios_listbox.set_items(scenario_titles)
                    self._set_status(f"Scenarios loaded ({len(self.scenarios)}).")
                    self._log(f"[INFO] Loaded scenarios: {len(self.scenarios)}")
                    
                    if self.auto_advance_var.get() and scenario_titles:
                        self._select_first_item(self.scenarios_listbox)

                elif et == "ensembles_loaded":
                    self.ensembles = ev["ensembles"]
                    ensemble_titles = [e["title"] for e in self.ensembles]
                    self.ensembles_listbox.set_items(ensemble_titles)
                    self._set_status(f"Ensembles loaded ({len(self.ensembles)}).")
                    self._log(f"[INFO] Loaded ensembles: {len(self.ensembles)}")
                    
                    if self.auto_advance_var.get() and ensemble_titles:
                        self.ensembles_listbox.select_all()

                elif et == "params_loaded":
                    self.params_by_ens = ev["params_by_ens"]
                    self.param_titles = ev["param_titles"]
                    self.params_checkbox.set_items(self.param_titles)
                    self._set_status(f"Parameters loaded ({len(self.param_titles)}).")
                    self._log(f"[INFO] Loaded parameters: {len(self.param_titles)}")
                    
                    if self.auto_advance_var.get() and self.param_titles:
                        self.params_checkbox.select_all()
                    # Update navigation buttons after loading parameters
                    self._update_navigation_buttons()

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
                    self.current_file_total = total
                    # Pass file total to speed calculator for better ETA
                    self.speed_calc.set_file_total(total)
                    
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
                    # Update speed calculator
                    self.speed_calc.add_bytes(ev["delta"])
                    
                    # Update speed display more frequently during active download
                    current_time = time.time()
                    if current_time - self.last_speed_update > 0.5:  # Update every 0.5 seconds
                        self._update_speed_display()
                        self.last_speed_update = current_time

                elif et == "dl_file_start":
                    self.cur_file_var.set(f"Current file: {ev['filename']}")
                    self.overall_var.set(f"Overall: {ev['index']}/{ev['total_files']}")
                    self.pb_overall.configure(mode="determinate", maximum=ev["total_files"], value=ev["index"] - 1)
                    self._set_status(f"Downloading {ev['index']}/{ev['total_files']} …")
                    
                    # Start new file in speed calculator
                    self.speed_calc.start_file(self.current_file_total)
                    self.files_done = ev["index"] - 1
                    self.total_files = ev["total_files"]
                    self._update_speed_display()

                elif et == "dl_file_done":
                    self.pb_overall["value"] = ev["index"]
                    self.files_done = ev["index"]
                    # Record file completion for better ETA prediction
                    self.speed_calc.record_file_completion(self.current_file_total)
                    # Force update of speed display when file finishes
                    self._update_speed_display()
                    # Record elapsed time for the finished file
                    elapsed = self.speed_calc.format_elapsed()
                    self._log(f"[INFO] File {ev['index']}/{self.total_files} completed. Elapsed: {elapsed}")

                elif et == "dl_restart_progress":
                    self.pb_file.stop()
                    self.pb_file["value"] = 0

                elif et == "done":
                    self._log(ev["msg"])
                    self._set_status("Download completed.")
                    self._apply_dl_state({"running": False, "paused": False})
                    
                    # Final update of speed display
                    self._update_speed_display()
                    
                    # Show final statistics
                    total_elapsed = self.speed_calc.format_elapsed()
                    total_downloaded = self.speed_calc.format_downloaded()
                    self._log(f"[INFO] Download completed in {total_elapsed}")
                    self._log(f"[INFO] Total downloaded: {total_downloaded}")

        except queue.Empty:
            pass

        # Update speed display periodically even when no events (for elapsed time)
        current_time = time.time()
        if self.is_downloading and current_time - self.last_speed_update > 1.0:
            self._update_speed_display()
            self.last_speed_update = current_time

        interval = 50 if (self.is_downloading and not self.is_paused) else (90 if self.is_downloading else 140)
        self.after(interval, self._pump_ui_queue)

    def _select_first_item(self, listbox_widget):
        """Select first item in a SearchableListbox."""
        if hasattr(listbox_widget, 'lb'):
            listbox_widget.lb.selection_clear(0, "end")
            if listbox_widget.lb.size() > 0:
                listbox_widget.lb.selection_set(0)
                listbox_widget.lb.see(0)
                listbox_widget._update_selection_info()

    # ---------------- Speed Display (IMPROVED ETA) ----------------

    def _update_speed_display(self):
        """Update speed and ETA display with improved ETA calculation."""
        if not self.is_downloading:
            return
            
        # Update speed
        current_speed = self.speed_calc.get_current_speed()
        avg_speed = self.speed_calc.get_average_speed()
        
        self.speed_var.set(f"Speed: {self.speed_calc.format_speed(current_speed)}")
        self.avg_speed_var.set(f"Avg Speed: {self.speed_calc.format_speed(avg_speed)}")
        self.downloaded_var.set(f"Downloaded: {self.speed_calc.format_downloaded()}")
        
        # Update ETA and remaining files
        files_remaining = self.total_files - self.files_done
        self.remaining_files_var.set(f"Remaining: {files_remaining} files")
        
        # Calculate ETA using improved algorithm
        if self.total_files > 0:
            eta = self.speed_calc.get_eta(files_remaining, self.files_done)
            self.eta_var.set(f"ETA: {eta}")
        else:
            self.eta_var.set("ETA: -")
            
        # Update elapsed time
        elapsed = self.speed_calc.format_elapsed()
        self.elapsed_var.set(f"Elapsed: {elapsed}")

    # ---------------- Tab Navigation ----------------

    def _on_tab_changed(self, event=None):
        self.current_tab = self.notebook.index(self.notebook.select())
        self._update_navigation_buttons()
        self._update_progress_bar()
        
        # Update info displays
        self._update_selection_info()

    def _update_navigation_buttons(self):
        """Update state of Previous/Next buttons based on current tab."""
        # Previous button
        if self.current_tab == 0:
            self.btn_prev.configure(state="disabled")
        else:
            self.btn_prev.configure(state="normal")
        
        # Next button
        if self.current_tab == 4:  # Last tab
            self.btn_next.configure(state="disabled")
        else:
            # Check if we can proceed to next tab
            can_proceed = self._can_proceed_to_next()
            self.btn_next.configure(state="normal" if can_proceed else "disabled")

    def _can_proceed_to_next(self) -> bool:
        """Check if user can proceed to next tab."""
        if self.current_tab == 0:  # Model selection
            return bool(self.models_listbox.get_selected())
        elif self.current_tab == 1:  # Scenario selection
            return bool(self.scenarios_listbox.get_selected())
        elif self.current_tab == 2:  # Ensemble selection
            return len(self.ensembles_listbox.get_selected()) > 0
        elif self.current_tab == 3:  # Parameter selection
            # Check if at least one parameter is selected
            return len(self.params_checkbox.get_selected()) > 0
        return True

    def prev_tab(self):
        if self.current_tab > 0:
            self.notebook.select(self.current_tab - 1)

    def next_tab(self):
        if self.current_tab < 4 and self._can_proceed_to_next():
            # Enable next tab
            self.notebook.tab(self.current_tab + 1, state="normal")
            self.notebook.select(self.current_tab + 1)

    def _update_progress_bar(self):
        """Update the progress bar based on current tab."""
        progress = ((self.current_tab + 1) / 5) * 100
        self.progress_bar["value"] = progress

    # ---------------- Selection Updates ----------------

    def _on_model_selected(self):
        selected = self.models_listbox.get_selected()
        if selected:
            model_name = selected[0]
            self.current_model_var.set(f"Selected Model: {model_name}")
            self.current_model_var2.set(f"Model: {model_name}")
            self.current_model_var3.set(f"Model: {model_name}")
            
            # Enable next tab if not already enabled
            self.notebook.tab(1, state="normal")
        
        self._update_navigation_buttons()

    def _on_scenario_selected(self):
        selected = self.scenarios_listbox.get_selected()
        if selected:
            scenario_name = selected[0]
            self.current_scenario_var.set(f"Scenario: {scenario_name}")
            self.current_scenario_var2.set(f"Scenario: {scenario_name}")
            
            # Enable next tab if not already enabled
            self.notebook.tab(2, state="normal")
        
        self._update_navigation_buttons()

    def _on_ensemble_selected(self):
        selected = self.ensembles_listbox.get_selected()
        count = len(selected)
        self.current_ensembles_var.set(f"Ensembles: {count} selected")
        
        if count > 0:
            # Enable next tab if not already enabled
            self.notebook.tab(3, state="normal")
        
        self._update_navigation_buttons()

    def _on_settings_changed(self, *_):
        # Validate year range
        try:
            y0 = int(self.y0_var.get().strip())
            y1 = int(self.y1_var.get().strip())
            if y0 > y1:
                self._set_status("Warning: Start year must be <= end year")
        except ValueError:
            self._set_status("Warning: Invalid year format")

    def _update_selection_info(self):
        """Update all selection info displays."""
        # Update current model info
        selected_model = self.models_listbox.get_selected()
        if selected_model:
            model_name = selected_model[0]
            self.current_model_var.set(f"Selected Model: {model_name}")
            self.current_model_var2.set(f"Model: {model_name}")
            self.current_model_var3.set(f"Model: {model_name}")
        
        # Update current scenario info
        selected_scenario = self.scenarios_listbox.get_selected()
        if selected_scenario:
            scenario_name = selected_scenario[0]
            self.current_scenario_var.set(f"Scenario: {scenario_name}")
            self.current_scenario_var2.set(f"Scenario: {scenario_name}")
        
        # Update current ensemble info
        selected_ensembles = self.ensembles_listbox.get_selected()
        count = len(selected_ensembles)
        self.current_ensembles_var.set(f"Ensembles: {count} selected")

    # ---------------- Convenience helpers ----------------

    def _selected_one(self, listbox_widget) -> Optional[str]:
        selected = listbox_widget.get_selected()
        return selected[0] if selected else None

    def _selected_multi(self, listbox_widget) -> List[str]:
        return listbox_widget.get_selected()

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
        model_title = self._selected_one(self.models_listbox)
        if not model_title:
            messagebox.showwarning("Select model", "Select a model first.")
            return
            
        model = self._find_entry_by_title(self.models, model_title)
        if not model:
            messagebox.showerror("Error", "Selected model not found in cache.")
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
        scen_title = self._selected_one(self.scenarios_listbox)
        if not scen_title:
            messagebox.showwarning("Select scenario", "Select a scenario first.")
            return
            
        scen = self._find_entry_by_title(self.scenarios, scen_title)
        if not scen:
            messagebox.showerror("Error", "Selected scenario not found in cache.")
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
        ens_titles = self._selected_multi(self.ensembles_listbox)
        if not ens_titles:
            messagebox.showwarning("Select ensembles", "Select one or more ensembles.")
            return

        selected_ens_entries = []
        for t in ens_titles:
            e = self._find_entry_by_title(self.ensembles, t)
            if e:
                selected_ens_entries.append(e)

        if not selected_ens_entries:
            messagebox.showerror("Error", "Selected ensembles not found in cache.")
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
        # Get selections
        model_name = self._selected_one(self.models_listbox) or ""
        scen_name = self._selected_one(self.scenarios_listbox) or ""
        ens_titles = self._selected_multi(self.ensembles_listbox)
        param_titles = self.params_checkbox.get_selected()

        # Validate inputs
        if not model_name or not scen_name or not ens_titles or not param_titles:
            messagebox.showwarning("Incomplete Selection", 
                                "Please complete all selection steps (1-4) before building queue.")
            return

        out_dir = self.out_dir_var.get().strip()
        if not out_dir:
            messagebox.showwarning("Output Directory", "Please specify an output directory.")
            return

        try:
            y0 = int(self.y0_var.get().strip())
            y1 = int(self.y1_var.get().strip())
            if y0 > y1:
                messagebox.showwarning("Invalid Years", "Start year must be <= end year.")
                return
        except ValueError:
            messagebox.showwarning("Invalid Years", "Please enter valid year numbers.")
            return

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
        for w in (self.btn_load_models, self.btn_load_scen, self.btn_load_ens, 
                 self.btn_load_params, self.btn_build_queue, self.btn_clear_queue):
            w.configure(state=cat_state)

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

        # Initialize speed calculator
        self.speed_calc.start_download()
        self.download_start_time = time.time()
        self.last_speed_update = time.time()
        self.total_files = len(self.download_items)
        self.files_done = 0
        
        # Reset speed display
        self.speed_var.set("Speed: Calculating...")
        self.avg_speed_var.set("Avg Speed: Calculating...")
        self.eta_var.set("ETA: Calculating...")
        self.downloaded_var.set("Downloaded: 0 B")
        self.elapsed_var.set("Elapsed: 0s")
        self.remaining_files_var.set(f"Remaining: {self.total_files} files")

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
                self.btn_build_queue, self.btn_clear_queue, self.btn_prev, self.btn_next
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