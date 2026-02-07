# flatex-pdf-downloader

[![CI](https://github.com/Rogger/flatex-pdf-downloader/actions/workflows/ci.yml/badge.svg)](https://github.com/Rogger/flatex-pdf-downloader/actions/workflows/ci.yml)

Python script that mirrors the Flatex Downloader extension flow for the classic document archive.

## How it works

For each visible archive row, the script:
1. Extracts `tokenId` and `windowId` from page context (`window.webcore`)
2. Reads current archive filter form values
3. Sends Flatex AJAX POST with `documentArchiveListTable.selectedrowidx`
4. Parses returned command script (`finished(...)` / `display(...)`) for PDF URL
5. Downloads PDF with logged-in browser session
6. If PDF fetch returns `503`, opens hidden iframe warm-up and retries

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install playwright
python -m playwright install chromium
```

## Run

```bash
python flatex_pdf_downloader.py \
  --archive-url "https://konto.flatex.at/" \
  --output-dir downloads \
  --skip-existing
```

Then in browser:
1. Log in
2. Open Flatex document archive (classic view)
3. Apply filters
4. Scroll/load all rows you want
5. Press Enter in terminal

## Notes

- The script processes rows sequentially (same as extension behavior).
- Use `--profile-dir` to persist login session across runs.
- Use `--headless` once session/profile is stable.
- Resume partial runs with `--start-row` and `--end-row`.

Example (retry only a failed slice):

```bash
python flatex_pdf_downloader.py \
  --archive-url "https://konto.flatex.at/" \
  --output-dir downloads \
  --start-row 190 \
  --end-row 340 \
  --retries 5 \
  --skip-existing
```

## Disclaimer

Use at your own risk. This project is provided "as is", without warranty of any kind.

## License

MIT. See `LICENSE`.

## Testing

```bash
source .venv/bin/activate
python -m pytest -q
```
