from __future__ import annotations

import asyncio
import io
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException, UploadFile

from app.api import files_api
from app.db import workspace
from app.services import file_service


def _upload(name: str, data: bytes, uploader: str | None = None) -> dict:
    """Drive the async upload endpoint with an in-memory file, as the router does."""
    upload = UploadFile(filename=name, file=io.BytesIO(data))
    return asyncio.run(files_api.upload_file(file=upload, uploader=uploader))


class WorkspaceFilesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._stack = ExitStack()
        self._dir = Path(self._stack.enter_context(tempfile.TemporaryDirectory()))
        self._stack.enter_context(patch.object(file_service, "UPLOAD_DIR", self._dir / "uploads"))
        self._stack.enter_context(patch.object(workspace, "WORKSPACE_DB", self._dir / "workspace.db"))
        workspace.init_db()

    def tearDown(self) -> None:
        self._stack.close()

    def test_upload_list_download_delete_roundtrip(self) -> None:
        created = _upload("report.pdf", b"hello pdf", uploader="amy")
        self.assertEqual(created["filename"], "report.pdf")
        self.assertEqual(created["size"], len(b"hello pdf"))
        self.assertEqual(created["uploader"], "amy")

        listing = files_api.list_files()
        self.assertEqual(len(listing["files"]), 1)
        self.assertEqual(listing["stats"]["total"], 1)
        self.assertEqual(listing["stats"]["total_size"], len(b"hello pdf"))
        self.assertEqual(listing["stats"]["contributors"], 1)

        response = files_api.download_file(created["id"])
        self.assertEqual(Path(response.path).read_bytes(), b"hello pdf")
        self.assertEqual(response.filename, "report.pdf")

        result = files_api.delete_file(created["id"])
        self.assertTrue(result["ok"])
        self.assertEqual(files_api.list_files()["files"], [])
        self.assertFalse(Path(response.path).exists())

    def test_blank_uploader_is_stored_as_null(self) -> None:
        created = _upload("notes.txt", b"x", uploader="   ")
        self.assertIsNone(created["uploader"])

    def test_rejects_disallowed_extension(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            _upload("malware.exe", b"x")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(files_api.list_files()["files"], [])

    def test_size_cap_rejects_and_cleans_up(self) -> None:
        with patch.object(file_service, "MAX_UPLOAD_BYTES", 4):
            with self.assertRaises(HTTPException) as ctx:
                _upload("big.log", b"way too many bytes")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(files_api.list_files()["files"], [])
        # No orphaned file left on disk.
        self.assertEqual(list((self._dir / "uploads").glob("*")), [])

    def test_duplicate_names_get_unique_on_disk(self) -> None:
        first = _upload("dup.csv", b"a")
        second = _upload("dup.csv", b"bb")
        self.assertEqual(first["filename"], "dup.csv")
        self.assertEqual(second["filename"], "dup_1.csv")

    def test_aggregates_group_by_type_and_uploader(self) -> None:
        _upload("a.csv", b"123", uploader="amy")
        _upload("b.csv", b"45", uploader="amy")
        _upload("c.log", b"6", uploader="nelson")

        stats = files_api.list_files()["stats"]
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["contributors"], 2)

        by_type = {row["ext"]: row for row in stats["files_by_type"]}
        self.assertEqual(by_type["csv"]["count"], 2)
        self.assertEqual(by_type["csv"]["size"], 5)
        self.assertEqual(by_type["log"]["count"], 1)

        top = {row["uploader"]: row["count"] for row in stats["top_uploaders"]}
        self.assertEqual(top["amy"], 2)
        self.assertEqual(top["nelson"], 1)
        # uploads_per_day is zero-filled to 14 days, today carries all 3.
        self.assertEqual(len(stats["uploads_per_day"]), 14)
        self.assertEqual(stats["uploads_per_day"][-1]["count"], 3)

    def test_pagination_pages_newest_first_and_reports_total(self) -> None:
        for name in ("one.log", "two.log", "three.log"):
            _upload(name, b"x")

        page = files_api.list_files(limit=2, offset=0)
        self.assertEqual([f["filename"] for f in page["files"]], ["three.log", "two.log"])
        self.assertEqual(page["total"], 3)

        rest = files_api.list_files(limit=2, offset=2)
        self.assertEqual([f["filename"] for f in rest["files"]], ["one.log"])
        self.assertEqual(rest["total"], 3)

        # No limit keeps the legacy full-list behaviour.
        legacy = files_api.list_files()
        self.assertEqual(len(legacy["files"]), 3)
        self.assertEqual(legacy["total"], 3)

    def test_search_filters_by_filename_and_total_follows(self) -> None:
        _upload("alpha-report.pdf", b"a")
        _upload("beta-notes.txt", b"b")
        _upload("ALPHA-extra.log", b"c")

        hit = files_api.list_files(q="alpha")
        self.assertEqual(
            sorted(f["filename"] for f in hit["files"]),
            ["ALPHA-extra.log", "alpha-report.pdf"],  # case-insensitive
        )
        self.assertEqual(hit["total"], 2)
        # KPI aggregates stay workspace-wide even while searching.
        self.assertEqual(hit["stats"]["total"], 3)

        # q combines with pagination: total is the match count, page is capped.
        page = files_api.list_files(limit=1, offset=0, q="alpha")
        self.assertEqual(len(page["files"]), 1)
        self.assertEqual(page["total"], 2)

    def test_search_treats_like_wildcards_literally(self) -> None:
        # Underscores survive upload sanitisation; unescaped they'd match any char.
        _upload("a_b.log", b"c")
        _upload("axb.log", b"d")
        self.assertEqual(
            [f["filename"] for f in files_api.list_files(q="a_b")["files"]],
            ["a_b.log"],
        )

        # "%" can't come through upload (secure_filename rewrites it) but can sit
        # in rows inserted out-of-band; it must match literally, not as a wildcard.
        workspace.execute(
            "INSERT INTO files (filename, filepath, size, uploader) VALUES (?, ?, ?, ?)",
            ("100%.log", "/x/100%.log", 1, None),
        )
        self.assertEqual(
            [f["filename"] for f in files_api.list_files(q="100%")["files"]],
            ["100%.log"],
        )
        self.assertEqual(files_api.list_files(q="0%.l")["total"], 1)

    def test_sort_by_name_and_size(self) -> None:
        _upload("bravo.log", b"12345", uploader="amy")
        _upload("alpha.log", b"1", uploader=None)
        _upload("charlie.log", b"123", uploader="nelson")

        by_name = files_api.list_files(sort="name", order="asc")
        self.assertEqual(
            [f["filename"] for f in by_name["files"]],
            ["alpha.log", "bravo.log", "charlie.log"],
        )
        by_size = files_api.list_files(sort="size", order="desc")
        self.assertEqual(
            [f["size"] for f in by_size["files"]],
            [5, 3, 1],
        )
        # Anonymous uploads sort after named uploaders regardless of order.
        by_uploader = files_api.list_files(sort="uploader", order="asc")
        self.assertEqual(
            [f["uploader"] for f in by_uploader["files"]],
            ["amy", "nelson", None],
        )

    def test_preview_image_streams_with_image_content_type(self) -> None:
        created = _upload("shot.png", b"\x89PNG-not-really")
        response = files_api.preview_file(created["id"])
        self.assertEqual(response.media_type, "image/png")
        self.assertEqual(Path(response.path).read_bytes(), b"\x89PNG-not-really")

    def test_preview_text_returns_head_and_truncation_flag(self) -> None:
        small = _upload("short.log", b"hello")
        full = files_api.preview_file(small["id"])
        self.assertEqual(full.body, b"hello")
        self.assertEqual(full.headers["x-preview-truncated"], "0")

        with patch.object(files_api, "PREVIEW_TEXT_BYTES", 4):
            big = _upload("long.log", b"0123456789")
            cut = files_api.preview_file(big["id"])
        self.assertEqual(cut.body, b"0123")
        self.assertEqual(cut.headers["x-preview-truncated"], "1")

    def test_preview_unsupported_type_is_415(self) -> None:
        created = _upload("capture.pcap", b"pcap-bytes")
        with self.assertRaises(HTTPException) as ctx:
            files_api.preview_file(created["id"])
        self.assertEqual(ctx.exception.status_code, 415)

    def test_preview_missing_file_is_404(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            files_api.preview_file(999)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_preview_rejects_path_outside_upload_dir(self) -> None:
        file_id = workspace.execute(
            "INSERT INTO files (filename, filepath, size, uploader) VALUES (?, ?, ?, ?)",
            ("evil.log", "/etc/passwd", 1, None),
        )
        with self.assertRaises(HTTPException) as ctx:
            files_api.preview_file(file_id)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_download_missing_file_is_404(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            files_api.download_file(999)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_download_rejects_path_outside_upload_dir(self) -> None:
        # A row whose filepath escapes UPLOAD_DIR must not be served.
        file_id = workspace.execute(
            "INSERT INTO files (filename, filepath, size, uploader) VALUES (?, ?, ?, ?)",
            ("passwd", "/etc/passwd", 1, None),
        )
        with self.assertRaises(HTTPException) as ctx:
            files_api.download_file(file_id)
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
