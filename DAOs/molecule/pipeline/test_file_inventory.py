"""Tests for file_inventory routing."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from file_inventory import classify_route, inventory_files


def test_classify_route_pdf():
    assert classify_route("report.pdf", "application/pdf") == "pdf"


def test_classify_route_video():
    assert classify_route("demo.mp4", "video/mp4") == "video"


def test_classify_route_skip_brand():
    assert classify_route("brand-guide.pdf") == "skip"


def test_inventory_files_from_manifest():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "study.pdf").write_bytes(b"%PDF-1.4")
        (root / "figure.png").write_bytes(b"\x89PNG\r\n")
        manifest = [
            {"fileName": "study.pdf", "path": "/study.pdf", "description": "Study"},
            {"fileName": "figure.png", "path": "/figure.png", "description": "Figure"},
        ]
        dataroom = {
            "files": [
                {"path": "/study.pdf", "contentType": "application/pdf"},
                {"path": "/figure.png", "contentType": "image/png"},
            ]
        }
        (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (root / "dataroom.json").write_text(json.dumps(dataroom), encoding="utf-8")

        entries = inventory_files(root)
        routes = {e["filename"]: e["route"] for e in entries}
        assert routes["study.pdf"] == "pdf"
        assert routes["figure.png"] == "image"
