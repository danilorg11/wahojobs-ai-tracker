"""Explicit protected Production configuration; the beta configuration is unchanged."""
import json
import re
import secrets


def build_candidate_integration(path, runtime, reader):
    from wahojobs.workos_authkit_staging import _validated_external_file, _read_bounded_file
    from wahojobs.workos_authkit import WorkOSAuthKitConfiguration, WorkOSAuthKitGateway, create_workos_sdk_boundary
    from wahojobs.trusted_login_completion import create_workos_authkit_trusted_login_completion_policy
    from wahojobs.candidate_account import CandidateAccountIntegration, ORIGIN
    from datetime import timedelta
    config_path = _validated_external_file(path,configuration=True)
    def unique(pairs):
        result = {}
        for key,value in pairs:
            if key in result:
                raise ValueError('Duplicate configuration field')
            result[key] = value
        return result
    document = json.loads(_read_bounded_file(config_path,maximum=16384),object_pairs_hook=unique)
    required = {'enabled','public_controls','scope','workos_environment','workos_client_id','workos_api_key','gateway_key'}
    if (set(document)!=required or type(document['enabled']) is not bool
            or type(document['public_controls']) is not bool
            or document['scope']!='candidate_my_jobs_v1' or document['workos_environment']!='production'
            or not re.fullmatch('[0-9a-f]{64}',document['gateway_key'])
            or reader is None):
        raise ValueError('Invalid production candidate configuration')
    if not document['enabled']:
        return None, None
    configuration = WorkOSAuthKitConfiguration(client_id=document['workos_client_id'],
        redirect_uri=ORIGIN+'/candidate/auth/callback',environment_namespace='production',public_candidate_registration=True)
    gateway = WorkOSAuthKitGateway(configuration=configuration,
        boundary=create_workos_sdk_boundary(api_key=document['workos_api_key'],client_id=document['workos_client_id']),
        invitation_lookup_key=secrets.token_bytes(32))
    policy = create_workos_authkit_trusted_login_completion_policy(environment_namespace='production',
        idle_ttl=timedelta(hours=1),absolute_ttl=timedelta(hours=8))
    integration = CandidateAccountIntegration(connection_factory=runtime._connections.open_writable_connection,
        gateway=gateway,completion_policy=policy,public_reader=reader,client_id=document['workos_client_id'])
    integration.public_controls_enabled = document['public_controls']
    return integration, document['gateway_key']
