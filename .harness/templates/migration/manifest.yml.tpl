version: 1
release: vX.Y.Z
created_at: ""
created_by: ""
execution:
  business_upgrade_rollback: forbidden
  progress_model: checkpoint
  candidate_on_failure: preserve
  resume_from: last-committed-checkpoint
  data_rollback: forbidden
  failure_phases: [migration, quality-check, service-start, release]
  rollback_authority: explicit-human-release-rollback
items:
  - id: 001-example
    description: ""
    forward: 001-example.up.sql
    rollback: 001-example.down.sql
    requires: []
    reversible: true
    forward_compatible: true
    estimated_duration_seconds: 30
signature: ""
