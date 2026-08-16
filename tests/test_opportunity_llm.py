import json
import unittest

from wahojobs.opportunity_llm import (
    MAX_OUTPUT_TOKENS,
    OPENAI_RESPONSES_URL,
    PROMPT_VERSION,
    REASONING_EFFORT,
    OpenAIEnrichmentError,
    OpenAIStructuredEnrichmentClient,
    structured_output_schema,
    system_prompt,
)


class FakeResponse:
    def __init__(self, data=None, *, status_code=200, json_error=None):
        self.data = data
        self.status_code = status_code
        self.json_error = json_error

    def json(self):
        if self.json_error is not None:
            raise self.json_error
        if self.data is not None:
            return self.data
        payload = {
            "role_family": {"value": None, "evidence": []},
            "professional_domains": [],
            "work_activities": [],
            "skills_required": [],
            "skills_preferred": [],
            "responsibilities": [],
            "candidate_profile": {"value": None, "evidence": []},
            "quick_take": {"value": None, "evidence": []},
            "caveats": [],
        }
        return {
            "id": "resp_fixture",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": json.dumps(payload)}
                    ],
                }
            ],
            "usage": {
                "input_tokens": 2_000,
                "output_tokens": 500,
                "total_tokens": 2_500,
            },
        }


class FakeSession:
    def __init__(self, response=None):
        self.calls = []
        self.response = response or FakeResponse()

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class OpportunityLLMTests(unittest.TestCase):
    def test_responses_api_uses_one_strict_schema_and_reports_usage(self):
        session = FakeSession()
        client = OpenAIStructuredEnrichmentClient(
            "test-key",
            model="gpt-5-mini",
            session=session,
        )
        result = client.enrich(
            {
                "canonical": {"canonical_title": "Python Reviewer"},
                "evidence_blocks": [
                    {
                        "evidence_block_id": "source_hash:fixture:body_paragraph:abc",
                        "source_ref": "source_hash:fixture:body",
                        "kind": "body_paragraph",
                        "label": "body paragraph 1",
                        "content": "Review Python code.",
                    }
                ],
            }
        )

        self.assertEqual(len(session.calls), 1)
        url, request = session.calls[0]
        self.assertEqual(url, OPENAI_RESPONSES_URL)
        self.assertEqual(request["json"]["model"], "gpt-5-mini")
        self.assertFalse(request["json"]["store"])
        self.assertEqual(MAX_OUTPUT_TOKENS, 8_000)
        self.assertEqual(request["json"]["max_output_tokens"], MAX_OUTPUT_TOKENS)
        self.assertEqual(REASONING_EFFORT, "low")
        self.assertEqual(
            request["json"]["reasoning"], {"effort": REASONING_EFFORT}
        )
        output_format = request["json"]["text"]["format"]
        self.assertEqual(output_format["type"], "json_schema")
        self.assertTrue(output_format["strict"])
        self.assertEqual(output_format["schema"], structured_output_schema())
        evidence_schema = output_format["schema"]["properties"][
            "responsibilities"
        ]["items"]["properties"]["evidence"]
        self.assertEqual(evidence_schema["items"], {"type": "string"})
        self.assertEqual(evidence_schema["minItems"], 1)
        self.assertEqual(client.prompt_version, PROMPT_VERSION)
        self.assertEqual(result.response_id, "resp_fixture")
        self.assertEqual(result.response_status, "completed")
        self.assertEqual(result.http_status, 200)
        self.assertEqual(result.total_tokens, 2_500)
        self.assertEqual(result.estimated_cost_usd, 0.0015)

    def test_quick_take_contract_is_plain_english_candidate_oriented_and_grounded(self):
        prompt = system_prompt()
        for requirement in (
            "two or three short, natural sentences",
            "what the person would do",
            "plain English",
            "concrete verbs",
            "unnecessary acronyms",
            "marketing language",
            "strictly grounded",
        ):
            self.assertIn(requirement, prompt)
        self.assertEqual(PROMPT_VERSION, "opportunity_semantic_v3")

    def assert_diagnostic(self, response, expected_category):
        client = OpenAIStructuredEnrichmentClient(
            "test-key",
            model="gpt-5-mini",
            session=FakeSession(response),
        )
        with self.assertRaises(OpenAIEnrichmentError) as raised:
            client.enrich({"canonical": {}, "sources": []})
        self.assertEqual(raised.exception.diagnostic["category"], expected_category)
        self.assertNotIn("test-key", json.dumps(raised.exception.diagnostic))
        return raised.exception

    def test_http_provider_error_is_sanitized_and_classified(self):
        error = self.assert_diagnostic(
            FakeResponse(
                {
                    "error": {
                        "type": "rate_limit_error",
                        "code": "rate_limit_exceeded",
                        "message": "Limit reached for test-key",
                    }
                },
                status_code=429,
            ),
            "http_provider_error",
        )
        self.assertEqual(error.diagnostic["http_status"], 429)
        self.assertEqual(error.diagnostic["provider_error_type"], "rate_limit_error")
        self.assertEqual(error.diagnostic["provider_error_code"], "rate_limit_exceeded")
        self.assertIn("[REDACTED]", error.diagnostic["provider_error_message"])

    def test_refusal_retains_metadata_without_refusal_text(self):
        error = self.assert_diagnostic(
            FakeResponse(
                {
                    "id": "resp_refusal",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {"type": "refusal", "refusal": "Sensitive refusal text"}
                            ],
                        }
                    ],
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 10,
                        "total_tokens": 110,
                    },
                }
            ),
            "refusal",
        )
        self.assertTrue(error.diagnostic["refusal"])
        self.assertNotIn("Sensitive refusal text", json.dumps(error.diagnostic))
        self.assertEqual(error.response_metadata.response_id, "resp_refusal")
        self.assertEqual(error.response_metadata.total_tokens, 110)

    def test_incomplete_response_retains_reason_and_usage(self):
        error = self.assert_diagnostic(
            FakeResponse(
                {
                    "id": "resp_incomplete",
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "output": [],
                    "usage": {
                        "input_tokens": 300,
                        "output_tokens": 2_500,
                        "total_tokens": 2_800,
                    },
                }
            ),
            "incomplete_response",
        )
        self.assertEqual(error.diagnostic["incomplete_reason"], "max_output_tokens")
        self.assertEqual(error.response_metadata.response_status, "incomplete")
        self.assertEqual(error.response_metadata.total_tokens, 2_800)

    def test_missing_output_is_distinct_from_invalid_json(self):
        missing = self.assert_diagnostic(
            FakeResponse({"id": "resp_missing", "status": "completed", "output": []}),
            "missing_output",
        )
        invalid = self.assert_diagnostic(
            FakeResponse(
                {
                    "id": "resp_invalid",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "{broken"}],
                        }
                    ],
                }
            ),
            "invalid_json",
        )
        self.assertEqual(missing.response_metadata.response_id, "resp_missing")
        self.assertEqual(invalid.response_metadata.response_id, "resp_invalid")

    def test_contract_excludes_factual_constraint_fields(self):
        properties = set(structured_output_schema()["properties"])
        self.assertEqual(
            properties,
            {
                "role_family",
                "professional_domains",
                "work_activities",
                "skills_required",
                "skills_preferred",
                "responsibilities",
                "candidate_profile",
                "quick_take",
                "caveats",
            },
        )
        serialized = json.dumps(structured_output_schema(), sort_keys=True)
        for forbidden in (
            "pay",
            "geographic_eligibility",
            "degree_requirements",
            "licenses",
            "hours",
            "employment_type",
        ):
            self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()
