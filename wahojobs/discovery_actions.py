"""Reversible owner actions; event history and application reports are retained."""
from wahojobs import pipeline_state as state
REVERSIBLE_ACTIONS = frozenset({'product_save', 'product_not_interested', 'product_show_again',
                               'product_remind_later', 'unsave'})


def offers_undo(operation):
    transition=operation.transition
    return bool(not operation.replayed and transition.get('action_name') in REVERSIBLE_ACTIONS
                and not transition.get('metadata',{}).get('transition_class')
                and transition.get('before_state') != transition.get('after_state'))


def restore_discovery(conn, *, action, pipeline_item_id, owner_profile_id,
                      expected_version, idempotency_key):
    from wahojobs import pipeline_actions as actions
    if action not in {'unsave', 'undo_discovery'} or not pipeline_item_id:
        raise state.InvalidTransition('Choose a tracked job to change its status.')
    state.require_expected_version(expected_version)
    if (not isinstance(idempotency_key, str) or len(idempotency_key) < 16
            or idempotency_key.startswith(actions.INTERNAL_IDEMPOTENCY_PREFIX)):
        raise actions.PipelineActionValidationError('Invalid discovery request.')
    request = dict(action=action, item=pipeline_item_id, owner=owner_profile_id, version=expected_version)
    with state.atomic(conn):
        state.require_owned_pipeline_item(conn, pipeline_item_id, owner_profile_id)
        replay = conn.execute('SELECT * FROM user_pipeline_transitions WHERE idempotency_key=?',
                              (idempotency_key,)).fetchone()
        if replay:
            transition = state.transition_dict(replay)
            if transition['metadata'].get('candidate_discovery_v1') != request:
                raise state.IdempotencyConflict('This request was used for another action.')
            result = state.MutationResult(state.get_current_state(conn, pipeline_item_id, owner_profile_id), transition, True)
        else:
            current = state.get_current_state(conn, pipeline_item_id, owner_profile_id)
            if current['version'] != expected_version:
                raise state.StaleStateVersion('This job changed. Refresh before changing its status.')
            common = dict(pipeline_item_id=pipeline_item_id, owner_profile_id=owner_profile_id,
                expected_version=expected_version, idempotency_key=idempotency_key,
                actor_source='candidate', metadata={'candidate_discovery_v1': request})
            if action == 'unsave':
                if current['workflow_status'] != 'saved':
                    raise state.InvalidTransition('Unsave is available only for Saved jobs. Other progress is preserved.')
                target = dict(state.validate_state(current), workflow_status='recommended', workflow_status_provenance='known')
                result = state.apply_mutation(conn, affected_dimension='workflow', action_name='unsave',
                    request_payload=request, state_builder=lambda _conn, _state: (target, None, None), **common)
            else:
                history = state.list_transition_history(conn, pipeline_item_id, owner_profile_id)
                original = history[-1] if history else None
                if not original or original['action_name'] not in REVERSIBLE_ACTIONS:
                    raise state.InvalidTransition('Only the latest reversible choice can be undone. Refresh this job.')
                result = state.undo_transition(conn, transition_id=original['transition_id'], **common)
            mirror = actions.legacy_compatibility_from_state(result.state)
            conn.execute('UPDATE user_pipeline_items SET status=?,reminder_date=?,status_date=?,last_user_action=?,updated_at=? '
                'WHERE pipeline_item_id=? AND profile_id=?',
                (mirror['status'], mirror['reminder_date'], result.transition['occurred_at'][:10],
                 action, result.transition['occurred_at'], pipeline_item_id, owner_profile_id))
        item = conn.execute('SELECT * FROM user_pipeline_items WHERE pipeline_item_id=?', (pipeline_item_id,)).fetchone()
        return actions.PipelineActionResult(actions._public_item_identity(item), result.state, result.transition,
            actions._compatibility_snapshot(item), None, False, result.replayed)
