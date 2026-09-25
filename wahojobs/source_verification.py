"""Shared read-only source verification contract; independent of candidate matching.

SQL fragments use jobs j and companies c. No policy thresholds or personalized
requirements are introduced here. Existing trust evaluation remains the caller.
"""

# A partial response cannot certify a complete snapshot. Only the exact job's
# accepted, versioned record capture can provide its availability clock.
INDIVIDUAL_SOURCE = "c.slug IN ('dataannotation', 'dataforce', 'handshake', 'surge', 'outlier')"
RECORD_OBSERVATION_IS_NEWER = f"""record_observation.id IS NOT NULL AND (
    {INDIVIDUAL_SOURCE} OR source_run.id IS NULL OR julianday(record_observation.observed_at) >=
    julianday(COALESCE(source_run.finished_at, source_run.started_at)))"""
RECORD_OBSERVATION_JOIN = """
LEFT JOIN job_source_content_captures record_observation
  ON record_observation.id = (
    SELECT sc.id FROM job_source_content_captures sc
    JOIN crawl_runs observed_run ON observed_run.id = sc.crawl_run_id
    WHERE sc.job_id = j.id AND sc.provider = c.slug
      AND ((c.slug = 'mercor' AND sc.source_type = 'mercor-marketplace'
        AND sc.record_promotion_contract_id = 'mercor_public_active_record_v1'
        AND sc.promotion_policy_version IN ('mercor_record_promotion_v1', 'mercor_record_promotion_v2')
        AND (sc.promotion_decision IN ('promoted', 'confirmed') OR (sc.promotion_policy_version = 'mercor_record_promotion_v2' AND sc.promotion_decision = 'held_degraded' AND sc.decision_reasons_json = '["summary_omits_compatible_supplemental_content"]')))
      OR (c.slug = 'dataannotation' AND sc.source_type = 'evergreen-application-pages'
        AND sc.record_promotion_contract_id IN ('dataannotation_coding_evergreen_record_v1', 'dataannotation_evergreen_role_record_v2')
        AND sc.promotion_policy_version = 'job_source_promotion_v2'
        AND sc.promotion_decision IN ('promoted', 'confirmed') AND sc.provider_outcome = 'partial')
      OR (c.slug = 'dataforce' AND sc.source_type = 'dataforce-community-html'
        AND sc.record_promotion_contract_id = 'dataforce_index_detail_record_v1'
        AND sc.promotion_policy_version = 'job_source_promotion_v2'
        AND sc.promotion_decision IN ('promoted', 'confirmed') AND sc.provider_outcome = 'partial')
      OR (c.slug = 'surge' AND sc.source_type = 'public-worker-pages'
        AND sc.record_promotion_contract_id = 'surge_remote_workforce_record_v1'
        AND sc.promotion_policy_version = 'job_source_promotion_v2'
        AND sc.promotion_decision IN ('promoted', 'confirmed') AND sc.provider_outcome = 'partial')
      OR (c.slug = 'handshake' AND sc.source_type = 'framer-public-inventory'
        AND sc.record_promotion_contract_id = 'handshake_public_cms_record_v1'
        AND sc.promotion_policy_version = 'job_source_promotion_v2'
        AND sc.promotion_decision IN ('promoted', 'confirmed') AND sc.provider_outcome = 'partial')
      OR (c.slug = 'outlier' AND sc.source_type = 'outlier-job-board'
        AND sc.record_promotion_contract_id = 'outlier_index_detail_record_v1'
        AND sc.promotion_policy_version = 'job_source_promotion_v2'
        AND sc.promotion_decision IN ('promoted', 'confirmed') AND sc.provider_outcome = 'partial'))
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

SOURCE_VERIFICATION_FIELDS = f"""CASE WHEN {RECORD_OBSERVATION_IS_NEWER} THEN record_observation.crawl_run_id
               WHEN {INDIVIDUAL_SOURCE} THEN NULL ELSE source_run.id END AS source_run_id,
          CASE WHEN {RECORD_OBSERVATION_IS_NEWER} THEN record_observation.observed_at
               WHEN {INDIVIDUAL_SOURCE} THEN NULL ELSE source_run.started_at END AS source_run_started_at,
          CASE WHEN {RECORD_OBSERVATION_IS_NEWER} THEN record_observation.observed_at
               WHEN {INDIVIDUAL_SOURCE} THEN NULL ELSE COALESCE(source_run.finished_at, source_run.started_at)
               END AS latest_successful_source_run_at,
          CASE WHEN {INDIVIDUAL_SOURCE} THEN record_observation.id IS NOT NULL
               WHEN source_run.id IS NULL AND record_observation.id IS NULL THEN 0 ELSE 1 END AS source_run_qualifies"""

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
          )""" + RECORD_OBSERVATION_JOIN
