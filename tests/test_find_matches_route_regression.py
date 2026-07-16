import http.client
import concurrent.futures
import re
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlencode, urlparse

import scripts.local_product_app as app
import scripts.profile_match_digest as matcher
from wahojobs.matching import fit_evidence, languages, specializations
from wahojobs.matching.opportunity_trust import STALE_SOURCE, TRUSTED, assess_opportunity_trust


product_state = app.product_state
preview = app.profile_preview


SOFTWARE_PROFILE = (
    "I live in Brazil and I am fluent in Portuguese and English. I am a software "
    "engineer with 8 years of professional experience. I have strong skills in "
    "Python, JavaScript, SQL, APIs, backend development, debugging, code review, "
    "software testing, and technical documentation. I have a bachelor's degree "
    "in Computer Science. I am interested in remote AI training, coding evaluation, "
    "code generation review, software engineering, technical annotation, and "
    "programming-related AI projects."
)

BIOLOGY_PROFILE = (
    "I live in Brazil and have a PhD in biology with research experience in "
    "microbiology, computational biology, scientific writing, and data analysis. "
    "I want remote AI evaluation and biology research work."
)

BEGINNER_PROFILE = (
    "I live in Brazil, speak English and Spanish, and want remote AI training, "
    "data annotation, search evaluation, and language review work. I do not have "
    "a college degree or a professional license."
)

PORTUGUESE_ENGLISH_BEGINNER_PROFILE = (
    "I live in Brazil. Portuguese is my native language and I am fluent in English. "
    "I have a generalist background and I am interested in remote AI training, data "
    "annotation, content evaluation, search evaluation, and language-data work. I do "
    "not have a university degree, specialized technical experience, scientific "
    "credentials, or professional certifications. I am looking for entry-level, "
    "flexible, non-phone work that does not require previous experience."
)


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        result = super().__exit__(exc_type, exc_value, traceback)
        self.close()
        return result


def make_row(
    job_id,
    title,
    *,
    category="Software Engineering",
    age_hours=1,
    now=None,
):
    now = now or datetime.now(timezone.utc)
    checked_at = (
        now - timedelta(hours=age_hours)
    ).replace(microsecond=0).isoformat()
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return {
        "job_id": job_id,
        "title": title,
        "canonical_title": title,
        "location": "Remote",
        "url": f"https://example.test/jobs/{job_id}/{slug}",
        "department": category,
        "expertise": category,
        "commitment": "Freelance",
        "source_category": category,
        "source": "Route Fixture",
        "source_slug": "route-fixture",
        "source_tier": "core",
        "inventory_model": "live_feed",
        "market_count_policy": "count_live",
        "opportunity_kind": "live_posting",
        "availability_basis": "api_feed",
        "include_in_live_market_estimate": 1,
        "canonical_opportunity_id": job_id,
        "job_is_active": True,
        "canonical_is_active": True,
        "job_last_seen_at": checked_at,
        "source_run_id": job_id,
        "source_run_started_at": checked_at,
        "latest_successful_source_run_at": checked_at,
        "source_run_qualifies": True,
        "language": None,
        "language_locale": None,
        "required_languages": None,
    }


def presentation_match(job_id=1, *, age_hours=80, now=None, title="Cached Software Engineer"):
    now = now or datetime.now(timezone.utc)
    checked_at = (now - timedelta(hours=age_hours)).isoformat()
    return {
        **make_row(job_id, title, age_hours=age_hours, now=now),
        "display_title": title,
        "score": 30,
        "preview_section": "best_matches",
        "effective_product_section": "best_matches",
        "eligible_for_personalized": True,
        "location_eligibility_status": "eligible",
        "affirmative_fit_status": "supported",
        "affirmative_fit_why": [
            "Your software engineering and backend experience align with this role."
        ],
        "primary_recommendation_eligible": False,
        "primary_admission_reasons": ["opportunity_trust_stale_source"],
        "actionability_cap_reasons": ["opportunity_trust_stale_source"],
        "opportunity_trust_status": STALE_SOURCE,
        "opportunity_trust_reasons": [],
        "opportunity_trust": {
            "status": STALE_SOURCE,
            "source_age_hours": age_hours,
            "job_is_active": True,
            "canonical_is_active": True,
            "selected_variant_id": None,
            "latest_successful_source_run_at": checked_at,
        },
    }


class FindMatchesRouteRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "route-test.sqlite"
        self.rows = []
        self.data_generation = 0
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self._initialize_database()
        self.patches = [
            mock.patch.object(app, "get_connection", self.connect),
            mock.patch.object(product_state, "get_connection", self.connect),
            mock.patch.object(app.demo, "get_connection", self.connect),
            mock.patch.object(preview, "load_preview_rows", side_effect=self.load_rows),
            mock.patch.object(
                app,
                "preview_data_signature",
                side_effect=lambda: ("route-fixture", self.data_generation),
            ),
            mock.patch.object(app, "current_utc_time", side_effect=lambda: self.now),
        ]
        for patch in self.patches:
            patch.start()
        app.build_cached_preview_context.cache_clear()
        app.build_cached_structured_preview_context.cache_clear()
        app.seed_local_product_profiles()
        self.registry = app.MatchRunRegistry(max_size=16)
        handler = app.make_handler(registry=self.registry, demo_mode=False)
        self.server = app.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        app.build_cached_preview_context.cache_clear()
        app.build_cached_structured_preview_context.cache_clear()
        for patch in reversed(self.patches):
            patch.stop()
        self.temp_dir.cleanup()

    def _initialize_database(self):
        root = Path(__file__).resolve().parents[1]
        schema = (root / "wahojobs" / "db" / "schema.sql").read_text(encoding="utf-8")
        migration = (
            root / "wahojobs" / "db" / "migrations" / "001_pipeline_state.sql"
        ).read_text(encoding="utf-8")
        conn = self.connect()
        try:
            conn.executescript(schema)
            conn.executescript(migration)
            conn.execute(
                "INSERT INTO wahojobs_schema_migrations (version) VALUES (?)",
                ("001_pipeline_state",),
            )
            conn.commit()
        finally:
            conn.close()

    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10, factory=ClosingConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def load_rows(self, use_overlay=True):
        return list(self.rows), {
            "enabled": bool(use_overlay),
            "path": "route-fixture",
            "records_loaded": 0,
            "rows_enriched": 0,
        }

    def set_rows(self, rows):
        self.rows = list(rows)
        self.data_generation += 1
        app.build_cached_preview_context.cache_clear()
        app.build_cached_structured_preview_context.cache_clear()

    def request(self, method, path, fields=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=30
        )
        body = urlencode(fields) if fields is not None else None
        headers = {"Content-Type": "application/x-www-form-urlencoded"} if body else {}
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        payload = response.read().decode("utf-8")
        result = response.status, dict(response.getheaders()), payload
        connection.close()
        return result

    def create_run(self, profile):
        status, headers, _ = self.request(
            "POST",
            "/find-matches",
            {"input_text": profile, "input_style": "long_paragraph"},
        )
        self.assertEqual(status, 303)
        location = headers["Location"]
        run_id = parse_qs(urlparse(location).query)["run"][0]
        pending = self.registry.get(run_id)
        self.assertFalse(pending.profile_confirmed)
        review_fields = app.profile_review_form_fields(
            pending.canonical_profile, run_id, pending.review_token
        )
        confirm_status, confirm_headers, confirm_body = self.request(
            "POST", "/find-matches", review_fields
        )
        self.assertEqual(confirm_status, 303, confirm_body)
        location = confirm_headers["Location"]
        get_status, _, html = self.request("GET", location)
        self.assertEqual(get_status, 200)
        return run_id, html

    @staticmethod
    def rendered_titles(html):
        return re.findall(r"<h3>([^<]+)</h3>", html)

    def test_software_route_renders_ten_technical_matches_above_finance(self):
        technical = [
            make_row(index, f"Software Engineer Python Backend {index:02d}")
            for index in range(1, 13)
        ]
        finance = [
            make_row(100 + index, f"Finance Analyst Python {index:02d}", category="Finance")
            for index in range(1, 4)
        ]
        self.set_rows(technical + finance)

        run_id, html = self.create_run(SOFTWARE_PROFILE)
        run = self.registry.get(run_id)
        expected = [
            match["display_title"]
            for match in app.build_browser_presentation_matches(run.recommendation_context)
        ]

        self.assertEqual(len(expected), 10)
        self.assertEqual(self.rendered_titles(html), expected)
        self.assertTrue(all("Software Engineer" in title for title in expected))
        self.assertNotIn("Finance Analyst", html)

    def test_biology_and_beginner_routes_keep_domain_and_language_safeguards(self):
        cases = (
            (
                BIOLOGY_PROFILE,
                [
                    make_row(201, "Computational Biology Specialist", category="Biology"),
                    make_row(202, "Microbiology Research Evaluator", category="Biology"),
                    make_row(203, "Biology AI Training Expert", category="Biology"),
                    make_row(204, "Generic Content Writer", category="Writing"),
                ],
                {"Computational Biology Specialist", "Microbiology Research Evaluator"},
                {"Generic Content Writer"},
            ),
            (
                BEGINNER_PROFILE,
                [
                    make_row(301, "English Language Data Contributor", category="Language"),
                    make_row(302, "Spanish Language Data Contributor", category="Language"),
                    make_row(303, "General Data Annotation Reviewer", category="Generalist"),
                    make_row(304, "Thai Language Specialist", category="Language"),
                ],
                {"English Language Data Contributor", "Spanish Language Data Contributor"},
                {"Thai Language Specialist"},
            ),
        )
        for profile, rows, expected, forbidden in cases:
            with self.subTest(profile=profile[:30]):
                self.set_rows(rows)
                _, html = self.create_run(profile)
                titles = set(self.rendered_titles(html))
                self.assertTrue(expected <= titles)
                self.assertFalse(forbidden & titles)

    def test_spanish_language_role_requires_declared_spanish(self):
        self.set_rows(
            [make_row(305, "Spanish Language Expert", category="Language", now=self.now)]
        )
        profiles = (
            (PORTUGUESE_ENGLISH_BEGINNER_PROFILE, False),
            (
                PORTUGUESE_ENGLISH_BEGINNER_PROFILE.replace(
                    "fluent in English", "fluent in English and Spanish"
                ),
                True,
            ),
            (
                PORTUGUESE_ENGLISH_BEGINNER_PROFILE.replace(
                    " and I am fluent in English", ""
                ),
                False,
            ),
        )
        for profile, expected_visible in profiles:
            with self.subTest(expected_visible=expected_visible, profile=profile[:70]):
                _, html = self.create_run(profile)
                self.assertEqual(
                    "Spanish Language Expert" in self.rendered_titles(html),
                    expected_visible,
                )

    def test_recent_stale_rows_are_bounded_and_never_claim_active_refresh(self):
        rows = [make_row(401, "Verified Software Engineer", age_hours=1)]
        rows.extend(
            make_row(410 + index, f"Cached Software Engineer {index:02d}", age_hours=80)
            for index in range(1, 13)
        )
        rows.append(make_row(499, "Expired Cache Software Engineer", age_hours=200))
        self.set_rows(rows)

        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network refresh attempted")):
            _, html = self.create_run(SOFTWARE_PROFILE)

        titles = self.rendered_titles(html)
        self.assertEqual(len(titles), 10)
        self.assertIn("Verified Software Engineer", titles)
        self.assertNotIn("Expired Cache Software Engineer", titles)
        self.assertIn("10 matches", html)
        self.assertNotIn("recently cached", html.lower())
        self.assertNotIn("source verification", html.lower())
        self.assertNotIn("check before applying", html.lower())

        self.set_rows(
            [
                make_row(row["job_id"], row["title"], age_hours=1)
                for row in rows[:-1]
            ]
        )
        _, refreshed_html = self.create_run(SOFTWARE_PROFILE)
        self.assertIn("10 matches", refreshed_html)
        self.assertNotIn("verified match", refreshed_html.lower())

    def test_second_identical_match_run_reuses_the_production_preview_cache(self):
        self.set_rows([make_row(501, "Software Engineer Python")])
        original = preview.build_preview_context_from_canonical
        with mock.patch.object(
            preview, "build_preview_context_from_canonical", wraps=original
        ) as build:
            first_run, first_html = self.create_run(SOFTWARE_PROFILE)
            second_run, second_html = self.create_run(SOFTWARE_PROFILE)

        self.assertNotEqual(first_run, second_run)
        self.assertEqual(build.call_count, 1)
        self.assertEqual(self.rendered_titles(first_html), self.rendered_titles(second_html))

    def test_warm_cache_reclassifies_at_trusted_and_fallback_boundaries(self):
        rows = [
            make_row(601, "Software Engineer Crosses Trusted Boundary", age_hours=71.9, now=self.now),
            make_row(602, "Software Engineer Remains Verified", age_hours=60, now=self.now),
            make_row(603, "Software Engineer Crosses Cached Boundary", age_hours=167.9, now=self.now),
            make_row(604, "Software Engineer Remains Cached", age_hours=150, now=self.now),
        ]
        self.set_rows(rows)
        original = preview.build_preview_context_from_canonical
        with mock.patch.object(
            preview, "build_preview_context_from_canonical", wraps=original
        ) as build:
            first_id, _ = self.create_run(SOFTWARE_PROFILE)
            first = app.build_browser_presentation_matches(
                self.registry.get(first_id).recommendation_context
            )
            self.now += timedelta(minutes=13)
            reload_status, _, reloaded_html = self.request(
                "GET", f"/find-matches?run={first_id}"
            )
            second_id, second_html = self.create_run(SOFTWARE_PROFILE)
            second = app.build_browser_presentation_matches(
                self.registry.get(second_id).recommendation_context
            )

        self.assertEqual(build.call_count, 1)
        self.assertEqual(reload_status, 200)
        self.assertNotIn("Software Engineer Crosses Cached Boundary", reloaded_html)
        self.assertIn("3 matches", reloaded_html)
        self.assertNotIn("recently cached", reloaded_html.lower())
        self.assertEqual(
            [match["display_title"] for match in first],
            [
                "Software Engineer Crosses Trusted Boundary",
                "Software Engineer Remains Verified",
                "Software Engineer Crosses Cached Boundary",
                "Software Engineer Remains Cached",
            ],
        )
        self.assertEqual(
            [match["display_title"] for match in second],
            [
                "Software Engineer Remains Verified",
                "Software Engineer Crosses Trusted Boundary",
                "Software Engineer Remains Cached",
            ],
        )
        self.assertEqual(
            [match["presentation_data_status"] for match in second],
            ["recently_verified", "recently_cached", "recently_cached"],
        )
        self.assertNotIn("Software Engineer Crosses Cached Boundary", second_html)

    def test_concurrent_match_runs_are_profile_isolated_and_deterministic(self):
        self.set_rows(
            [
                make_row(701, "Software Engineer Python Backend", now=self.now),
                make_row(702, "Computational Biology Specialist", category="Biology", now=self.now),
                make_row(703, "English Language Data Contributor", category="Language", now=self.now),
                make_row(704, "Spanish Language Data Contributor", category="Language", now=self.now),
            ]
        )
        profiles = [SOFTWARE_PROFILE, BIOLOGY_PROFILE, BEGINNER_PROFILE, SOFTWARE_PROFILE]
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(self.create_run, profiles))
        titles = [self.rendered_titles(html) for _, html in results]

        self.assertEqual(titles[0], titles[3])
        self.assertIn("Software Engineer Python Backend", titles[0])
        self.assertIn("Computational Biology Specialist", titles[1])
        self.assertIn("English Language Data Contributor", titles[2])
        self.assertNotEqual(titles[0], titles[1])


