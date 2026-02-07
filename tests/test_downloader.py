from __future__ import annotations

from pathlib import Path

import pytest

import flatex_pdf_downloader as cli
import src.download as dl
import src.parse_utils as pu


class FakeResponse:
    def __init__(self, *, status: int = 200, headers: dict[str, str] | None = None, body: bytes = b"%PDF-1.7"):
        self.status = status
        self.headers = headers or {"content-type": "application/pdf"}
        self._body = body

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def body(self) -> bytes:
        return self._body


class Dummy:
    pass


def test_extract_pdf_link_finished_pattern():
    script = 'foo finished("/downloadData/123/file.pdf", "x") bar'
    link = pu.extract_pdf_link_from_script(script, "https://konto.flatex.at/archive")
    assert link == "https://konto.flatex.at/downloadData/123/file.pdf"


def test_extract_pdf_link_display_pattern_with_escaped_ampersand():
    script = 'display("\\/downloadData\\/123\\/file.pdf?x=1\\u0026y=2", "x")'
    link = pu.extract_pdf_link_from_script(script, "https://konto.flatex.at/archive")
    assert link == "https://konto.flatex.at/downloadData/123/file.pdf?x=1&y=2"


def test_extract_pdf_link_invalid_raises():
    with pytest.raises(RuntimeError):
        pu.extract_pdf_link_from_script("nope", "https://konto.flatex.at/")


def test_filename_from_url_and_sanitize():
    url = "https://konto.flatex.at/download?filename=My%20File%20(1).pdf"
    assert pu.filename_from_url(url, "fallback") == "My_File_1.pdf"


def test_build_stable_stem_prefers_id_param():
    url = "https://konto.flatex.at/x?documentId=abc-123"
    assert pu.build_stable_stem(url) == "flatex_abc-123"


def test_is_allowed_download_url():
    assert pu.is_allowed_download_url("https://konto.flatex.at/downloadData/1/a.pdf") is True
    assert pu.is_allowed_download_url("https://konto.flatex.de/downloadData/1/a.pdf") is True
    assert pu.is_allowed_download_url("https://evil.example/a.pdf") is False


def test_save_pdf_from_link_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    context = Dummy()
    page = Dummy()

    def fake_fetch(_context, _link, _timeout):
        return FakeResponse(
            headers={
                "content-type": "application/pdf",
                "content-disposition": 'attachment; filename="foo.pdf"',
            },
            body=b"%PDF-test",
        )

    monkeypatch.setattr(dl, "fetch_pdf_response", fake_fetch)

    ok, msg, retriable = dl.save_pdf_from_link(
        context,
        page,
        "https://konto.flatex.at/downloadData/1/foo.pdf",
        tmp_path,
        10,
        skip_existing=False,
    )

    assert ok is True
    assert retriable is False
    assert "saved foo.pdf" in msg
    assert (tmp_path / "foo.pdf").exists()


def test_save_pdf_from_link_503_warmup_then_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    context = Dummy()
    page = Dummy()
    calls = {"n": 0, "warm": 0}

    def fake_fetch(_context, _link, _timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResponse(status=503, headers={"content-type": "text/plain"}, body=b"busy")
        return FakeResponse(
            headers={
                "content-type": "application/pdf",
                "content-disposition": 'attachment; filename="ok.pdf"',
            },
            body=b"%PDF-ok",
        )

    def fake_warmup(_page, _link):
        calls["warm"] += 1

    monkeypatch.setattr(dl, "fetch_pdf_response", fake_fetch)
    monkeypatch.setattr(dl, "warmup_pdf_link", fake_warmup)

    ok, msg, retriable = dl.save_pdf_from_link(
        context,
        page,
        "https://konto.flatex.at/downloadData/1/ok.pdf",
        tmp_path,
        10,
        skip_existing=False,
    )

    assert ok is True
    assert retriable is False
    assert calls["warm"] == 1
    assert "saved ok.pdf" in msg


def test_save_pdf_from_link_503_warmup_failure_is_retriable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    context = Dummy()
    page = Dummy()

    def fake_fetch(_context, _link, _timeout):
        return FakeResponse(status=503, headers={"content-type": "text/plain"}, body=b"busy")

    def fake_warmup(_page, _link):
        raise RuntimeError("iframe-timeout")

    monkeypatch.setattr(dl, "fetch_pdf_response", fake_fetch)
    monkeypatch.setattr(dl, "warmup_pdf_link", fake_warmup)

    ok, msg, retriable = dl.save_pdf_from_link(
        context,
        page,
        "https://konto.flatex.at/downloadData/1/slow.pdf",
        tmp_path,
        10,
        skip_existing=False,
    )

    assert ok is False
    assert retriable is True
    assert "503 warm-up failed" in msg


def test_save_pdf_from_link_skip_existing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    context = Dummy()
    page = Dummy()

    existing = tmp_path / "already.pdf"
    existing.write_bytes(b"%PDF-existing")

    def fake_fetch(_context, _link, _timeout):
        raise AssertionError("fetch should not be called when skip-existing matches")

    monkeypatch.setattr(dl, "fetch_pdf_response", fake_fetch)

    ok, msg, retriable = dl.save_pdf_from_link(
        context,
        page,
        "https://konto.flatex.at/downloadData/1/already.pdf",
        tmp_path,
        10,
        skip_existing=True,
    )

    assert ok is True
    assert retriable is False
    assert msg == "skipped existing already.pdf"


def test_parse_args_range_and_skip_existing(monkeypatch: pytest.MonkeyPatch):
    argv = [
        "prog",
        "--archive-url",
        "https://konto.flatex.at/",
        "--start-row",
        "10",
        "--end-row",
        "20",
        "--skip-existing",
    ]
    monkeypatch.setattr("sys.argv", argv)
    args = cli.parse_args()

    assert args.archive_url == "https://konto.flatex.at/"
    assert args.start_row == 10
    assert args.end_row == 20
    assert args.skip_existing is True


def test_save_pdf_from_link_blocks_non_flatex_host(tmp_path: Path):
    context = Dummy()
    page = Dummy()

    ok, msg, retriable = dl.save_pdf_from_link(
        context,
        page,
        "https://evil.example/malware.pdf",
        tmp_path,
        10,
        skip_existing=False,
    )

    assert ok is False
    assert retriable is False
    assert msg == "blocked non-Flatex download host"
