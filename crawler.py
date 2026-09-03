#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import os
import re
import time
import hashlib
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode
from urllib.robotparser import RobotFileParser

import pandas as pd
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


SKIP_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico",
    ".pdf", ".zip", ".gz", ".rar", ".7z",
    ".mp3", ".wav", ".m4a", ".mp4", ".mov", ".avi", ".wmv",
    ".css", ".js", ".json", ".xml", ".txt",
    ".woff", ".woff2", ".ttf", ".eot",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
}

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "yclid", "msclkid",
}

DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.8,en;q=0.7",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def host_key(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return re.sub(r"[^a-z0-9._-]+", "_", host)


def normalize_url(url: str, keep_query: bool = False) -> Optional[str]:
    try:
        p = urlparse(url.strip())
    except Exception:
        return None

    if p.scheme not in ("http", "https"):
        return None

    scheme = p.scheme.lower()
    netloc = p.netloc.lower()

    # Remove default ports.
    if netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    if netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]

    path = re.sub(r"/{2,}", "/", p.path or "/")
    query = ""

    if keep_query and p.query:
        pairs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                 if k.lower() not in TRACKING_PARAMS]
        if pairs:
            query = urlencode(sorted(pairs))

    normalized = urlunparse((scheme, netloc, path, "", query, ""))
    return normalized


def is_same_site(url: str, allowed_host: str, include_subdomains: bool) -> bool:
    host = urlparse(url).netloc.lower().split(":")[0]
    allowed_host = allowed_host.lower().split(":")[0]
    if host == allowed_host:
        return True
    if include_subdomains and host.endswith("." + allowed_host):
        return True
    return False


def has_skipped_extension(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in SKIP_EXTENSIONS)


def first_second_directory(url: str) -> Tuple[str, str]:
    parts = [x for x in urlparse(url).path.split("/") if x]
    first = f"/{parts[0]}/" if len(parts) >= 1 else "/"
    second = f"/{parts[0]}/{parts[1]}/" if len(parts) >= 2 else ""
    return first, second


def build_session(user_agent: str, timeout: int) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        status=4,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "HEAD"]),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(DEFAULT_HEADERS)
    session.headers["User-Agent"] = user_agent
    return session


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_state(path: Path, start_url: str) -> dict:
    if not path.exists():
        return {
            "start_url": start_url,
            "queue": [[start_url, 0, "START"]],
            "seen": [],
            "rows": [],
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
            "completed": False,
        }
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_state(path: Path, state: dict) -> None:
    state["updated_at"] = utc_now_iso()
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def build_robots(session: requests.Session, start_url: str, user_agent: str, timeout: int):
    p = urlparse(start_url)
    robots_url = f"{p.scheme}://{p.netloc}/robots.txt"
    rp = RobotFileParser()
    rp.set_url(robots_url)
    try:
        r = session.get(robots_url, timeout=timeout)
        if r.ok:
            rp.parse(r.text.splitlines())
            return rp, robots_url, True
    except Exception:
        pass
    return rp, robots_url, False


def extract_page(html: str, base_url: str) -> Tuple[str, str, str, str, List[str]]:
    soup = BeautifulSoup(html, "html.parser")

    title = ""
    if soup.title and soup.title.string:
        title = " ".join(soup.title.string.split())

    canonical = ""
    can = soup.find("link", rel=lambda v: v and "canonical" in str(v).lower())
    if can and can.get("href"):
        canonical = urljoin(base_url, can.get("href"))

    robots_meta = ""
    meta = soup.find("meta", attrs={"name": re.compile(r"^robots$", re.I)})
    if meta and meta.get("content"):
        robots_meta = meta.get("content").strip()

    h1 = ""
    h1_tag = soup.find("h1")
    if h1_tag:
        h1 = " ".join(h1_tag.get_text(" ", strip=True).split())

    links = []
    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
            continue
        links.append(urljoin(base_url, href))

    return title, canonical, robots_meta, h1, links


