#!/usr/bin/env python3
"""Batch PDF downloader for Flatex document archive (classic flow).

This script mirrors the browser extension approach:
- read rows + filter state from archive page,
- read internal credentials (tokenId/windowId) from page context,
- POST selected row index with Flatex AJAX headers,
- parse execute-command script for PDF URL,
- fetch PDFs with session cookies, with 503 warm-up fallback.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from playwright.sync_api import BrowserContext, Page, Response, sync_playwright

ROW_SELECTOR = 'tr[onclick^="DocumentViewer.openPopupIfRequired"]'
SCRIPT_PATTERNS = [
    re.compile(r'finished\("([^\"]+)",'),
    re.compile(r'display\("([^\"]+)",'),
]
FILENAME_RE = re.compile(r"filename\*?=(?:UTF-8''|\")?([^\";]+)", re.IGNORECASE)


class FlatexError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download Flatex archive PDFs")
    parser.add_argument("--archive-url", required=True, help="Flatex document archive URL")
    parser.add_argument("--output-dir", default="downloads", help="Destination folder")
    parser.add_argument("--profile-dir", default=".playwright-profile", help="Persistent Chromium profile")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout in seconds")
    parser.add_argument("--retries", type=int, default=3, help="Retries per row")
    parser.add_argument("--start-row", type=int, default=1, help="1-based row index to start from")
    parser.add_argument("--end-row", type=int, default=0, help="1-based row index to end at (0 = all)")
    parser.add_argument("--skip-existing", action="store_true", help="Skip file if it already exists")
    parser.add_argument("--headless", action="store_true", help="Run headless")
    return parser.parse_args()


def sanitize_filename(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip())
    safe = safe.strip("._")
    return safe or "document.pdf"


def filename_from_headers_or_url(response: Response, url: str, fallback_stem: str) -> str:
    content_disp = response.headers.get("content-disposition", "")
    match = FILENAME_RE.search(content_disp)
    if match:
        candidate = unquote(match.group(1)).strip()
        if candidate:
            if not candidate.lower().endswith(".pdf"):
                candidate += ".pdf"
            return sanitize_filename(candidate)

    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    for key in ("filename", "file", "name", "documentName", "id"):
        if key in qs and qs[key]:
            candidate = unquote(qs[key][0])
            if not candidate.lower().endswith(".pdf"):
                candidate += ".pdf"
            return sanitize_filename(candidate)

    tail = Path(parsed.path).name
    if tail:
        if not tail.lower().endswith(".pdf"):
            tail += ".pdf"
        return sanitize_filename(unquote(tail))

    return sanitize_filename(f"{fallback_stem}.pdf")


def filename_from_url(url: str, fallback_stem: str) -> str:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    for key in ("filename", "file", "name", "documentName", "id"):
        if key in qs and qs[key]:
            candidate = unquote(qs[key][0]).strip()
            if candidate:
                if not candidate.lower().endswith(".pdf"):
                    candidate += ".pdf"
                return sanitize_filename(candidate)

    tail = Path(parsed.path).name
    if tail:
        if not tail.lower().endswith(".pdf"):
            tail += ".pdf"
        return sanitize_filename(unquote(tail))

    return sanitize_filename(f"{fallback_stem}.pdf")


def build_stable_stem(url: str) -> str:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    for key in ("id", "documentId", "docId", "mailingId", "uuid"):
        if key in qs and qs[key]:
            return f"flatex_{sanitize_filename(qs[key][0])}"
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    return f"flatex_{digest}"


def get_archive_state(page: Page) -> dict:
    now = datetime.now()
    default_start = f"01.01.{now.year - 5}"
    default_end = now.strftime("%d.%m.%Y")

    state = page.evaluate(
        """
        ({ rowSelector, defaultStart, defaultEnd }) => {
          const pick = (selector) => document.querySelector(selector);
          const value = (selector, fallback) => {
            const el = pick(selector);
            if (!el) return fallback;
            if ('value' in el && el.value) return el.value;
            return fallback;
          };
          const idx = (selector, fallback) => {
            const el = pick(selector);
            const v = el?.dataset?.valueSelecteditemindex;
            return (v ?? fallback).toString();
          };

          let tokenId = '';
          let windowId = '';

          try {
            tokenId = window.webcore?.getTokenId?.() || '';
            windowId = window.webcore?.getWindowManagement?.().getCurrentWindowId?.() || '';
          } catch (_) {
            // ignore and return empty values
          }

          return {
            pageUrl: location.href,
            rowCount: document.querySelectorAll(rowSelector).length,
            credentials: { tokenId, windowId },
            form: {
              'dateRangeComponent.startDate.text': value('#documentArchiveListForm_dateRangeComponent_startDate', defaultStart),
              'dateRangeComponent.endDate.text': value('#documentArchiveListForm_dateRangeComponent_endDate', defaultEnd),
              'accountSelection.account.selecteditemindex': idx('#documentArchiveListForm_accountSelection_account', '0'),
              'documentCategory.selecteditemindex': idx('#documentArchiveListForm_documentCategory', '0'),
              'readState.selecteditemindex': idx('#documentArchiveListForm_readState', '0'),
              'dateRangeComponent.retrievalPeriodSelection.selecteditemindex': idx('#documentArchiveListForm_dateRangeComponent_retrievalPeriodSelection', '0'),
              'storeSettings.checked': 'off',
            },
          };
        }
        """,
        {"rowSelector": ROW_SELECTOR, "defaultStart": default_start, "defaultEnd": default_end},
    )
    return state


def wait_for_user_ready() -> None:
    print("\nPrepare the archive page:")
    print("1) Log in")
    print("2) Open the document archive")
    print("3) Apply filters")
    print("4) Scroll until all rows are visible")
    input("Press ENTER to start batch download... ")


def normalize_command_url(url: str, base_url: str) -> str:
    url = url.replace("\\/", "/").replace("\\u0026", "&")
    return urljoin(base_url, url)


def extract_pdf_link_from_script(script: str, base_url: str) -> str:
    for pattern in SCRIPT_PATTERNS:
        match = pattern.search(script)
        if match:
            return normalize_command_url(match.group(1), base_url)
    raise FlatexError("command-invalid: no PDF link pattern matched")


def fetch_row_command(page: Page, token_id: str, window_id: str, form_data: dict[str, str], row_index: int) -> dict:
    payload = page.evaluate(
        """
        async ({ tokenId, windowId, formData, rowIndex }) => {
          const fd = new FormData();
          for (const [k, v] of Object.entries(formData)) {
            fd.set(k, String(v));
          }
          fd.set('documentArchiveListTable.selectedrowidx', String(rowIndex));

          const res = await fetch('', {
            method: 'POST',
            headers: {
              'x-ajax': 'true',
              'x-requested-with': 'XMLHttpRequest',
              'x-tokenid': tokenId,
              'x-windowid': windowId,
            },
            body: fd,
          });

          const text = await res.text();
          try {
            return {
              ok: res.ok,
              status: res.status,
              json: JSON.parse(text),
              parseError: null,
            };
          } catch (error) {
            return {
              ok: res.ok,
              status: res.status,
              json: null,
              parseError: String(error),
              raw: text,
            };
          }
        }
        """,
        {
            "tokenId": token_id,
            "windowId": window_id,
            "formData": form_data,
            "rowIndex": row_index,
        },
    )
    return payload


def get_row_pdf_link(page: Page, state: dict, row_index: int) -> str:
    creds = state["credentials"]
    payload = fetch_row_command(page, creds["tokenId"], creds["windowId"], state["form"], row_index)

    if not payload.get("ok"):
        raise FlatexError(f"row {row_index}: command HTTP {payload.get('status')}")

    data = payload.get("json")
    if not isinstance(data, dict):
        raise FlatexError(f"row {row_index}: command parse failed")

    commands = data.get("commands")
    if not isinstance(commands, list):
        raise FlatexError(f"row {row_index}: command list missing")

    execute = next((cmd for cmd in commands if isinstance(cmd, dict) and cmd.get("command") == "execute"), None)
    if not isinstance(execute, dict) or not isinstance(execute.get("script"), str):
        raise FlatexError(f"row {row_index}: execute command missing")

    return extract_pdf_link_from_script(execute["script"], state["pageUrl"])


def warmup_pdf_link(page: Page, link: str) -> None:
    page.evaluate(
        """
        async (url) => {
          const frame = document.createElement('iframe');
          frame.style.visibility = 'hidden';
          frame.style.opacity = '0';
          frame.style.width = '0';
          frame.style.height = '0';

          await new Promise((resolve, reject) => {
            const t = window.setTimeout(() => reject(new Error('iframe-timeout')), 30000);
            frame.addEventListener('load', () => {
              window.clearTimeout(t);
              resolve(null);
            }, { once: true });
            frame.src = url;
            document.body.appendChild(frame);
          });

          await new Promise((resolve) => setTimeout(resolve, 5000));
          frame.remove();
        }
        """,
        link,
    )


def fetch_pdf_response(context: BrowserContext, link: str, timeout_s: int) -> Response:
    return context.request.get(link, timeout=timeout_s * 1000)


def save_pdf_from_link(
    context: BrowserContext,
    page: Page,
    link: str,
    output_dir: Path,
    timeout_s: int,
    skip_existing: bool,
) -> tuple[bool, str, bool]:
    stem = build_stable_stem(link)
    if skip_existing:
        optimistic_name = filename_from_url(link, stem)
        optimistic_target = output_dir / optimistic_name
        if optimistic_target.exists():
            return True, f"skipped existing {optimistic_target.name}", False

    try:
        response = fetch_pdf_response(context, link, timeout_s)
    except Exception as exc:
        return False, f"request failed: {exc}", True

    if response.status == 503:
        try:
            warmup_pdf_link(page, link)
            response = fetch_pdf_response(context, link, timeout_s)
        except Exception as exc:
            return False, f"503 warm-up failed: {exc}", True

    if not response.ok:
        return False, f"HTTP {response.status}", response.status in (429, 500, 502, 503, 504)

    body = response.body()
    ctype = response.headers.get("content-type", "").lower()
    if "pdf" not in ctype and not body.startswith(b"%PDF"):
        return False, f"not a PDF (content-type={ctype or 'unknown'})", False

    name = filename_from_headers_or_url(response, link, stem)
    target = output_dir / name
    if skip_existing and target.exists():
        return True, f"skipped existing {target.name}", False

    if target.exists():
        i = 2
        while True:
            alt = output_dir / f"{target.stem}_{i}{target.suffix}"
            if not alt.exists():
                target = alt
                break
            i += 1

    target.write_bytes(body)
    return True, f"saved {target.name} ({target.stat().st_size / 1024:.1f} KB)", False


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir)
    profile_dir = Path(args.profile_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=args.headless,
            accept_downloads=True,
            viewport={"width": 1400, "height": 1000},
        )

        page = context.new_page()
        print(f"Opening: {args.archive_url}")
        page.goto(args.archive_url, wait_until="domcontentloaded")

        wait_for_user_ready()
        time.sleep(1)

        state = get_archive_state(page)
        row_count = int(state.get("rowCount", 0))
        token_id = state.get("credentials", {}).get("tokenId", "")
        window_id = state.get("credentials", {}).get("windowId", "")

        if row_count <= 0:
            print("No rows found. Confirm you are on the Flatex document archive (classic view).")
            context.close()
            return 1

        if not token_id or not window_id:
            print("Could not extract Flatex token/window credentials from page context.")
            print("Make sure the archive page is fully loaded and you are logged in.")
            context.close()
            return 1

        start_row = max(1, args.start_row)
        end_row = row_count if args.end_row <= 0 else min(args.end_row, row_count)
        if start_row > end_row:
            print(f"Invalid range: start-row ({start_row}) > end-row ({end_row})")
            context.close()
            return 1

        print(f"Found {row_count} rows. Processing rows {start_row}..{end_row}...")

        success = 0
        failed = 0
        total = end_row - start_row + 1

        for row_index in range(start_row - 1, end_row):
            row_no = row_index + 1
            link = ""
            error = ""
            row_ok = False
            row_msg = ""
            last_link = ""

            for attempt in range(1, args.retries + 1):
                try:
                    # Re-resolve link on every attempt: old links can expire.
                    link = get_row_pdf_link(page, state, row_index)
                except Exception as exc:
                    error = str(exc)
                    if attempt < args.retries:
                        time.sleep(2)
                    continue

                last_link = link
                ok, msg, retriable = save_pdf_from_link(
                    context,
                    page,
                    link,
                    output_dir,
                    args.timeout,
                    args.skip_existing,
                )
                if ok:
                    row_ok = True
                    row_msg = msg
                    break

                row_msg = msg
                if retriable and attempt < args.retries:
                    time.sleep(2 * attempt)
                    continue
                break

            if not link:
                failed += 1
                print(f"[{row_no}/{end_row}] FAIL: could not resolve PDF link ({error})")
                continue

            if row_ok:
                success += 1
                print(f"[{row_no}/{end_row}] OK: {row_msg}")
            else:
                failed += 1
                print(f"[{row_no}/{end_row}] FAIL: {row_msg} :: {last_link or link}")

        print(f"\nDone. Downloaded: {success}, failed: {failed}, processed: {total}, output: {output_dir.resolve()}")
        context.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
