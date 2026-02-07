#!/usr/bin/env python3
"""Batch PDF downloader for Flatex document archive (classic flow)."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

from src.download import save_pdf_from_link
from src.parse_utils import extract_pdf_link_from_script

ROW_SELECTOR = 'tr[onclick^="DocumentViewer.openPopupIfRequired"]'

logger = logging.getLogger("flatex-pdf-downloader")


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
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity",
    )
    parser.add_argument(
        "--report-file",
        default="run_report.json",
        help="JSON summary filename written into output-dir",
    )
    return parser.parse_args()


def configure_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level), format="%(asctime)s %(levelname)s %(message)s")


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

    try:
        return extract_pdf_link_from_script(execute["script"], state["pageUrl"])
    except RuntimeError as exc:
        raise FlatexError(str(exc)) from exc


def write_report(output_dir: Path, report_file: str, report: dict) -> None:
    path = output_dir / report_file
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    logger.info("Report written: %s", path)


def main() -> int:
    args = parse_args()
    configure_logging(args.log_level)

    output_dir = Path(args.output_dir)
    profile_dir = Path(args.profile_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.mkdir(parents=True, exist_ok=True)

    failures: list[dict[str, object]] = []

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=args.headless,
            accept_downloads=True,
            viewport={"width": 1400, "height": 1000},
        )

        page = context.new_page()
        logger.info("Opening: %s", args.archive_url)
        page.goto(args.archive_url, wait_until="domcontentloaded")

        wait_for_user_ready()
        time.sleep(1)

        state = get_archive_state(page)
        row_count = int(state.get("rowCount", 0))
        token_id = state.get("credentials", {}).get("tokenId", "")
        window_id = state.get("credentials", {}).get("windowId", "")

        if row_count <= 0:
            logger.error("No rows found. Confirm you are on Flatex document archive (classic view).")
            context.close()
            return 1

        if not token_id or not window_id:
            logger.error("Could not extract Flatex token/window credentials from page context.")
            context.close()
            return 1

        start_row = max(1, args.start_row)
        end_row = row_count if args.end_row <= 0 else min(args.end_row, row_count)
        if start_row > end_row:
            logger.error("Invalid range: start-row (%s) > end-row (%s)", start_row, end_row)
            context.close()
            return 1

        logger.info("Found %s rows. Processing rows %s..%s", row_count, start_row, end_row)

        success = 0
        failed = 0
        skipped = 0
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
                    link = get_row_pdf_link(page, state, row_index)
                except Exception as exc:
                    error = str(exc)
                    logger.warning("[%s/%s] link resolve failed attempt %s: %s", row_no, end_row, attempt, error)
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
                logger.warning("[%s/%s] attempt %s failed: %s", row_no, end_row, attempt, msg)
                if retriable and attempt < args.retries:
                    time.sleep(2 * attempt)
                    continue
                break

            if not link:
                failed += 1
                reason = f"could not resolve PDF link ({error})"
                logger.error("[%s/%s] FAIL: %s", row_no, end_row, reason)
                failures.append({"row": row_no, "reason": reason, "url": None})
                continue

            if row_ok:
                if row_msg.startswith("skipped existing"):
                    skipped += 1
                else:
                    success += 1
                logger.info("[%s/%s] OK: %s", row_no, end_row, row_msg)
            else:
                failed += 1
                url = last_link or link
                logger.error("[%s/%s] FAIL: %s :: %s", row_no, end_row, row_msg, url)
                failures.append({"row": row_no, "reason": row_msg, "url": url})

        logger.info(
            "Done. Downloaded: %s, skipped: %s, failed: %s, processed: %s, output: %s",
            success,
            skipped,
            failed,
            total,
            output_dir.resolve(),
        )
        context.close()

    report = {
        "total": total,
        "downloaded": success,
        "skipped": skipped,
        "failed": failed,
        "archive_url": args.archive_url,
        "output_dir": str(output_dir.resolve()),
        "failures": failures,
    }
    write_report(output_dir, args.report_file, report)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