def write_outputs(rows: List[dict], out_dir: Path, target_url: str) -> Tuple[Path, Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    key = host_key(target_url)

    cols = [
        "url", "title", "h1", "status_code", "content_type", "canonical",
        "robots_meta", "depth", "discovered_from",
        "first_directory", "second_directory",
        "response_ms", "final_url", "fetched_at", "error"
    ]
    df = pd.DataFrame(rows)
    for c in cols:
        if c not in df.columns:
            df[c] = ""
    df = df[cols]

    csv_path = out_dir / f"{key}_urls.csv"
    xlsx_path = out_dir / f"{key}_urls.xlsx"
    summary_path = out_dir / f"{key}_summary.json"

    df.to_csv(csv_path, index=False, encoding="utf-8-sig")

    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="URL一覧")

        summary = pd.DataFrame([
            ["対象サイト", target_url],
            ["取得HTML URL数", len(df)],
            ["200件数", int((df["status_code"] == 200).sum()) if len(df) else 0],
            ["3xx件数", int(df["status_code"].between(300, 399).sum()) if len(df) else 0],
            ["4xx件数", int(df["status_code"].between(400, 499).sum()) if len(df) else 0],
            ["5xx件数", int(df["status_code"].between(500, 599).sum()) if len(df) else 0],
            ["エラー件数", int((df["error"].astype(str) != "").sum()) if len(df) else 0],
            ["出力日時(UTC)", utc_now_iso()],
        ], columns=["項目", "値"])
        summary.to_excel(writer, index=False, sheet_name="集計")

        if len(df):
            dir_summary = (
                df.groupby("first_directory", dropna=False)
                .size()
                .reset_index(name="URL数")
                .sort_values("URL数", ascending=False)
            )
        else:
            dir_summary = pd.DataFrame(columns=["first_directory", "URL数"])
        dir_summary.to_excel(writer, index=False, sheet_name="第1階層集計")

        # Readability tweaks.
        for ws in writer.book.worksheets:
            ws.freeze_panes = "A2"
            for cell in ws[1]:
                cell.font = cell.font.copy(bold=True)
            for col in ws.columns:
                max_len = min(max((len(str(c.value)) if c.value is not None else 0) for c in col), 60)
                ws.column_dimensions[col[0].column_letter].width = max(10, max_len + 2)

    with summary_path.open("w", encoding="utf-8") as f:
        json.dump({
            "target_url": target_url,
            "html_url_count": len(df),
            "generated_at": utc_now_iso(),
        }, f, ensure_ascii=False, indent=2)

    return csv_path, xlsx_path, summary_path


