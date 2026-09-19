"""Owner-requested correction of an accidental Applied tracking action."""
import json

from wahojobs import pipeline_state as state


def correct_applied(conn, *, action, pipeline_item_id, owner_profile_id,
                    expected_version, idempotency_key):
    from wahojobs import pipeline_actions as actions
    if action not in {'undo_applied', 'correct_applied'} or not pipeline_item_id:
        raise state.InvalidTransition('Choose a tracked job to change its status.')
    state.require_expected_version(expected_version)
    if (not isinstance(idempotency_key, str) or len(idempotency_key) < 16
            or idempotency_key.startswith(actions.INTERNAL_IDEMPOTENCY_PREFIX)):
        raise actions.PipelineActionValidationError('Invalid correction request.')
    request = dict(action=action, item=pipeline_item_id, owner=owner_profile_id, version=expected_version)
    with state.atomic(conn):
        state.require_owned_pipeline_item(conn, pipeline_item_id, owner_profile_id)
        replay = conn.execute('SELECT * FROM user_pipeline_transitions WHERE idempotency_key=?',
                              (idempotency_key,)).fetchone()
        if replay:
            transition = state.transition_dict(replay)
            if transition['metadata'].get('candidate_status_correction_v1') != request:
                raise state.IdempotencyConflict('This request was already used for another action.')
            result = state.MutationResult(state.get_current_state(conn, pipeline_item_id, owner_profile_id), transition, True)
        else:
            current = state.get_current_state(conn, pipeline_item_id, owner_profile_id)
            if current['version'] != expected_version:
                raise state.StaleStateVersion('This job changed. Refresh before changing its status.')
            if current['workflow_status'] != 'applied':
                raise state.InvalidTransition('Only a current Applied status can be corrected here.')
            # No-op receipts are not effective workflow changes. Find the actual
            # application action, without guessing that its previous state was Saved.
            history = state.list_transition_history(conn, pipeline_item_id, owner_profile_id)
            effective = [t for t in history if t['affected_dimension'] in {'workflow', 'correction', 'undo'}
                         and t['before_state'] != t['after_state']]
            original = effective[-1] if effective else None
            if (original is None or original['action_name'] not in {'product_applied', 'resolve_unknown_workflow_applied'}
                    or not original['before_state'] or original['after_state']['workflow_status'] != 'applied'):
                raise state.InvalidTransition('The previous application status cannot be restored safely.')
            target = state.validate_state(current)
            target['workflow_status'] = original['before_state']['workflow_status']
            target['workflow_status_provenance'] = original['before_state']['workflow_status_provenance']
            if target['workflow_status'] is None and target['visibility'] == 'visible' and not target['reminder_at']:
                raise state.InvalidTransition('The earlier unknown status requires its reminder or hidden state.')

            def restore(inner, before):
                state.require_transition_has_no_child(inner, original['transition_id'])
                return target, None, original['transition_id']

            result = state.apply_mutation(conn, pipeline_item_id=pipeline_item_id,
                owner_profile_id=owner_profile_id, affected_dimension='correction',
                action_name=action, expected_version=expected_version, idempotency_key=idempotency_key,
                request_payload=request, state_builder=restore, actor_source='candidate',
                metadata={'candidate_status_correction_v1': request})
            mirror = actions.legacy_compatibility_from_state(result.state)
            conn.execute('UPDATE user_pipeline_items SET status=?,reminder_date=?,status_date=?,last_user_action=?,updated_at=? '
                         'WHERE pipeline_item_id=? AND profile_id=?',
                         (mirror['status'], mirror['reminder_date'], result.transition['occurred_at'][:10],
                          action, result.transition['occurred_at'], pipeline_item_id, owner_profile_id))
        item = conn.execute('SELECT * FROM user_pipeline_items WHERE pipeline_item_id=?', (pipeline_item_id,)).fetchone()
        return actions.PipelineActionResult(actions._public_item_identity(item), result.state, result.transition,
            actions._compatibility_snapshot(item), None, False, result.replayed)
