# Opportunity Metadata Gap Review Summary

Review artifact only. Nothing has been applied to jobs, canonical opportunities, fixtures, or the database.

- Total candidates: **86**

## Counts by Source

- mercor: 18
- meridial: 18
- oneforma: 18
- welocalize: 16
- appen: 9
- outlier: 4
- rws: 3

## Counts by Candidate Type

- language_locale: 32
- ambiguous_metadata: 27
- language_requirement: 25
- location_restriction: 2

## Counts by Confidence

- high: 43
- ambiguous: 27
- medium: 16

## High-Confidence Examples

- `omgr_oneforma_language-locale_language-dash-locale_6522`: Andromeda - French - France (oneforma, language_dash_locale)
- `omgr_oneforma_language-locale_language-dash-locale_6526`: Andromeda - Spanish - Spain (oneforma, language_dash_locale)
- `omgr_oneforma_language-locale_language-dash-locale_6598`: Ava Audio Collection - English - Canada (oneforma, language_dash_locale)
- `omgr_oneforma_language-locale_language-dash-locale_6601`: Ava Audio Collection - English - United States (oneforma, language_dash_locale)
- `omgr_oneforma_language-locale_language-dash-locale_6602`: Ava Audio Collection - French - Canada (oneforma, language_dash_locale)
- `omgr_oneforma_language-locale_language-dash-locale_6603`: Ava Audio Collection - French - France (oneforma, language_dash_locale)
- `omgr_oneforma_language-locale_language-parenthetical-locale_6851`: Acceptability and Preference: Translation Raters - English (United States) - Bulgarian (Bulgaria) (oneforma, language_parenthetical_locale)
- `omgr_oneforma_language-locale_language-parenthetical-locale_6852`: Acceptability and Preference: Translation Raters - English (United States) - Catalan (Spain) (oneforma, language_parenthetical_locale)
- `omgr_oneforma_language-locale_language-parenthetical-locale_6855`: Acceptability and Preference: Translation Raters - English (United States) - Croatian (Croatia) (oneforma, language_parenthetical_locale)
- `omgr_oneforma_language-locale_language-parenthetical-locale_6858`: Acceptability and Preference: Translation Raters - English (United States) - Dutch (Belgium) (oneforma, language_parenthetical_locale)

## Ambiguous Examples

- `omgr_mercor_ambiguous-metadata_ampersand-language-list_986`: Generalist - English & Assamese (mercor, ampersand_language_list)
- `omgr_welocalize_ambiguous-metadata_country-only-title-text_7525`: Ads Quality Rater - Bengali (India) (welocalize, country_only_title_text)
- `omgr_welocalize_ambiguous-metadata_country-only-title-text_7528`: Ads Quality Rater - Catalan (Spain) (welocalize, country_only_title_text)
- `omgr_welocalize_ambiguous-metadata_country-only-title-text_7532`: Ads Quality Rater - Dutch (France) (welocalize, country_only_title_text)
- `omgr_welocalize_ambiguous-metadata_country-only-title-text_7543`: Ads Quality Rater - Italian (Brazil) (welocalize, country_only_title_text)
- `omgr_welocalize_ambiguous-metadata_country-only-title-text_7544`: Ads Quality Rater - Italian (Germany) (welocalize, country_only_title_text)
- `omgr_welocalize_ambiguous-metadata_country-only-title-text_7556`: Ads Quality Rater - Tamil (India) (welocalize, country_only_title_text)
- `omgr_welocalize_ambiguous-metadata_unmapped-dialect-or-language-token_7797`: Shape the Future of AI - Basque Talent Hub (welocalize, unmapped_dialect_or_language_token)
- `omgr_rws_ambiguous-metadata_country-only-title-text_7491`: Linguistic AI Auditor (Bengali Bangladesh/India) (rws, country_only_title_text)
- `omgr_rws_ambiguous-metadata_country-only-title-text_7505`: Speech AI Evaluation Specialist - Bengali (India) (rws, country_only_title_text)

## Human Review Instructions

- Edit `review_decision` in the CSV; default is `pending_review`.
- High-confidence candidates still require human approval before any future apply workflow.
- Ambiguous candidates are `apply_eligible=no` by default and should usually be kept diagnostic-only or sent for more research.
- This workflow does not apply metadata. A future guarded apply mode would need separate review.