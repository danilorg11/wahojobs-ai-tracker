"""Finite stored-state maintenance bounds; source request scopes are unchanged."""
RUNTIME_PREPARATION_SECONDS = 180
HEALTH_SECONDS = 210
SERVICE_START_SECONDS = 240
FINAL_READINESS_SECONDS = 20
BACKUP_SECONDS = 180
JOURNAL_PREPARATION_SECONDS = 300
PUBLICATION_SECONDS = 600
RECOVERY_SECONDS = 420
# Preparation, journal packaging, publication and supervisor overhead.
NON_NETWORK_EXECUTION_SECONDS = 960
