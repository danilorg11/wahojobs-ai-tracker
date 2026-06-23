# Matching Label Review Summary

Generated: 2026-06-23T02:09:44.172510+00:00

This packet is for one-time benchmark calibration. It does not change runtime matching behavior.

- Cases prioritized: **30**
- Limit: **30**
- Profile filter: **none**
- HTML review file: `exports\matching_label_review.html`
- Editable CSV: `exports\matching_label_review.csv`

## Cases By Profile

- `beginner_bilingual_no_degree`: 8
- `phd_history_researcher`: 5
- `biology_or_medicine_academic`: 4
- `lawyer`: 3
- `generalist_no_degree`: 3
- `multilingual_translator`: 3
- `english_teacher_remote`: 2
- `portuguese_english_reviewer`: 1
- `software_engineer`: 1

## Cases By Review Reason

- `matcher_disagreement`: 25
- `sparse_metadata`: 21
- `priority_profile`: 21
- `top_10_impact`: 15
- `important_case`: 13
- `review_required`: 8
- `top_4_impact`: 8
- `visible_false_negative`: 7
- `ambiguous_weak_profile_label`: 4

## Key Buckets

- Visible false negatives: 7
- Review-required cases: 8
- Top-4 / top-10 impact cases: 23
- Sparse metadata cases: 21

## Human Review Instructions

1. Open the HTML file for context.
2. Edit only the human columns in the CSV.
3. Use labels: `strong`, `plausible`, `weak`, `false_positive`.
4. Use sections: `do_these_first`, `best_matches`, `also_worth_reviewing`, `explore_only`, `exclude`.
5. Leave a row blank if no decision is approved yet.

## Apply Instructions

Dry run first:

```bash
python scripts/matching_label_review.py apply --input exports/matching_label_review.csv --dry-run
```

Apply only after approved human decisions are present:

```bash
python scripts/matching_label_review.py apply --input exports/matching_label_review.csv --yes
python scripts/matching_quality_report.py
```
