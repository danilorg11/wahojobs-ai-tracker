from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

from scripts.render_public_job_preview_routes import (
    render_caddy_configuration,
    render_vercel_configuration,
)
from wahojobs import public_job_identity
from wahojobs.public_job_release import (
    KARL_CANONICAL_KEY,
    KARL_PUBLIC_JOB_ID,
    KARL_PUBLIC_JOB_PATH,
    PublicJobReleaseError,
    build_preview_release,
    load_preview_binding_publications,
    load_preview_registry_artifact,
    load_preview_release_manifest,
    validate_preview_release_manifest,
)


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "deploy/public-job-preview/registry.json"
BINDINGS = ROOT / "deploy/public-job-preview/bindings.json"
MANIFEST = ROOT / "deploy/vercel-preview-gateway/release-manifest.json"
VERCEL = ROOT / "deploy/vercel-preview-gateway/vercel.json"
CADDY = ROOT / "deploy/digitalocean-preview/Caddyfile"


def _canonical_release(document: dict) -> dict:
    payload = {
        "database_sha256": document["database_sha256"],
        "format": document["format"],
        "published_details": sorted(
            document["published_details"], key=lambda item: item["path"]
        ),
        "registry_sha256": document["registry_sha256"],
    }
    encoded = (
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("ascii")
    result = deepcopy(document)
    result["published_details"] = payload["published_details"]
    result["release_id"] = sha256(encoded).hexdigest()
    return result


class PublicJobPreviewReleaseTests(unittest.TestCase):
    def test_checked_in_release_is_derived_from_exact_registry_and_bindings(self):
        registry = load_preview_registry_artifact(REGISTRY)
        bindings = load_preview_binding_publications(BINDINGS)
        release = load_preview_release_manifest(MANIFEST)
        rebuilt = build_preview_release(
            database_sha256=release.database_sha256,
            registry_artifact=registry,
            bindings=bindings,
        )
        self.assertEqual(rebuilt, release)
        self.assertEqual(
            registry.sha256,
            "59d0295a7edcfa7b41767abb97b84e560e77b60c1da355d55c52bc1ee1fdbccb",
        )
        self.assertEqual(len(release.published_details), 2)
        karl = next(
            item for item in release.published_details if item.path == KARL_PUBLIC_JOB_PATH
        )
        self.assertEqual(karl.public_job_id, KARL_PUBLIC_JOB_ID)
        self.assertEqual(karl.canonical_key, KARL_CANONICAL_KEY)
        canary = next(
            item for item in release.published_details if item.path != KARL_PUBLIC_JOB_PATH
        )
        self.assertTrue(canary.path.endswith("-" + canary.public_job_id))
        public_job_identity.validate_new_public_job_path(canary.path)

    def test_both_boundaries_are_generated_from_that_same_manifest(self):
        release = load_preview_release_manifest(MANIFEST)
        self.assertEqual(
            VERCEL.read_text(encoding="utf-8"),
            render_vercel_configuration(release),
        )
        self.assertEqual(
            CADDY.read_text(encoding="utf-8"),
            render_caddy_configuration(release),
        )
        vercel = json.loads(VERCEL.read_text(encoding="utf-8"))
        detail_rewrites = [
            item for item in vercel["rewrites"] if item["destination"] == "/api/job"
        ]
        self.assertEqual(
            [item["source"] for item in detail_rewrites], list(release.paths)
        )
        self.assertFalse(
            any(
                "*" in item["source"] or ":" in item["source"]
                for item in detail_rewrites
            )
        )

    def test_manifest_identity_changes_if_projection_digest_changes(self):
        registry = load_preview_registry_artifact(REGISTRY)
        bindings = load_preview_binding_publications(BINDINGS)
        first = build_preview_release(
            database_sha256="1" * 64,
            registry_artifact=registry,
            bindings=bindings,
        )
        second = build_preview_release(
            database_sha256="2" * 64,
            registry_artifact=registry,
            bindings=bindings,
        )
        self.assertNotEqual(first.release_id, second.release_id)

    def test_registry_rejects_aliases_extra_paths_and_path_id_mismatch(self):
        original = json.loads(REGISTRY.read_text(encoding="ascii"))
        variants = []

        alias = deepcopy(original)
        alias_path = deepcopy(alias["paths"][0])
        alias_path["path"] += "-alias"
        alias_path["normalized_path"] += "-alias"
        alias_path["path_role"] = "alias"
        alias["paths"].append(alias_path)
        variants.append(alias)

        mismatch = deepcopy(original)
        canary = next(
            item for item in mismatch["paths"] if item["path"] != KARL_PUBLIC_JOB_PATH
        )
        canary["public_job_id"] = KARL_PUBLIC_JOB_ID
        variants.append(mismatch)

        with tempfile.TemporaryDirectory(prefix="wahojobs-release-invalid-") as name:
            root = Path(name)
            for index, document in enumerate(variants):
                path = root / f"registry-{index}.json"
                path.write_text(
                    json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="ascii",
                )
                with self.subTest(index=index):
                    with self.assertRaises(PublicJobReleaseError):
                        load_preview_registry_artifact(path)

    def test_manifest_rejects_karl_rebinding_even_with_recomputed_digest(self):
        document = json.loads(MANIFEST.read_text(encoding="ascii"))
        karl = next(
            item
            for item in document["published_details"]
            if item["path"] == KARL_PUBLIC_JOB_PATH
        )
        karl["canonical_key"] = "other::identity"
        with self.assertRaises(PublicJobReleaseError):
            validate_preview_release_manifest(_canonical_release(document))


if __name__ == "__main__":
    unittest.main()
