"""Stage consistent private runtime/proxy/invitation files without activation."""
import argparse
import base64
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))


def stage_configuration(input_path, destination):
    from wahojobs.beta_recovery import _new_directory,_write,_json
    from wahojobs.workos_authkit_staging import load_workos_authkit_staging_configuration
    configuration=load_workos_authkit_staging_configuration(str(input_path),remote_beta=True)
    try:
        if configuration.professional_background_companion is not None:
            raise ValueError('declared_beta_has_no_companion')
        target=_new_directory(destination)
        key=target/'invitation.key'
        runtime=dict(version=2,runtime_mode='remote_beta',environment_namespace=configuration.environment_namespace,
            database_path=str(configuration.database_path),public_origin=configuration.public_origin,
            redirect_uri=configuration.redirect_uri,workos_client_id=configuration.workos_client_id,
            workos_api_key=configuration.workos_api_key,proxy_secret=configuration.proxy_secret,
            wahojobs_invitation_lookup_key_base64=base64.b64encode(configuration.invitation_lookup_key).decode('ascii'),
            session_idle_ttl_seconds=int(configuration.session_idle_ttl.total_seconds()),
            session_absolute_ttl_seconds=int(configuration.session_absolute_ttl.total_seconds()),public_job_canary_ids=[])
        _write(target/'runtime.json',_json(runtime))
        _write(key,bytes(configuration.invitation_lookup_key))
        _write(target/'invitation-operations.json',_json(dict(version=2,purpose='private_beta_invitations',
            environment=configuration.environment_namespace,database_path=str(configuration.database_path),
            account_invitation_lookup_key_file=str(key))))
        _write(target/'caddy.env',('WAHOJOBS_BETA_HOST='+configuration.public_origin[8:]+'\n'
            'WAHOJOBS_BETA_PROXY_SECRET='+configuration.proxy_secret+'\n').encode('ascii'))
        return {'staged':True,'provider_calls':0,'activation':False}
    finally:
        configuration.clear_secrets()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',required=True,help='Protected complete runtime JSON; never pass secret values in arguments.')
    parser.add_argument('--new-directory',required=True)
    args=parser.parse_args(argv)
    try:
        stage_configuration(args.input,args.new_directory)
        print('private_configuration_staged; no listener or provider calls')
        return 0
    except Exception:
        print('private_configuration_failed; preserve partial output',file=sys.stderr)
        return 2


if __name__=='__main__': raise SystemExit(main())
