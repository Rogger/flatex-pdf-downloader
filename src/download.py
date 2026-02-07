from __future__ import annotations

from pathlib import Path

from playwright.sync_api import BrowserContext, Page, Response

from src.parse_utils import (
    build_stable_stem,
    filename_from_headers_or_url,
    filename_from_url,
    is_allowed_download_url,
)


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
    if not is_allowed_download_url(link):
        return False, "blocked non-Flatex download host", False

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

