"""Production candidate-only browser boundary using the existing account/workflow services.

No provider, database, source collector, ranking engine or model is constructed
on import. The public reader never calls this boundary. Pending login contexts
expire after ten minutes and fail closed on process restart; tracked data is durable.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from html import escape
import hmac
import json
import re
import secrets
import threading
from urllib.parse import parse_qsl, urlencode, urlsplit

from wahojobs import accounts, pipeline_actions, pipeline_state
from wahojobs.browser_session_authentication import DurableBrowserSessionAuthenticationGateway
from wahojobs.browser_session_lifecycle import create_request_scoped_session_secret_vault, discard_request_scoped_session_secret_vault
from wahojobs.persistent_profiles_application import BrowserRequestContext
from wahojobs.pipeline_records import list_pipeline_records
from wahojobs.pipeline_postings import load_posting
from wahojobs.trusted_login_completion import prepare_session_delivery
from wahojobs.workos_authkit_browser import WorkOSAuthKitBrowserResponse, _delivery_cookies

ORIGIN = 'https://www.wahojobs.com'
SESSION = '__Host-wahojobs_candidate_session'
CSRF = '__Host-wahojobs_candidate_csrf'
LOGIN_CSRF = '__Host-wahojobs_candidate_login_csrf'
TRANSACTION = '__Host-wahojobs_candidate_tx'
CONTEXT = '__Host-wahojobs_candidate_context'
COOKIES = frozenset({SESSION, CSRF, LOGIN_CSRF, TRANSACTION, CONTEXT})
ROUTES = {'/my-jobs': ('GET', 'HEAD'), '/candidate/login': ('GET', 'HEAD'),
    '/candidate/auth/start': ('POST',), '/candidate/auth/callback': ('GET',),
    '/candidate/state': ('GET',), '/candidate/intent': ('POST',),
    '/candidate/action': ('POST',), '/candidate/resume': ('GET', 'POST'),
    '/candidate/logout': ('POST',)}
ACTIONS = frozenset({'save', 'unsave', 'applied', 'undo_applied', 'not_interested', 'show_again', 'undo_discovery'})
OPAQUE = re.compile(r'[A-Za-z0-9_-]{43}\Z')


def safe_return(value):
    if type(value) is not str or len(value) > 1800 or re.search(r'[\x00-\x20\x7f\\#]', value):
        return None
    try:
        url = urlsplit(value)
        if url.scheme or url.netloc or re.search(r'%(?![0-9a-fA-F]{2})', value):
            return None
        pairs = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True, max_num_fields=8)
        if len({k for k, _ in pairs}) != len(pairs):
            return None
        if url.path == '/jobs':
            from wahojobs.public_jobs_catalog import parse_catalog_query
            return value if parse_catalog_query(url.query) is not None else None
        if re.fullmatch(r'/jobs/opportunity-[1-9][0-9]{0,18}', url.path):
            from wahojobs.public_jobs_catalog import validate_catalog_return_target
            for key, item in pairs:
                if key == 'variant' and re.fullmatch(r'[1-9][0-9]{0,18}', item):
                    continue
                if key == 'return_to' and validate_catalog_return_target(item) is not None:
                    continue
                return None
            return value
        if url.path == '/my-jobs' and all(k == 'view' and v in {'all', 'saved', 'in_progress', 'hidden'} for k,v in pairs):
            return value
    except (ValueError, UnicodeError):
        pass
    return None


def cookie(headers, name, pattern=OPAQUE):
    values = [v for k,v in headers if k.lower() == 'cookie']
    if len(values) > 1:
        return None
    found = []
    for part in values[0].split(';') if values else []:
        key, sep, value = part.strip().partition('=')
        if key == name and sep:
            found.append(value)
    return found[0] if len(found) == 1 and pattern.fullmatch(found[0]) else None


def set_cookie(name, value, *, age=600):
    return f'{name}={value}; Path=/; Max-Age={age}; Secure; HttpOnly; SameSite=Lax'


def reply(status, body='', *, location=None, extra=(), lease=None, owner=None, content_type='text/html; charset=utf-8'):
    body = body.encode() if type(body) is str else body
    headers = (('Content-Type',content_type),('Content-Length',str(len(body))),
        ('Cache-Control','private, no-store'),('X-Robots-Tag','noindex, nofollow'),
        ('X-Content-Type-Options','nosniff'),('Referrer-Policy','no-referrer'),
        ('Content-Security-Policy',"default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; "
            "font-src 'self'; script-src 'self'; connect-src 'self'; form-action 'self' https://api.workos.com https://*.authkit.app; base-uri 'none'; frame-ancestors 'none'")) + extra
    if location:
        headers += (('Location',location),)
    return WorkOSAuthKitBrowserResponse(status,body,headers,delivery_lease=lease,connection_owner=owner)


def page(title, body, *, resume=False):
    from wahojobs.public_catalog_brand import HEADER, CSS
    # Use the established My Jobs component styles and hierarchy.
    from scripts.local_product_app import CSS as WORKFLOW_CSS
    nav = HEADER.replace('</nav>',"<a href='/my-jobs'>My Jobs</a></nav>")
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta name='robots' content='noindex,nofollow'>"
        f'<title>{escape(title)} | Wahojobs</title><style>{WORKFLOW_CSS}{CSS}</style></head><body>{nav}'
        f"<main class='app-main'>{body}</main>"
        + ("<script defer src='/candidate-client.js'></script>" if resume else '') + '</body></html>')


def form_data(headers, stream):
    types = [v for k,v in headers if k.lower() == 'content-type']
    sizes = [v for k,v in headers if k.lower() == 'content-length']
    if types != ['application/x-www-form-urlencoded'] or len(sizes) != 1 or not re.fullmatch('[1-9][0-9]{0,3}',sizes[0]):
        raise ValueError('Invalid request')
    size = int(sizes[0])
    if size > 4096 or any(k.lower() == 'transfer-encoding' for k,v in headers):
        raise ValueError('Invalid request')
    raw = stream.read(size)
    if len(raw) != size:
        raise ValueError('Incomplete request')
    pairs = parse_qsl(raw.decode('utf-8','strict'),keep_blank_values=True,strict_parsing=True,max_num_fields=10)
    if len({k for k,v in pairs}) != len(pairs):
        raise ValueError('Repeated request field')
    return dict(pairs)


@dataclass
class PendingContext:
    target: str
    expires: datetime
    intent: dict | None = None
    transaction: str | None = None
    account: str | None = None
    consumed: bool = False


class CandidateAccountIntegration:
    def __init__(self, *, connection_factory, gateway, completion_policy, public_reader, client_id, clock=None):
        self.connections = connection_factory
        self.gateway = gateway
        self.policy = completion_policy
        self.reader = public_reader
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.subject_prefix = 'production:' + client_id + ':'
        if gateway.redirect_uri != ORIGIN + '/candidate/auth/callback':
            raise ValueError('Invalid candidate callback')
        self.auth = DurableBrowserSessionAuthenticationGateway(trusted_environment_namespace='production',clock=self.clock)
        self.contexts = {}
        self.lock = threading.RLock()

    def close(self):
        with self.lock:
            self.contexts.clear()
        self.gateway.close()

    def _context(self, headers):
        token = cookie(headers, CONTEXT)
        if token is None:
            return None
        key = sha256(token.encode()).hexdigest()
        value = self.contexts.get(key)
        return value if value and self.clock() < value.expires else None

    def _new_context(self, target, intent=None):
        now = self.clock()
        self.contexts = {k:v for k,v in self.contexts.items() if now < v.expires}
        if len(self.contexts) >= 128:
            raise ValueError('Too many unfinished sign-ins. Try again later.')
        token = secrets.token_urlsafe(32)
        self.contexts[sha256(token.encode()).hexdigest()] = PendingContext(target,now+timedelta(minutes=10),intent)
        return token

    def _actor(self, db, headers):
        token = cookie(headers, SESSION)
        if token is None:
            return None
        from wahojobs.persistent_profile_read_authorization import DurablePersistentProfileReadAuthorizationGateway
        previous = db.execute('PRAGMA query_only').fetchone()[0]
        try:
            db.execute('PRAGMA query_only=ON')
            actor = self.auth.authenticate_browser_request(db,
                BrowserRequestContext('GET','/account/profile',(('Cookie','wahojobs_session='+token),)))
            decision = (DurablePersistentProfileReadAuthorizationGateway().authorize_persistent_profile_read(
                db,authenticated_actor=actor) if actor else None)
        finally:
            db.execute('PRAGMA query_only='+('ON' if previous else 'OFF'))
        if actor is None:
            return None
        user_id, environment = actor.account_reference_for_authorization()
        identity = db.execute("SELECT provider_subject FROM auth_identities WHERE user_id=? "
            "AND provider='workos_authkit' AND disabled_at IS NULL",(user_id,)).fetchone()
        if environment != 'production' or identity is None or not identity[0].startswith(self.subject_prefix):
            return None
        # The existing ownership resolver checks the binding's immutable lineage.
        if decision is None or decision.state != 'authorized':
            return None
        principal = decision.grant_for_application().principal_for_repository()
        return user_id, 'candidate::' + principal.principal_id

    def _require_csrf(self, db, headers, form, *, login=False):
        name = LOGIN_CSRF if login else CSRF
        token = cookie(headers,name)
        supplied = form.get('csrf','')
        if token is None or not hmac.compare_digest(token,supplied):
            raise PermissionError('Request rejected')
        if not login:
            accounts.validate_session_csrf(db,session_token=cookie(headers,SESSION),csrf_secret=token,now=self.clock())
        return token

    def _posting(self, db, canonical, variant, *, current=True):
        posting = load_posting(db,variant)
        if posting is None or posting['canonical_opportunity_id'] != canonical:
            raise ValueError('This opportunity version is unavailable.')
        if current:
            with self.reader._lock:
                now = self.clock()
                if now < self.reader._valid_from:
                    raise ValueError('Availability requires a fresh observation.')
                if now >= self.reader._deadline:
                    self.reader._refresh(now)
                group = self.reader._by_id.get(canonical)
                if group is None or not any(v['job_id']==variant for v in group['_catalog_variants']):
                    raise ValueError('This version is unavailable or needs verification. Your history is retained.')
        return posting

    def _record(self, db, owner, canonical):
        rows = db.execute('SELECT pipeline_item_id FROM user_pipeline_items WHERE profile_id=? AND canonical_id=?',
            (owner,canonical)).fetchall()
        if len(rows) > 1:
            raise ValueError('Existing histories need support review; none was overwritten.')
        if not rows:
            return None
        from wahojobs.pipeline_records import load_pipeline_record
        return load_pipeline_record(db,rows[0][0],owner_profile_id=owner,mutation_grade=True)

    def _intent(self, db, form):
        expected = {'csrf','action','canonical','variant','version','key','return_to'}
        if set(form) != expected or form['action'] not in ACTIONS:
            raise ValueError('Invalid tracking action')
        if (not re.fullmatch('[1-9][0-9]{0,18}',form['canonical'])
                or not re.fullmatch('[1-9][0-9]{0,18}',form['variant'])
                or not re.fullmatch('0|[1-9][0-9]{0,9}',form['version'])
                or not OPAQUE.fullmatch(form['key']) or safe_return(form['return_to']) is None):
            raise ValueError('Invalid tracking context')
        canonical, variant = int(form['canonical']),int(form['variant'])
        posting = self._posting(db,canonical,variant) if form['action'] in {'save','applied'} else None
        if form['action']=='not_interested':
            # Hiding an existing history remains possible after source closure.
            posting = load_posting(db,variant)
            if posting is not None and posting['canonical_opportunity_id'] != canonical:
                raise ValueError('Invalid tracking context')
        return dict(action=form['action'],canonical=canonical,variant=variant,version=int(form['version']),
            key=form['key'],return_to=form['return_to'],source_identity=(posting['company_id'],posting['source_hash']) if posting else None)

    def _mutate(self, db, actor, intent):
        user, owner = actor
        action = intent['action']
        posting = self._posting(db,intent['canonical'],intent['variant']) if action in {'save','applied'} else None
        if posting and (posting['company_id'],posting['source_hash']) != intent['source_identity']:
            raise ValueError('This source identity changed. Refresh before tracking.')
        with pipeline_state.atomic(db):
            # This is only an empty internal owner row, never a confirmed profile.
            stamp = self.clock().isoformat(timespec='seconds')
            db.execute("INSERT INTO user_profiles(user_id,profile_id,display_name,is_sample,created_at,updated_at) "
                "SELECT ?,?,'Candidate',0,?,? WHERE NOT EXISTS(SELECT 1 FROM user_profiles WHERE profile_id=?)",
                (user,owner,stamp,stamp,owner))
            stored = db.execute('SELECT user_id FROM user_profiles WHERE profile_id=?',(owner,)).fetchone()
            if stored[0] != user:
                raise PermissionError('Owner unavailable')
            record = self._record(db,owner,intent['canonical'])
            common = dict(action=action,owner_profile_id=owner,idempotency_key=intent['key'],
                expected_version=intent['version'],match_run_id=None,actor_source='candidate_catalog')
            if record:
                # The first selected source remains authoritative. In particular,
                # a sibling must not replace the variant the candidate applied to.
                linked_id = re.fullmatch(r'pipeline::posting-v1::([1-9][0-9]*)::[0-9a-f]{64}',record.pipeline_item['pipeline_item_id'])
                if (linked_id is None or int(linked_id[1]) != intent['variant'] or posting and (
                        record.opportunity['external_id'] != posting['external_id'] or record.opportunity['url'] != (posting['url'] or ''))):
                    raise ValueError('This opportunity is tracked through another version. Open its version in My Jobs.')
                if intent['version'] != 0:
                    common['pipeline_item_id'] = record.pipeline_item['pipeline_item_id']
                if action == 'save' and intent['version'] != 0 and record.normalized_state['workflow_status'] not in {'recommended','saved'}:
                    raise ValueError('Existing application progress is preserved. Use its supported correction.')
            if not record or intent['version']==0:
                if action not in {'save','applied','not_interested'} or intent['version'] != 0:
                    raise ValueError('Refresh this job before changing its status.')
                if posting is None:
                    posting = self._posting(db,intent['canonical'],intent['variant'])
                common.update(source=posting['source'],title=posting['title'],url=posting['url'] or '',
                    opportunity_external_id=posting['external_id'] or '',canonical_id=intent['canonical'],posting_job_id=intent['variant'])
            return pipeline_actions.perform_pipeline_action(db,**common)

    def handle(self, method, target, headers, body_stream=None):
        items = tuple(headers.items()) if hasattr(headers,'items') else tuple(headers)
        parsed = urlsplit(target)
        if parsed.path not in ROUTES:
            return reply(404,'Page unavailable')
        if method not in ROUTES[parsed.path]:
            return reply(405,'Method not allowed')
        if len(target)>4096 or re.search(r'[\x00-\x20\x7f\\#]',target) or [v for k,v in items if k.lower()=='host'] != ['www.wahojobs.com']:
            return reply(400,'Invalid request')
        if method=='POST' and [v for k,v in items if k.lower()=='origin'] != [ORIGIN]:
            return reply(403,'Request rejected')
        if any(k.lower() in {'authorization','forwarded','x-owner-id','x-user-id'} or k.lower().startswith('x-forwarded-') for k,v in items):
            return reply(400,'Request rejected')
        db = None
        try:
            db = self.connections()
            with self.lock:
                result = self._handle(db,method,parsed,target,items,body_stream)
                if getattr(result,'_connection_owner',None) is not None:
                    db = None
                return result
        except PermissionError:
            return reply(403,page('Request rejected',"<h1>Request rejected</h1><a href='/my-jobs'>Sign in again</a>"))
        except accounts.SessionUnavailable:
            return reply(401,page('Sign in again',"<h1>Sign in again</h1><p>Your session expired. Your saved jobs and history are retained.</p><a href='/candidate/login'>Continue with email</a>"))
        except (ValueError,pipeline_state.PipelineStateError):
            return reply(409,page('Tracking unchanged',"<h1>Tracking unchanged</h1><p>This action could not be confirmed. Refresh to check the current status. A tracked source version and application history are preserved; sibling versions do not replace them.</p><a href='/my-jobs'>Refresh My Jobs</a>"))
        except Exception:
            return reply(503,page('Candidate account unavailable',"<h1>Candidate account temporarily unavailable</h1><p>Your action has not been confirmed. Try again shortly.</p><a href='/jobs'>Browse jobs</a>"))
        finally:
            if db is not None:
                db.close()

    def _handle(self, db, method, parsed, target, headers, stream):
        path = parsed.path
        if path == '/candidate/auth/callback':
            return self._callback(db,target,headers)
        params = dict(parse_qsl(parsed.query,keep_blank_values=True,strict_parsing=True,max_num_fields=3))
        if len(params) != len(parse_qsl(parsed.query,keep_blank_values=True)):
            raise ValueError('Invalid context')
        actor = self._actor(db,headers)
        if path == '/candidate/state':
            if set(params) != {'ids'} or not re.fullmatch(r'[1-9][0-9]{0,18}(?:,[1-9][0-9]{0,18}){0,49}',params['ids']):
                raise ValueError('Invalid job selection')
            token = cookie(headers,CSRF) if actor else secrets.token_urlsafe(32)
            states = {}
            if actor:
                for cid in set(map(int,params['ids'].split(','))):
                    record = self._record(db,actor[1],cid)
                    if record:
                        selected = re.fullmatch(r'pipeline::posting-v1::([1-9][0-9]*)::[0-9a-f]{64}',record.pipeline_item['pipeline_item_id'])
                        history = pipeline_state.list_transition_history(db,record.pipeline_item['pipeline_item_id'],actor[1])
                        states[str(cid)] = dict(record.normalized_state,
                            selected_variant=int(selected[1]) if selected else None,
                            restorable_applied=record.diagnostics['restorable_applied'],
                            undo_available=bool(history and history[-1]['action_name'] in {'product_save','product_not_interested','product_show_again','unsave'}))
            return reply(200,json.dumps(dict(authenticated=actor is not None,csrf=token,states=states)),
                content_type='application/json; charset=utf-8',extra=() if actor else (('Set-Cookie',set_cookie(LOGIN_CSRF,token)),))
        if path == '/candidate/login':
            if set(params)-{'return_to'}:
                raise ValueError('Invalid return context')
            context = self._context(headers)
            destination = context.target if context else safe_return(params.get('return_to','/my-jobs'))
            if destination is None:
                raise ValueError('Invalid return context')
            if actor and (not context or context.intent is None):
                return reply(303,location=destination)
            extras = []
            if context is None:
                token = self._new_context(destination)
                extras.append(('Set-Cookie',set_cookie(CONTEXT,token)))
            csrf = secrets.token_urlsafe(32)
            extras.append(('Set-Cookie',set_cookie(LOGIN_CSRF,csrf)))
            content = "<h1>Keep your jobs in one place</h1><p>Save opportunities, record where you applied, and show hidden jobs again. Sign in or create an account using an email code.</p>"
            content += f"<form method='post' action='/candidate/auth/start'><input type='hidden' name='csrf' value='{csrf}'><button>Continue with email</button></form>"
            content += f"<p><a href='{escape(destination,quote=True)}'>Continue browsing</a></p><p><a href='/privacy-policy'>Privacy Policy</a> · <a href='/tos'>Terms of Service</a> · <a href='/contact'>Account support</a></p>"
            return reply(200,page('Candidate sign in',content),extra=tuple(extras))
        if parsed.query and path != '/my-jobs':
            raise ValueError('Unexpected query')
        if path == '/candidate/auth/start':
            form = form_data(headers,stream)
            if set(form) != {'csrf'}:
                raise ValueError('Invalid login')
            self._require_csrf(db,headers,form,login=True)
            context = self._context(headers)
            if context is None or context.consumed:
                raise ValueError('Sign-in context expired. Please start again.')
            prepared = self.gateway.prepare_authorization(db)
            context.transaction = prepared.transaction_id
            return reply(303,location=prepared.authorization_url,extra=(('Set-Cookie',set_cookie(TRANSACTION,prepared.transaction_id)),))
        if path in {'/candidate/intent','/candidate/action'}:
            form = form_data(headers,stream)
            self._require_csrf(db,headers,form,login=actor is None)
            intent = self._intent(db,form)
            if actor is None:
                if intent['action'] not in {'save','applied','not_interested'}:
                    raise PermissionError('Sign in to change existing tracking')
                token = self._new_context(intent['return_to'],intent)
                return reply(303,location='/candidate/login',extra=(('Set-Cookie',set_cookie(CONTEXT,token)),))
            self._mutate(db,actor,intent)
            return reply(303,location=intent['return_to'])
        if actor is None:
            if path == '/my-jobs':
                return reply(303,location='/candidate/login?'+urlencode({'return_to':target}))
            return reply(401,'Sign in again')
        if path == '/candidate/logout':
            form = form_data(headers,stream)
            if set(form) != {'csrf'}:
                raise ValueError('Invalid sign out')
            self._require_csrf(db,headers,form)
            session = accounts.resolve_session(db,session_token=cookie(headers,SESSION),now=self.clock())
            accounts.revoke_current_session(db,session_token=cookie(headers,SESSION),expected_session_version=session.session_version,
                reason='user_logout',now=self.clock())
            return reply(303,location='/jobs',extra=tuple(('Set-Cookie',set_cookie(name,'',age=0)) for name in COOKIES))
        if path == '/candidate/resume':
            context = self._context(headers)
            if context is None or context.consumed or context.account != actor[0] or context.intent is None:
                raise ValueError('This one-use action expired or was already used. Check My Jobs.')
            if method == 'GET':
                csrf = cookie(headers,CSRF)
                return reply(200,page('Continue your tracking action',f"<h1>Continue your tracking action</h1><form data-candidate-resume method='post' action='/candidate/resume'><input type='hidden' name='csrf' value='{csrf}'><button>Continue</button></form><p><a href='{escape(context.target,quote=True)}'>Cancel</a></p>",resume=True))
            form = form_data(headers,stream)
            if set(form) != {'csrf'}:
                raise ValueError('Invalid continuation')
            self._require_csrf(db,headers,form)
            self._mutate(db,actor,context.intent)
            context.consumed = True
            return reply(303,location=context.target,extra=(('Set-Cookie',set_cookie(CONTEXT,'',age=0)),))
        if path == '/my-jobs':
            if set(params)-{'view'} or params.get('view','all') not in {'all','saved','in_progress','hidden'}:
                raise ValueError('Invalid view')
            return self._my_jobs(db,actor,headers,params.get('view','all'))
        return reply(404,'Page unavailable')

    def _callback(self, db, target, headers):
        context = self._context(headers)
        tx = cookie(headers,TRANSACTION,re.compile(r'wtx_[0-9a-f]{32}\Z'))
        if context is None or context.consumed or context.transaction != tx:
            raise PermissionError('Login context unavailable')
        vault = create_request_scoped_session_secret_vault()
        completion = self.gateway.complete_authorization(db,target,tx,self.policy,vault)
        if getattr(completion,'status',None) != 'issued':
            discard_request_scoped_session_secret_vault(vault)
            raise PermissionError('Email sign-in failed or expired. Start again.')
        lease = None
        try:
            lease = prepare_session_delivery(db,completion,vault,now=self.clock())
            return self._session_response(db,context,lease)
        except BaseException:
            if lease is not None:
                lease.fail_delivery()
            else:
                discard_request_scoped_session_secret_vault(vault)
            raise

    def _session_response(self, db, context, lease):
        session_cookie, csrf_cookie = _delivery_cookies(lease)
        # Reuse opaque account-session authority, with candidate-only host cookies.
        session_cookie = session_cookie.replace('wahojobs_session=',SESSION+'=',1)
        csrf_cookie = csrf_cookie.replace('__Host-wahojobs_session_csrf=',CSRF+'=',1)
        raw_session = session_cookie.split('=',1)[1].split(';',1)[0]
        context.account = accounts.resolve_session(db,session_token=raw_session,now=self.clock()).user_id
        extras = (('Set-Cookie',session_cookie),('Set-Cookie',csrf_cookie),
            ('Set-Cookie',set_cookie(TRANSACTION,'',age=0)),('Set-Cookie',set_cookie(LOGIN_CSRF,'',age=0)))
        # The connection must remain held until the existing delivery lease is
        # acknowledged by the HTTP handler. A separately opened owner is used.
        result = reply(303,location='/candidate/resume' if context.intent else context.target,extra=extras,lease=lease,owner=_BorrowedConnection(db))
        return result

    def _my_jobs(self, db, actor, headers, view):
        from scripts.local_product_app import render_lightweight_tracker_header, render_my_jobs_workspace, render_my_jobs_card
        user, owner = actor
        records = []
        for stored in list_pipeline_records(db,owner,mutation_grade=True):
            current = stored.normalized_state
            canonical = stored.opportunity['canonical_id']
            posting = db.execute('SELECT id FROM jobs WHERE canonical_opportunity_id=? AND external_id=? AND url=?',
                (canonical,stored.opportunity['external_id'],stored.opportunity['url'])).fetchall()
            exact = re.fullmatch(r'pipeline::posting-v1::([1-9][0-9]*)::[0-9a-f]{64}',stored.pipeline_item['pipeline_item_id'])
            variant = int(exact[1]) if exact else None
            available = False
            if variant and len(posting)==1 and posting[0][0]==variant:
                try:
                    self._posting(db,canonical,variant)
                    available = True
                except ValueError:
                    pass
            records.append(dict(current,status='not_interested' if current['visibility']=='hidden' else current['workflow_status'],
                state_version=current['version'],source=stored.opportunity['source'],title=stored.opportunity['title'],
                url=stored.opportunity['url'],canonical=canonical,variant=variant,available=available,
                notes=stored.display['notes'],reminder_date=stored.compatibility['reminder_date'],
                next_action='' if available else 'Availability unconfirmed or source unavailable. Your application history is retained.',
                restorable_applied=stored.diagnostics['restorable_applied']))
        csrf = cookie(headers,CSRF)
        def card(record, _run, tracker_view='all'):
            controls = []
            action_names = ['show_again'] if record['visibility']=='hidden' else ['not_interested']
            if record['workflow_status']=='saved':
                action_names += ['unsave'] + (['applied'] if record['available'] else [])
            if record['workflow_status']=='applied' and record['restorable_applied']:
                action_names += ['undo_applied']
            if record['workflow_status']=='recommended' and record['available']:
                action_names += ['save','applied']
            history = pipeline_state.list_transition_history(db,record['pipeline_item_id'],owner)
            if history and history[-1]['action_name'] in {'product_save','product_not_interested','product_show_again','unsave'}:
                action_names += ['undo_discovery']
            labels = {'save':'Save','unsave':'Unsave','applied':'Mark as applied','undo_applied':'Correct Applied status',
                'not_interested':'Not interested','show_again':'Show again','undo_discovery':'Undo'}
            destination = '/my-jobs'+('?' + urlencode({'view':view}) if view!='all' else '')
            if record['variant']:
                for action in action_names:
                    values = dict(csrf=csrf,action=action,canonical=record['canonical'],variant=record['variant'],
                        version=record['version'],key=secrets.token_urlsafe(32),return_to=destination)
                    inputs = ''.join(f"<input type='hidden' name='{k}' value='{escape(str(v),quote=True)}'>" for k,v in values.items())
                    controls.append(f"<form method='post' action='/candidate/action'>{inputs}<button>{labels[action]}</button></form>")
            controls.append('<details><summary>History</summary><ul>'+''.join(
                f"<li>{escape(t['occurred_at'])}: {escape(t['action_name'].replace('product_','').replace('_',' '))}</li>" for t in history)+'</ul></details>')
            detail = '/jobs/opportunity-'+str(record['canonical'])+'?'+urlencode({'variant':record['variant'],'return_to':'/jobs'}) if record['variant'] else '/jobs'
            return render_my_jobs_card(record,None,tracker_view=tracker_view,candidate_controls=''.join(controls),candidate_detail=detail,
                candidate_allow_original=record['available'])
        workspace = render_my_jobs_workspace(records,None,view,candidate_card=card,
            candidate_filter=lambda key:'/my-jobs'+('?' + urlencode({'view':key}) if key!='all' else ''))
        signout = f"<form method='post' action='/candidate/logout'><input type='hidden' name='csrf' value='{csrf}'><button>Candidate sign out</button></form>"
        body = render_lightweight_tracker_header(records,candidate=True)+"<p>Apply on the employer’s website. Mark as applied records your statement; Wahojobs does not submit applications.</p>"+workspace+signout
        return reply(200,page('My Jobs',body))


class _BorrowedConnection:
    """Delivery retains a separate connection owner; caller transfers ownership."""
    def __init__(self, connection):
        self.connection = connection
    def close(self):
        self.connection.close()
