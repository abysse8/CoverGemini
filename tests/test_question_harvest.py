"""unmapped_questions: the "questions to feed Marie" harvester (pure, no browser).

When a form has a REQUIRED control that maps to no known logical field, Helene can't answer it
from a packet -- Marie must. These tests pin the harvest: radio groups collapse to one question
with their choices, the legend (group_label) wins over the bare option label, and optional or
already-mapped controls are not surfaced.
"""

from __future__ import annotations

import unittest

from coverai.browser_apply import unmapped_questions


def _ctrl(**kw):
    base = {"tag": "input", "type": "text", "name": "", "id": "", "selector": "", "label": "",
            "group_label": "", "placeholder": "", "required": True, "options": [], "selector_index": 0}
    base.update(kw)
    if not base["selector"]:
        base["selector"] = base["id"] or base["name"] or base["label"] or "x"
    return base


def _scan(controls):
    return {"ats": "test", "controls": controls}


class QuestionHarvestTests(unittest.TestCase):
    def test_radio_group_collapses_to_one_question_using_the_legend(self):
        scan = _scan([
            _ctrl(type="radio", name="partners", label="Oui", group_label="Contacté par nos partenaires ?", id="p-yes"),
            _ctrl(type="radio", name="partners", label="Non", group_label="Contacté par nos partenaires ?", id="p-no"),
        ])
        qs = unmapped_questions(scan)
        self.assertEqual(len(qs), 1)                                  # two radios -> one question
        self.assertEqual(qs[0]["label"], "Contacté par nos partenaires ?")  # legend, not "Oui"/"Non"
        self.assertEqual(qs[0]["field_type"], "radio")
        self.assertEqual(sorted(qs[0]["options"]), ["Non", "Oui"])

    def test_radio_group_falls_back_to_name_when_no_legend(self):
        scan = _scan([
            _ctrl(type="radio", name="consent", label="Oui", id="c-yes"),
            _ctrl(type="radio", name="consent", label="Non", id="c-no"),
        ])
        qs = unmapped_questions(scan)
        self.assertEqual(len(qs), 1)
        self.assertEqual(qs[0]["label"], "consent")

    def test_mapped_and_optional_controls_are_not_surfaced(self):
        scan = _scan([
            _ctrl(type="email", name="email", id="email", label="E-mail", selector="input#email"),          # maps -> answered
            _ctrl(type="text", name="q1", id="q1", label="Combien d'années d'expérience ?", selector="input#q1"),  # surface
            _ctrl(type="text", name="opt", id="opt", label="Complément facultatif", required=False, selector="input#opt"),  # optional
        ])
        labels = [q["label"] for q in unmapped_questions(scan)]
        self.assertIn("Combien d'années d'expérience ?", labels)  # required + unmapped
        self.assertNotIn("E-mail", labels)                        # maps to the email field
        self.assertNotIn("Complément facultatif", labels)         # not required -> not a blocker


if __name__ == "__main__":
    unittest.main()
