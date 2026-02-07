# AGENTS

## Pre-Publish Checklist

1. Never commit local artifacts.
- Exclude `downloads/`, `.playwright-profile/`, `.venv/`, and caches.

2. Run mandatory checks before pushing.
- `python -m pytest -q`
- `python -m py_compile flatex_pdf_downloader.py`

3. Run a secret/PII scan on tracked files.
- Do not commit tokens, credentials, session data, or account PDFs.

4. Verify README repository metadata.
- Replace badge placeholder owner/repo values before first public push.

5. Keep security-related changes in separate commits.
- Prefer distinct commits for license, CI, and security hardening.
