# Wahojobs 2.0

## Vision

Wahojobs 2.0 is not a traditional job board.

Its purpose is to track the AI Training job market and show users what changed since yesterday.

The core value is not job listings themselves. The core value is market intelligence.

Examples:

* New jobs posted today
* Jobs removed today
* New companies hiring
* Salary changes
* Location changes
* Hiring trends

## Target Users

People looking for:

* AI Training Jobs
* RLHF Jobs
* Data Annotation Jobs
* AI Evaluator Jobs
* Search Evaluation Jobs

## Initial Companies

Priority companies for the MVP and early expansion:

- Outlier
- DataAnnotation
- Mercor
- Handshake
- micro1
- Alignerr
- TELUS
- Welocalize
- OneForma
- RWS
- DataForce
- Centific
- Appen
- Invisible Technologies
- Scale AI
- Turing
- Remotasks
- Toloka

## MVP Scope

Build only the smallest possible MVP.

Requirements:

1. Database tables:

   * companies
   * jobs
   * crawl_runs

2. Support only one company initially.

3. Store:

   * title
   * company
   * location
   * url
   * first_seen_at
   * last_seen_at

4. Homepage:

   * total active jobs
   * jobs discovered today
   * last crawl date

5. Admin page:

   * run crawler manually
   * view crawler logs

6. Technology:

   * Next.js
   * TypeScript
   * Supabase
   * Vercel

7. Architecture:

   * crawler
   * parser
   * database
   * ui

## Important

Do not build email alerts yet.

Do not build authentication yet.

Do not build AI summaries yet.

Focus only on creating a clean and maintainable foundation.

Every job should preserve historical information whenever possible.

The future goal is to become an AI Training Jobs Tracker, not a traditional job board.