def crawl(config: dict, fresh: bool = False) -> int:
    start_url = normalize_url(config["target_url"], keep_query=config.get("keep_query", False))
    if not start_url:
        raise ValueError("target_url が不正です")

    parsed = urlparse(start_url)
    allowed_host = parsed.netloc.lower().split(":")[0]

    state_dir = Path(config.get("state_dir", "state"))
    output_dir = Path(config.get("output_dir", "output"))
    state_dir.mkdir(parents=True, exist_ok=True)

    state_path = state_dir / f"{host_key(start_url)}_state.json"
    if fresh and state_path.exists():
        state_path.unlink()

    session = build_session(
        config.get("user_agent", "SiteURLCrawler/1.0 (+GitHub Actions)"),
        int(config.get("timeout_seconds", 20)),
    )

    respect_robots = bool(config.get("respect_robots_txt", True))
    rp, robots_url, robots_loaded = build_robots(
        session, start_url, config.get("user_agent", "*"),
        int(config.get("timeout_seconds", 20))
    )

    state = load_state(state_path, start_url)
    q = deque(state.get("queue", [[start_url, 0, "START"]]))
    seen: Set[str] = set(state.get("seen", []))
    rows: List[dict] = state.get("rows", [])

    max_pages = int(config.get("max_pages_per_run", 5000))
    delay = float(config.get("delay_seconds", 0.7))
    timeout = int(config.get("timeout_seconds", 20))
    include_subdomains = bool(config.get("include_subdomains", False))
    keep_query = bool(config.get("keep_query", False))
    max_depth = int(config.get("max_depth", 50))
    save_every = int(config.get("save_every", 50))
    exclude_regex = [re.compile(p) for p in config.get("exclude_url_regex", [])]

    processed_this_run = 0

    print(f"Target: {start_url}")
    print(f"Robots: {robots_url} loaded={robots_loaded} respect={respect_robots}")
    print(f"Resume: seen={len(seen)} queued={len(q)} rows={len(rows)}")
    print(f"Run limit: {max_pages} pages")

    while q and processed_this_run < max_pages:
        raw_url, depth, discovered_from = q.popleft()
        url = normalize_url(raw_url, keep_query=keep_query)
        if not url or url in seen:
            continue
        if depth > max_depth:
            continue
        if not is_same_site(url, allowed_host, include_subdomains):
            continue
        if has_skipped_extension(url):
            continue
        if any(p.search(url) for p in exclude_regex):
            continue
        if respect_robots and robots_loaded and not rp.can_fetch(config.get("user_agent", "*"), url):
            seen.add(url)
            continue

        seen.add(url)
        processed_this_run += 1
        started = time.perf_counter()

        row = {
            "url": url,
            "title": "",
            "h1": "",
            "status_code": 0,
            "content_type": "",
            "canonical": "",
            "robots_meta": "",
            "depth": depth,
            "discovered_from": discovered_from,
            "first_directory": first_second_directory(url)[0],
            "second_directory": first_second_directory(url)[1],
            "response_ms": 0,
            "final_url": "",
            "fetched_at": utc_now_iso(),
            "error": "",
        }

        try:
            r = session.get(url, timeout=timeout, allow_redirects=True)
            row["status_code"] = r.status_code
            row["content_type"] = r.headers.get("Content-Type", "")
            row["final_url"] = normalize_url(r.url, keep_query=keep_query) or r.url
            row["response_ms"] = int((time.perf_counter() - started) * 1000)

            content_type = row["content_type"].lower()
            is_html = "text/html" in content_type or "application/xhtml+xml" in content_type

            if is_html:
                title, canonical, robots_meta, h1, links = extract_page(r.text, r.url)
                row["title"] = title
                row["canonical"] = normalize_url(canonical, keep_query=keep_query) if canonical else ""
                row["robots_meta"] = robots_meta
                row["h1"] = h1

                for link in links:
                    n = normalize_url(link, keep_query=keep_query)
                    if not n or n in seen:
                        continue
                    if not is_same_site(n, allowed_host, include_subdomains):
                        continue
                    if has_skipped_extension(n):
                        continue
                    if any(p.search(n) for p in exclude_regex):
                        continue
                    q.append([n, depth + 1, url])

                rows.append(row)
            else:
                # Non-HTML is intentionally not included in final output.
                pass

        except requests.RequestException as e:
            row["response_ms"] = int((time.perf_counter() - started) * 1000)
            row["error"] = f"{type(e).__name__}: {e}"
            # Network errors are retained for diagnostics.
            rows.append(row)

        if processed_this_run % 25 == 0:
            print(
                f"processed={processed_this_run} "
                f"seen={len(seen)} queue={len(q)} html_rows={len(rows)}"
            )

        if processed_this_run % save_every == 0:
            state["queue"] = list(q)
            state["seen"] = sorted(seen)
            state["rows"] = rows
            state["completed"] = False
            save_state(state_path, state)
            write_outputs(rows, output_dir, start_url)

        time.sleep(delay)

    state["queue"] = list(q)
    state["seen"] = sorted(seen)
    state["rows"] = rows
    state["completed"] = len(q) == 0
    save_state(state_path, state)
    csv_path, xlsx_path, summary_path = write_outputs(rows, output_dir, start_url)

    print("")
    print("=== RESULT ===")
    print(f"Processed this run: {processed_this_run}")
    print(f"Total seen: {len(seen)}")
    print(f"HTML/error rows: {len(rows)}")
    print(f"Remaining queue: {len(q)}")
    print(f"Completed: {state['completed']}")
    print(f"CSV: {csv_path}")
    print(f"Excel: {xlsx_path}")
    print(f"Summary: {summary_path}")
    print(f"State: {state_path}")

    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--fresh", action="store_true", help="保存済みstateを削除して最初から開始")
    args = parser.parse_args()

    config = load_config(Path(args.config))
    return crawl(config, fresh=args.fresh)


if __name__ == "__main__":
    raise SystemExit(main())