class FindMatchesAdmissionContractTests(unittest.TestCase):
    NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)

    def test_safe_job_url_contract(self):
        invalid = (
            None,
            "",
            "   ",
            "/jobs/1",
            "not-a-url",
            "javascript:alert(1)",
            "data:text/html,hello",
            "file:///tmp/job",
            "vbscript:msgbox(1)",
            "//example.test/jobs/1",
            "https:///jobs/1",
            "https://example.test/jobs/1\nnext",
            "https://exa mple.test/jobs/1",
        )
        for value in invalid:
            with self.subTest(value=value):
                self.assertIsNone(app.safe_job_url(value))
        for value in ("http://example.test/jobs/1", "https://example.test/jobs/1"):
            with self.subTest(value=value):
                self.assertEqual(app.safe_job_url(value), value)

    def test_fallback_requires_stable_identity_and_deduplicates_canonical_id(self):
        invalid = (None, "", " ", "abc", 0, -1, True)
        for value in invalid:
            match = presentation_match(now=self.NOW)
            match["canonical_opportunity_id"] = value
            if value in (None, ""):
                match["job_id"] = value
            with self.subTest(value=value):
                self.assertIn(
                    "invalid_stable_identity", app.browser_match_rejection_reasons(match)
                )
        job_only = presentation_match(now=self.NOW)
        job_only["canonical_opportunity_id"] = None
        self.assertEqual(app.stable_opportunity_identity(job_only), ("job", 1))

        duplicate = presentation_match(2, now=self.NOW, title="Duplicate variant")
        duplicate["canonical_opportunity_id"] = 1
        context = {"matches": {section: [] for section in preview.SECTION_ORDER}}
        context["matches"]["best_matches"] = [presentation_match(now=self.NOW), duplicate]
        rendered = app.build_browser_presentation_matches(context)
        self.assertEqual(len(rendered), 1)

    def test_invalid_rows_are_not_rendered_or_counted_as_verification_only(self):
        invalid_url = presentation_match(10, now=self.NOW, title="Unsafe URL")
        invalid_url["url"] = "javascript:alert(1)"
        missing_id = presentation_match(11, now=self.NOW, title="Missing identity")
        missing_id["canonical_opportunity_id"] = None
        missing_id["job_id"] = None
        context = {"matches": {section: [] for section in preview.SECTION_ORDER}}
        context["matches"]["best_matches"] = [invalid_url, missing_id]

        self.assertEqual(app.build_browser_presentation_matches(context), [])
        self.assertEqual(app.supported_candidates_needing_verification(context), 0)
        header = app.render_preview_results_header(context)
        self.assertIn("0 matches", header)
        self.assertNotIn("source verification", header)

    def test_exact_freshness_ttl_boundaries(self):
        cases = (
            (timedelta(hours=71, minutes=59, seconds=59, milliseconds=999), TRUSTED, True),
            (timedelta(hours=72), TRUSTED, True),
            (timedelta(hours=72, microseconds=1), STALE_SOURCE, True),
            (timedelta(hours=167, minutes=59, seconds=59, milliseconds=999), STALE_SOURCE, True),
            (timedelta(hours=168), STALE_SOURCE, True),
            (timedelta(hours=168, microseconds=1), STALE_SOURCE, False),
        )
        for age, expected_status, expected_fallback in cases:
            with self.subTest(age=age):
                match = presentation_match(age_hours=80, now=self.NOW)
                checked_at = self.NOW - age
                for field in (
                    "job_last_seen_at",
                    "source_run_started_at",
                    "latest_successful_source_run_at",
                ):
                    match[field] = checked_at.isoformat()
                refreshed = preview.refresh_match_freshness(match, self.NOW)
                self.assertEqual(refreshed["opportunity_trust_status"], expected_status)
                self.assertEqual(
                    app.recent_cached_match_is_usable(refreshed), expected_fallback and expected_status == STALE_SOURCE
                )

    def test_equivalent_timestamp_offsets_and_naive_utc_match(self):
        instant = self.NOW - timedelta(hours=72)
        representations = (
            instant.isoformat(),
            instant.astimezone(timezone(timedelta(hours=-3))).isoformat(),
            instant.astimezone(timezone(timedelta(hours=5, minutes=30))).isoformat(),
            instant.replace(tzinfo=None).isoformat(),
        )
        statuses = []
        for text in representations:
            row = make_row(20, "Offset Role", now=self.NOW)
            row.update(
                job_last_seen_at=text,
                source_run_started_at=text,
                latest_successful_source_run_at=text,
            )
            statuses.append(assess_opportunity_trust(row, "eligible", now=self.NOW).status)
        self.assertEqual(statuses, [TRUSTED] * len(representations))

    def test_verified_cards_precede_distinct_cached_cards_with_separate_copy(self):
        verified = presentation_match(30, age_hours=1, now=self.NOW, title="Verified Role")
        verified["opportunity_trust_status"] = TRUSTED
        verified["opportunity_trust"]["status"] = TRUSTED
        verified["primary_recommendation_eligible"] = True
        verified["actionability_cap_reasons"] = []
        verified["primary_admission_reasons"] = []
        cached = presentation_match(31, now=self.NOW, title="Cached Role")
        context = {"matches": {section: [] for section in preview.SECTION_ORDER}}
        context["matches"]["best_matches"] = [cached, verified]

        ranked = app.build_browser_presentation_matches(context)
        self.assertEqual(
            [match["presentation_data_status"] for match in ranked],
            ["recently_verified", "recently_cached"],
        )
        card = app.render_ranked_preview_card(
            ranked[1], app.demo.build_tracked_index([]), "run-test"
        )
        self.assertIn("Why it fits", card)
        self.assertIn("software engineering and backend experience", card)
        self.assertNotIn("Recently cached", card)
        self.assertNotIn("Source verification needed", card)
        self.assertNotIn("cached-source-card", card)
        self.assertNotIn("being refreshed", card)


