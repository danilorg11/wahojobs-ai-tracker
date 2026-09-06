from dataclasses import replace
import json
import unittest
from tests import test_profile_intake_final_save as support
from wahojobs.profile_intake.browser import _review_page
from wahojobs.profile_intake.runtime import (
    hydrate_profile_intake_checkpoint, serialize_profile_intake_checkpoint,
)


class _NoNameAdapter(support._FinalSaveAdapter):
    def extract(self, evidence):
        result = super().extract(evidence)
        return replace(result, facts=tuple(
            fact for fact in result.facts if fact.field_path != "identity.display_name"
        ))


class _UnpairedYearAdapter(_NoNameAdapter):
    def extract(self, evidence):
        self.include_education = True
        result = super().extract(evidence)
        return replace(result, facts=tuple(
            fact for fact in result.facts
            if not fact.field_path.startswith("education.")
            or fact.field_path == "education.graduation_years"
        ))


class ProfileIntakeDisplayNameTests(unittest.TestCase):
    def setUp(self):
        self.case = support.ProfileIntakeFinalSaveTests()
        self.case.setUp()

    def tearDown(self):
        self.case.tearDown()

    def upload_without_name(self):
        c = self.case
        c.integration.close()
        c.integration = c._build(_NoNameAdapter(c.path, (c.read_provider, c.write_provider)))
        return c._reference(c._upload())

    def body(self, reference, name, **kwargs):
        c = self.case
        return c._review_body(reference, action="save",
                              preference_overrides={"missing_display_name": name}, **kwargs)

    def test_missing_name_error_preserves_review_and_entitlement(self):
        reference = self.upload_without_name()
        c = self.case
        original = c.integration._processing.vault.get(reference, c._grant())
        skill = next(i for i, f in enumerate(original.review.facts) if f.field_path == "skills.normalized")
        response = c._post_review(reference, self.body(reference, "   ", changes={skill: "Edited skill"}))
        self.assertEqual(response.status, 422)
        self.assertIn(b"Enter a display name of 1", response.body)
        self.assertIn(b"data-focus-display-name", response.body)
        self.assertIn(b"Edited skill", response.body)
        self.assertIn(b"doesn", response.body)
        with c._database() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM product_profiles").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT state FROM ai_profile_import_entitlements").fetchone()[0], "reserved")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ai_profile_intake_checkpoints").fetchone()[0], 1)

    def test_user_entered_name_saves_exactly_once_with_confirmation_provenance(self):
        reference = self.upload_without_name()
        c = self.case
        original = c.integration._processing.vault.get(reference, c._grant())
        body = self.body(reference, "Alex Test")
        self.assertEqual(c._post_review(reference, body).status, 303)
        self.assertEqual(c._post_review(reference, body).status, 303)
        with c._database() as db:
            rows = db.execute("SELECT structured_profile_json FROM product_profile_revisions").fetchall()
            self.assertEqual(len(rows), 1)
            profile = json.loads(rows[0][0])
            self.assertEqual(profile["identity"]["display_name"], "Alex Test")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM product_profiles").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT state FROM ai_profile_import_entitlements").fetchone()[0], "consumed")
            self.assertIn("user_confirmation", json.dumps(profile))
            # V2 omits identity field-source rows; the name is explicitly
            # entered in the review, never added to the extraction facts.
            self.assertFalse(any(f.field_path == "identity.display_name" for f in original.review.facts))

    def test_unpaired_numeric_graduation_year_survives_finalization(self):
        c = self.case
        c.integration.close()
        c.integration = c._build(_UnpairedYearAdapter(c.path, (c.read_provider, c.write_provider)))
        reference = c._reference(c._upload())
        self.assertEqual(c._post_review(reference, self.body(reference, "Alex Test")).status, 303)
        with c._database() as db:
            profile = json.loads(db.execute("SELECT structured_profile_json FROM product_profile_revisions").fetchone()[0])
            self.assertEqual(profile["education"]["graduation_years"], [2016])

    def test_unsafe_name_has_field_error_and_keeps_other_edits(self):
        reference = self.upload_without_name()
        c = self.case
        snapshot = c.integration._processing.vault.get(reference, c._grant())
        skill = next(i for i, f in enumerate(snapshot.review.facts) if f.field_path == "skills.normalized")
        for value in ("person@example.com", "a\x00b", "a" * 1000):
            with self.subTest(value_length=len(value)):
                response = c._post_review(reference, self.body(reference, value, changes={skill: "Edited skill"}))
                self.assertEqual(response.status, 422)
                self.assertIn(b"display-name-error", response.body)
                self.assertIn(b"Edited skill", response.body)
                self.assertNotIn(b"person@example.com", response.body)
        with c._database() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM product_profiles").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT state FROM ai_profile_import_entitlements").fetchone()[0], "reserved")

    def test_invalid_name_autosave_preserves_other_edits_with_field_feedback(self):
        reference = self.upload_without_name()
        c = self.case
        snapshot = c.integration._processing.vault.get(reference, c._grant())
        skill = next(i for i, f in enumerate(snapshot.review.facts) if f.field_path == "skills.normalized")
        response = c._post_review(reference, c._review_body(
            reference, action="autosave", changes={skill: "Edited skill"},
            preference_overrides={"missing_display_name": "person@example.com"},
        ))
        self.assertEqual(response.status, 204)
        self.assertEqual(dict(response.headers)["X-Wahojobs-Review-Invalid-Field"], "missing_display_name")
        saved = c.integration._processing.vault.get(reference, c._grant())
        self.assertEqual(saved.review.facts[skill].value, "Edited skill")
        self.assertEqual(dict(saved.review.user_inputs)["display_name"], "")
        with c._database() as db:
            payload = db.execute("SELECT review_payload_json FROM ai_profile_intake_checkpoints").fetchone()[0]
            self.assertNotIn("person@example.com", payload)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM product_profiles").fetchone()[0], 0)

    def test_removed_direct_fact_is_not_reintroduced_by_rendering(self):
        c = self.case
        reference = c._reference(c._upload())
        snapshot = c.integration._processing.vault.get(reference, c._grant())
        index = next(i for i, f in enumerate(snapshot.review.facts) if f.field_path == "location.city")
        facts = tuple(replace(f, decision="remove") if i == index else f for i, f in enumerate(snapshot.review.facts))
        page = _review_page(reference, replace(snapshot, review=replace(snapshot.review, facts=facts)), c.session["csrf_secret"], save_enabled=True)
        from html.parser import HTMLParser
        class Inputs(HTMLParser):
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == "input" and attrs.get("name") == f"fact_{index}_value":
                    self.value = attrs.get("value")
        parser = Inputs()
        parser.feed(page)
        self.assertEqual(parser.value, "")

    def test_overlong_name_does_not_create_partial_profile(self):
        reference = self.upload_without_name()
        response = self.case._post_review(reference, self.body(reference, "a" * 161))
        self.assertEqual(response.status, 422)
        self.assertIn(b"display-name-error", response.body)
        with self.case._database() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM product_profile_revisions").fetchone()[0], 0)

    def test_extracted_name_remains_editable_and_cannot_be_cleared_at_save(self):
        c = self.case
        reference = c._reference(c._upload())
        snapshot = c.integration._processing.vault.get(reference, c._grant())
        index = next(i for i, f in enumerate(snapshot.review.facts) if f.field_path == "identity.display_name")
        response = c._post_review(reference, c._review_body(reference, action="save", changes={index: ""}))
        self.assertEqual(response.status, 422)
        self.assertNotIn(b"name='missing_display_name'", response.body)
        response = c._post_review(reference, c._review_body(reference, action="save", changes={index: "Morgan Test"}))
        self.assertEqual(response.status, 303)

    def test_legacy_checkpoint_without_name_is_rendered_with_recovery_control(self):
        reference = self.upload_without_name()
        c = self.case
        snapshot = c.integration._processing.vault.get(reference, c._grant())
        old = replace(snapshot.review,
                      missing_user_fields=tuple(n for n in snapshot.review.missing_user_fields if n != "display_name"),
                      user_inputs=tuple((n, v) for n, v in snapshot.review.user_inputs if n != "display_name"))
        payload = serialize_profile_intake_checkpoint(old)
        hydrated = hydrate_profile_intake_checkpoint(payload)
        self.assertEqual(serialize_profile_intake_checkpoint(hydrated), payload)
        page = _review_page(reference, replace(snapshot, review=hydrated), c.session["csrf_secret"], save_enabled=True)
        self.assertIn("name='missing_display_name'", page)
        self.assertNotIn("Alex Test", page)

    def test_wrong_owner_cannot_save_name(self):
        reference = self.upload_without_name()
        c = self.case
        other = dict(c.session, session_token="z" * 43)
        response = c._post_review(reference, self.body(reference, "Alex Test"), session=other)
        self.assertNotEqual(response.status, 303)
        with c._database() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM product_profiles").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
