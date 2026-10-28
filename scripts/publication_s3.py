"""Common adapter for the already reviewed conditional company-logo publisher.

Recovery retains immutable uploaded objects and the accepted site. No delete,
credential refresh, automatic PUT retry or new historical namespace is provided.
"""
import importlib.util
from pathlib import Path
import sys

import routine_release as r


def load(payload):
    path=Path(payload['native_module'])
    r.require(r.digest(path.read_bytes())==payload['native_sha256'],'native_asset_publisher_changed')
    spec=importlib.util.spec_from_file_location('company_asset_native',path)
    module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
    return module


def operate(action,request,root,native):
    payload=request['payload'];root=Path(root)
    boundary=native.Boundary(**payload['boundary']);boundary.validate()
    assets=native.load_assets(Path(payload['workspace']));reused=native.load_reused_assets(Path(payload['workspace']))
    journal=native.Journal(root/'native-journal')
    result=lambda state,**kw:dict(operation=request['operation'],signature=request['signature'],state=state,**kw)
    observations=[native.public_probe(a,boundary.bucket_owner) for a in (*assets,*reused)]
    if any(o['state']=='conflict' for o in observations):return result('foreign')
    if any(o['state']=='ambiguous' for o in observations):return result('unknown')
    dispatched=(root/'native-dispatch.json').exists()
    intents=[e for e in journal.events if e['kind']=='put_intent']
    responded={e['intent_id']:e['outcome'] for e in journal.events if e['kind']=='put_result'}
    uncertain=any(not native.is_denied(responded.get(e['intent_id'],{}))
        and next(o for a,o in zip((*assets,*reused),observations) if a.key==e['key'])['state']!='exact' for e in intents)
    exact=all(o['state']=='exact' for o in observations)
    if action in ('observe','cleanup'):
        if (root/'recovered.json').exists():return result('recovered',immutable_objects_retained=True)
        if not dispatched:return result('before',already_exact=sum(o['state']=='exact' for o in observations))
        if exact:return result('accepted' if (root/'accepted.json').exists() else 'applied')
        if uncertain:return result('unknown',native_intents_preserved=True)
        if (root/'known-failure.json').exists():return result('failed',recovery_required=True)
        return result('unknown',native_intents_preserved=True)
    if action=='apply':
        r.require(not dispatched,'native_put_dispatch_never_replayed')
        r.once(root/'native-dispatch.json',dict(at=r.now(),boundary=boundary.record()))
        aws=native.AwsCli(payload['aws_executable'],credential_mode=boundary.credential_mode)
        try:
            actual=native.operate('upload',assets,boundary,aws,journal,reused=reused)
        except Exception:
            # A known response is not permission to resend. The next observation
            # either proves exact objects or preserves an unresolved native intent.
            r.once(root/'known-failure.json',dict(at=r.now()))
            raise
        r.once(root/'native-result.json',actual)
        return result('applied')
    if action=='check':
        r.require(exact,'immutable_assets_not_all_exact')
        if not (root/'accepted.json').exists():r.once(root/'accepted.json',dict(at=r.now(),objects=len(observations)))
        return result('accepted',objects=len(observations))
    r.require(action=='recover' and dispatched and not uncertain,'unresolved_put_requires_original_reconciliation')
    if not (root/'recovered.json').exists():r.once(root/'recovered.json',dict(at=r.now(),accepted_site_unchanged=True,deleted_objects=0))
    return result('recovered',immutable_objects_retained=True,deleted_objects=0)


def publication(action,request,root):return operate(action,request,root,load(request['payload']))