class MatcherOptimizationEquivalenceTests(unittest.TestCase):
    def test_regex_fast_paths_match_reference_scans(self):
        language_samples = (
            "Portuguese (Brasil) and ENGLISH",
            "K'iche'-Spanish translation",
            "French / German evaluator",
            "Kiswahili\t audio specialist",
        )
        for sample in language_samples:
            normalized = languages.normalize_language_text(sample)
            optimized = languages.find_language_mentions_cached(normalized)
            mentions = []
            seen = set()
            for alias, language, pattern in languages._ALIAS_PATTERNS:
                for found in pattern.finditer(normalized):
                    key = (found.start(), found.end(), language)
                    if key in seen:
                        continue
                    seen.add(key)
                    mentions.append(
                        {
                            "language": language,
                            "alias": alias,
                            "start": found.start(),
                            "end": found.end(),
                        }
                    )
            mentions.sort(key=lambda item: (item["start"], -(item["end"] - item["start"])))
            reference = tuple(tuple(item.items()) for item in mentions)
            self.assertEqual(optimized, reference)

        role_samples = (
            "Senior BACK-END software engineer",
            "Biology / microbiology research evaluator",
            "Legal-IP review specialist",
            "Audio   annotation expert",
        )
        for sample in role_samples:
            normalized = fit_evidence.normalize_text(sample)
            candidates = []
            for concept in fit_evidence.ROLE_CONCEPTS:
                for alias in concept.aliases:
                    for found in fit_evidence.alias_pattern(alias).finditer(normalized):
                        candidates.append((found.start(), found.end(), concept.key))
            candidates.sort(key=lambda value: (value[0], -(value[1] - value[0]), value[2]))
            reference = []
            for candidate in candidates:
                if any(candidate[0] < item[1] and candidate[1] > item[0] for item in reference):
                    continue
                reference.append(candidate)
            self.assertEqual(fit_evidence._role_mentions(normalized), tuple(reference))

    def test_memoized_helpers_match_uncached_results(self):
        for sample in (
            "Python Engineer",
            "Portuguese-language reviewer",
            "BIOLOGY\tResearch",
            "French / English translation",
        ):
            self.assertEqual(matcher.normalize_text(sample), matcher.normalize_text.__wrapped__(sample))
            normalized = specializations._normalize_specialization_text(sample)
            self.assertEqual(
                specializations._non_overlapping_mentions(normalized),
                specializations._non_overlapping_mentions.__wrapped__(normalized),
            )


if __name__ == "__main__":
    unittest.main()
