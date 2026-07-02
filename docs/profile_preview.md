# Profile to Matches Preview

`scripts/profile_to_matches_preview.py` is a local demo flow for trying the
canonical profile normalizer against current tracked opportunities.

It runs:

1. raw background text
2. `BaselineHeuristicProfileNormalizer`
3. `canonical_profile_v1` validation
4. conversion to the existing matcher-compatible profile dict
5. current matcher scoring and product-section grouping

Example:

```bash
python -B scripts/profile_to_matches_preview.py \
  --input-text "I speak English and Spanish, no college degree, looking for remote beginner AI data tasks." \
  --input-style short_paragraph
```

Supported output formats:

```bash
python -B scripts/profile_to_matches_preview.py --input-file profile.txt --input-style resume_or_linkedin_style --format json
python -B scripts/profile_to_matches_preview.py --input-file profile.txt --input-style resume_or_linkedin_style --format html --out exports/profile_to_matches_preview.html
```

This is not a production resume or LinkedIn parser. The baseline normalizer is
deterministic and intentionally conservative: it extracts only obvious stated
facts and should not invent licenses, credentials, countries, years of
experience, or language proficiency.
