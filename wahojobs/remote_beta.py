"""Single-host HTTPS ingress for the existing WorkOS product composition.

Caddy terminates TLS and supplies one secret to a loopback-only listener. No
forwarded header supplies identity, host or scheme. Importing this module has no
side effects; no fixture transport can be selected by runtime configuration.
"""
from datetime import datetime, timezone
import hmac
import os
import re
from urllib.parse import urlsplit

from wahojobs.workos_authkit_browser import WorkOSAuthKitBrowserResponse
from wahojobs.workos_authkit_staging import WorkOSAuthKitStagingError

PROXY_HEADER = 'X-Wahojobs-Proxy'
_PROXY_HEADERS = {'forwarded', 'via', 'x-real-ip', 'x-original-host'}
_HOST = re.compile(r'(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}')
_PUBLIC = {'/login', '/auth/workos/start', '/auth/workos/callback', '/privacy', '/robots.txt'}


def require_remote_capabilities():
    # These switches belong only to development/extraction tools, never the beta.
    if any(name in os.environ for name in (
            'WAHOJOBS_PROFILE_INTAKE_OPENAI_ENABLED', 'WAHOJOBS_ALLOW_LOCAL_LOGIN',
            'WAHOJOBS_PRACTICE_LOGIN', 'WAHOJOBS_TEST_OVERLAY', 'OPENAI_API_KEY')):
        raise WorkOSAuthKitStagingError('configuration_invalid')


def validate_remote_configuration(document):
    require_remote_capabilities()
    origin = document.get('public_origin')
    if type(origin) is not str or not origin.startswith('https://'):
        raise WorkOSAuthKitStagingError('configuration_invalid')
    hostname = origin[8:]
    companion = document.get('professional_background_companion')
    if (not _HOST.fullmatch(hostname) or hostname.endswith(('.localhost', '.local'))
            or document.get('redirect_uri') != origin + '/auth/workos/callback'
            or document.get('runtime_mode') != 'remote_beta'
            or type(document.get('proxy_secret')) is not str
            or not re.fullmatch('[0-9a-f]{64}', document['proxy_secret'])
            or type(document.get('workos_api_key')) is not str
            or document['workos_api_key'].startswith('REPLACE')
            or document.get('public_job_canary_ids', []) != []
            or companion is not None and (type(companion) is not dict
                or companion.get('basis') != 'semantic_model_output')):
        raise WorkOSAuthKitStagingError('configuration_invalid')


def response(status, body, *, location=None, content_type='text/plain; charset=utf-8'):
    body = body.encode('utf-8')
    headers = (('Content-Type', content_type), ('Content-Length', str(len(body))),
        ('Cache-Control', 'no-store'), ('Referrer-Policy', 'no-referrer'),
        ('X-Content-Type-Options', 'nosniff'), ('X-Robots-Tag', 'noindex, nofollow'),
        ('Content-Security-Policy', "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"))
    if location:
        headers += (('Location', location),)
    return WorkOSAuthKitBrowserResponse(status, body, headers)


PRIVACY_TEXT = '''Wahojobs invitation-only beta — data use

WorkOS verifies your email using a one-time code. Wahojobs stores your WorkOS
identity, email, invitation record, account status and protected session records.
It stores profiles you explicitly confirm, profile revisions, saved jobs and the
application status, notes and in-app reminders you choose to record. Unconfirmed
manual work can be saved; saved manual and correction drafts persist across sessions.

Document uploads, document extraction and paid model preparation are disabled
in this beta. Recommendations compare your confirmed information with recorded
public employer evidence. A recent source observation is not confirmation that
an employer will accept your application or that an opportunity fits you.

You apply on the employer's website under its own terms. Wahojobs does not submit
applications and does not automatically send your Wahojobs profile to employers.

The operator can access stored beta data for support, recovery and account
requests. Technical request logs contain route categories, status, duration and
request IDs, not submitted profiles, callback queries or session credentials.
Private backups include stored account/profile/history data and evidence receipts.

Contact the beta operator through the same private channel that supplied your
invitation to request access to your data or account closure. Closure can revoke
access immediately; it does not instantly erase every retained record or backup.
The operator must explain the applicable retention and deletion policy before
real invitations. This page is a technical description, not a legal certification.
'''


