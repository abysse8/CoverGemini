from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from coverai.explorer import is_noise_title, looks_like_login_wall
from coverai.storage import CoverAiStore, normalize_offer_url, offer_dedupe_hash


class NormalizeUrlTests(unittest.TestCase):
    def test_tracking_params_do_not_split_the_same_job(self) -> None:
        # Same LinkedIn posting, two search impressions -> one dedupe hash.
        a = "https://fr.linkedin.com/jobs/view/stage-embarque-1234?position=1&pageNum=0&refId=AAA&trackingId=BBB"
        b = "https://fr.linkedin.com/jobs/view/stage-embarque-1234?position=7&pageNum=2&refId=ZZZ&trackingId=YYY"
        self.assertEqual(normalize_offer_url(a), normalize_offer_url(b))
        self.assertEqual(offer_dedupe_hash(url=a), offer_dedupe_hash(url=b))

    def test_meaningful_params_are_kept(self) -> None:
        a = "https://www.apec.fr/candidat/detail-offre/x?numeroOffre=42"
        b = "https://www.apec.fr/candidat/detail-offre/x?numeroOffre=99"
        self.assertNotEqual(normalize_offer_url(a), normalize_offer_url(b))

    def test_scheme_and_trailing_slash_ignored(self) -> None:
        self.assertEqual(
            normalize_offer_url("http://Example.com/jobs/1/"),
            normalize_offer_url("https://example.com/jobs/1"),
        )


class NoiseTitleTests(unittest.TestCase):
    def test_nav_labels_are_noise(self) -> None:
        for label in ("Déconnexion", "Parcourir les offres", "N/A",
                      "conditions générales de diffusion des offres",
                      "Multiple opportunities (e.g., Alten, Netatmo)"):
            self.assertTrue(is_noise_title(label), label)

    def test_real_titles_pass(self) -> None:
        for label in ("Stage - Ingénieur en système embarqué",
                      "Embedded Software Engineer Intern"):
            self.assertFalse(is_noise_title(label), label)


class LoginWallTests(unittest.TestCase):
    def test_linkedin_guest_wall_detected(self) -> None:
        wall = ("Sign Up | LinkedIn LinkedIn respects your privacy ... "
                "Agree & Join LinkedIn ... Cookie Policy ... User Agreement")
        self.assertTrue(looks_like_login_wall(wall))

    def test_real_body_not_flagged(self) -> None:
        body = ("Nous recherchons un ingénieur systèmes embarqués pour développer "
                "du firmware C/C++ sur microcontrôleurs. Missions: ...")
        self.assertFalse(looks_like_login_wall(body))


class OffersCleanViewTests(unittest.TestCase):
    def test_view_hides_noise_and_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CoverAiStore(Path(tmp) / "t.db")
            with store.connect() as conn:
                for oid, status in (("off_ok", "ok"), ("off_noise", "noise"),
                                    ("off_dup", "duplicate"), ("off_thin", "thin_body")):
                    conn.execute(
                        "INSERT INTO offers (id, dedupe_hash, url, title, status, cleanup_status, "
                        "created_at, updated_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        (oid, oid, f"https://x/{oid}", oid, "new", status, "t", "t", "t"),
                    )
            with store.connect() as conn:
                visible = {r["id"] for r in conn.execute("SELECT id FROM offers_clean")}
            self.assertEqual(visible, {"off_ok", "off_thin"})


if __name__ == "__main__":
    unittest.main()
