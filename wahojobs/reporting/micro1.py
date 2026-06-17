def get_micro1_metrics(conn):
    row = conn.execute(
        """
        SELECT
          COUNT(*) AS active_jobs,
          COUNT(DISTINCT TRIM(title)) AS unique_titles
        FROM jobs j
        JOIN companies c ON c.id = j.company_id
        WHERE c.slug = 'micro1'
          AND j.is_active = 1
          AND j.title NOT LIKE '[SIMULATION]%'
        """
    ).fetchone()
    duplicate_row = conn.execute(
        """
        SELECT COUNT(*) AS duplicate_title_count
        FROM (
          SELECT TRIM(title) AS normalized_title
          FROM jobs j
          JOIN companies c ON c.id = j.company_id
          WHERE c.slug = 'micro1'
            AND j.is_active = 1
            AND j.title NOT LIKE '[SIMULATION]%'
          GROUP BY normalized_title
          HAVING COUNT(*) > 1
        )
        """
    ).fetchone()

    return {
        "active_jobs": row["active_jobs"],
        "unique_titles": row["unique_titles"],
        "duplicate_title_count": duplicate_row["duplicate_title_count"],
    }