class RemoteBetaIntegration:
    """Gate every non-auth route using the existing durable session authority."""
    def __init__(self, runtime, *, clock=None):
        self.runtime = runtime
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def handle(self, method, target, headers, body_stream=None):
        # Handler already enforced trusted ingress. The underlying product still
        # enforces its exact Host/Origin/CSRF contract independently.
        path = urlsplit(target).path
        if path == '/_ops/ready' and target == path and method in ('GET', 'HEAD'):
            try:
                with self.runtime._connections.read_only_connection_provider() as connection:
                    connection.execute('SELECT id FROM canonical_opportunities LIMIT 1').fetchone()
                    connection.execute('SELECT profile_id FROM user_profiles LIMIT 1').fetchone()
                return response(200, 'ready\n')
            except Exception:
                return response(503, 'unavailable\n')
        if path == '/robots.txt' and target == path and method in ('GET', 'HEAD'):
            return response(200, 'User-agent: *\nDisallow: /\n')
        if path == '/privacy' and target == path and method in ('GET', 'HEAD'):
            return response(200, PRIVACY_TEXT)
        # Block upload/extraction before consuming any request body, even if an
        # application feature switch or route is accidentally exposed elsewhere.
        from wahojobs.profile_intake.runtime import PROFILE_INTAKE_ROUTE, PROFILE_INTAKE_REVIEW_ROUTE
        if path in {PROFILE_INTAKE_ROUTE, PROFILE_INTAKE_REVIEW_ROUTE}:
            if method in ('GET', 'HEAD'):
                return response(303, 'Use manual profile creation.\n', location='/find-matches')
            return response(404, 'This capability is unavailable.\n')
        if path not in _PUBLIC:
            from wahojobs.browser_session_authentication import DurableBrowserSessionAuthenticationGateway
            from wahojobs.persistent_profiles_application import BrowserRequestContext
            gateway = DurableBrowserSessionAuthenticationGateway(
                trusted_environment_namespace='private_beta', clock=self.clock)
            with self.runtime._connections.read_only_connection_provider() as connection:
                actor = gateway.authenticate_browser_request(connection,
                    BrowserRequestContext('GET', '/account/profile', headers))
            if actor is None:
                return response(303, 'Sign in with your invitation.\n', location='/login')
        result = self.runtime.browser_integration.handle(method, target, headers, body_stream)
        if path == '/login' and method == 'GET' and result.status == 200:
            result.body = result.body.replace(b'</section>',
                b"<p><a href='/privacy'>Privacy and data use</a></p></section>", 1)
            result.headers = tuple((name, str(len(result.body)) if name.lower() == 'content-length' else value)
                for name, value in result.headers)
        return result


def make_remote_handler(runtime, proxy_secret, *, diagnostics=None, clock=None):
    from wahojobs.durable_product_browser_handler import make_durable_product_browser_handler
    if type(proxy_secret) is not str or not re.fullmatch('[0-9a-f]{64}', proxy_secret):
        raise WorkOSAuthKitStagingError('configuration_invalid')
    base = make_durable_product_browser_handler(RemoteBetaIntegration(runtime, clock=clock), diagnostics=diagnostics)
    authority = urlsplit(runtime.public_origin).netloc

    class RemoteHandler(base):
        def _dispatch_integration_request(self, method):
            items = tuple(self.headers.raw_items())
            secrets = [v for k, v in items if k.lower() == PROXY_HEADER.lower()]
            hosts = [v for k, v in items if k.lower() == 'host']
            if (self.client_address[0] != '127.0.0.1' or hosts != [authority]
                    or len(secrets) != 1 or not hmac.compare_digest(secrets[0], proxy_secret)
                    or any(k.lower() in _PROXY_HEADERS or k.lower().startswith('x-forwarded-') for k, v in items)):
                from wahojobs.durable_product_browser_handler import _validate_durable_response
                self._write_durable_response(_validate_durable_response(response(403, 'Ingress rejected.\n')),
                                             head=method == 'HEAD')
                self.close_connection = True
                return
            del self.headers[PROXY_HEADER]
            super()._dispatch_integration_request(method)

        def log_error(self, *_args):
            # http.server parser errors must not print a request/callback query.
            pass
    return RemoteHandler
