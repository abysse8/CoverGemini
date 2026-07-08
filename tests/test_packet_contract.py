"""The Helene<->Marie upload contract, made checkable.

validate_packet_for_upload is Helene's side of the CV / cover-letter seam: it tells Marie
whether the file-backed fields in a packet (curated CV, motivation PDF) will actually upload.
These tests pin the contract's rules so a producer change can't quietly break it. No browser.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from coverai.browser_apply import validate_packet_for_upload


def _packet(cv_value, artifacts, status="ready"):
    return {
        "offer_ref": "offer:test",
        "fields": [{"name": "cv_upload", "value": cv_value, "status": status}],
        "artifacts": artifacts,
    }


class UploadContractTests(unittest.TestCase):
    def test_resolvable_local_cv_has_no_problems(self):
        with tempfile.TemporaryDirectory() as td:
            cv = Path(td) / "curated_cv.pdf"
            cv.write_bytes(b"%PDF-1.4 curated\n")
            packet = _packet("artifact:cv1", [
                {"artifact_id": "cv1", "kind": "pdf", "storage_ref": cv.as_uri()},
            ])
            self.assertEqual(validate_packet_for_upload(packet), [])

    def test_missing_artifact_entry_is_flagged(self):
        packet = _packet("artifact:cv1", [])  # no matching artifacts[] entry
        problems = validate_packet_for_upload(packet)
        self.assertEqual([p["issue"] for p in problems], ["artifact_missing"])

    def test_remote_storage_ref_is_rejected(self):
        packet = _packet("artifact:cv1", [
            {"artifact_id": "cv1", "kind": "pdf", "storage_ref": "https://cdn.example/cv.pdf"},
        ])
        problems = validate_packet_for_upload(packet)
        self.assertEqual([p["issue"] for p in problems], ["storage_not_local_file"])

    def test_local_file_that_does_not_exist_is_flagged(self):
        packet = _packet("artifact:cv1", [
            {"artifact_id": "cv1", "kind": "pdf", "storage_ref": "file:///no/such/curated_cv.pdf"},
        ])
        problems = validate_packet_for_upload(packet)
        self.assertEqual([p["issue"] for p in problems], ["file_not_found"])

    def test_bare_path_instead_of_artifact_ref_is_flagged(self):
        packet = _packet("/home/j3/cv.pdf", [])  # must be 'artifact:<id>', not a raw path
        problems = validate_packet_for_upload(packet)
        self.assertEqual([p["issue"] for p in problems], ["value_not_artifact_ref"])

    def test_missing_or_unready_file_field_is_not_a_problem(self):
        # A CV that isn't ready yet is Helene's to skip, not a contract violation.
        self.assertEqual(validate_packet_for_upload(_packet("", [], status="missing")), [])
        self.assertEqual(validate_packet_for_upload({"fields": [], "artifacts": []}), [])


if __name__ == "__main__":
    unittest.main()
