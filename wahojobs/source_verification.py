"""Shared read-only source verification contract; independent of candidate matching.

SQL fragments use jobs j and companies c. No policy thresholds or personalized
requirements are introduced here. Existing trust evaluation remains the caller.
"""

# A Mercor response never certifies a complete snapshot. Only the exact job's
# accepted, versioned record capture can provide a newer availability clock.
MERCOR_OBSERVATION_IS_NEWER = """record_observation.id IS NOT NULL AND (
    source_run.id IS NULL OR julianday(record_observation.observed_at) >=
    julianday(COALESCE(source_run.finished_at, source_run.started_at)))"""
MERCOR_OBSERVATION_JOIN = """
LEFT JOIN job_source_content_captures record_observation
  ON record_observation.id = (
    SELECT sc.id FROM job_source_content_captures sc
    JOIN crawl_runs observed_run ON observed_run.id = sc.crawl_run_id
    WHERE c.slug = 'mercor' AND sc.job_id = j.id
      AND sc.provider = 'mercor' AND sc.source_type = 'mercor-marketplace'
      AND sc.record_promotion_contract_id = 'mercor_public_active_record_v1'
      AND sc.promotion_policy_version IN ('mercor_record_promotion_v1', 'mercor_record_promotion_v2')
      AND (sc.promotion_decision IN ('promoted', 'confirmed') OR (sc.promotion_policy_version = 'mercor_record_promotion_v2' AND sc.promotion_decision = 'held_degraded' AND sc.decision_reasons_json = '["summary_omits_compatible_supplemental_content"]'))
      AND sc.used_sample_data = 0
      AND sc.provider_outcome IN ('success', 'partial')
      AND sc.normalized_record_count = sc.candidate_count
      AND observed_run.company_id = c.id
      AND observed_run.status IN ('success', 'partial')
      AND observed_run.used_sample_data = 0
      AND julianday(sc.observed_at) IS NOT NULL
    ORDER BY sc.id DESC LIMIT 1
  )
"""

SOURCE_VERIFICATION_FIELDS = f"""CASE WHEN {MERCOR_OBSERVATION_IS_NEWER} THEN record_observation.crawl_run_id
               ELSE source_run.id END AS source_run_id,
          CASE WHEN {MERCOR_OBSERVATION_IS_NEWER} THEN record_observation.observed_at
               ELSE source_run.started_at END AS source_run_started_at,
          CASE WHEN {MERCOR_OBSERVATION_IS_NEWER} THEN record_observation.observed_at
               ELSE COALESCE(source_run.finished_at, source_run.started_at)
               END AS latest_successful_source_run_at,
          CASE WHEN source_run.id IS NULL AND record_observation.id IS NULL THEN 0 ELSE 1 END AS source_run_qualifies"""

SOURCE_VERIFICATION_JOINS = """LEFT JOIN crawl_runs source_run
          ON source_run.id = (
            SELECT cr.id
            FROM crawl_runs cr
            WHERE cr.company_id = c.id
              AND cr.status = 'success'
              AND cr.used_sample_data = 0
              AND cr.error_message IS NULL
            ORDER BY COALESCE(cr.finished_at, cr.started_at) DESC, cr.id DESC
            LIMIT 1
          )""" + MERCOR_OBSERVATION_JOIN
