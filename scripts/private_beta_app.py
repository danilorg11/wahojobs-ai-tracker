"""Supervised remote beta process; explicit configuration, never migrations."""
from http.server import ThreadingHTTPServer
from pathlib import Path
import argparse
import os
import signal
import socket
import sys
import threading
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wahojobs.remote_beta import make_remote_handler
from wahojobs.workos_authkit_staging import (
    WorkOSAuthKitStagingError, build_workos_authkit_staging_runtime,
    load_workos_authkit_staging_configuration,
)


class BetaServer(ThreadingHTTPServer):
    # Linux must rebind after a clean stop while prior connections are in
    # TIME_WAIT. This is not SO_REUSEPORT; the lifetime lease still precedes bind.
    allow_reuse_address = os.name == 'posix'
    daemon_threads = False
    block_on_close = True
    request_queue_size = 16

    def __init__(self, address, handler):
        self._slots = threading.BoundedSemaphore(16)
        super().__init__(address, handler)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(30)
        return request, address

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, *_args):
        print('private_beta_request_failed', file=sys.stderr, flush=True)


def run(configuration_path, *, diagnostics=None, runtime_builder=build_workos_authkit_staging_runtime,
        server_factory=BetaServer, ready=None):
    configuration = load_workos_authkit_staging_configuration(configuration_path, remote_beta=True)
    runtime = server = None
    previous = {}
    try:
        proxy_secret = configuration.proxy_secret
        runtime = runtime_builder(configuration)
        handler = make_remote_handler(runtime, proxy_secret, diagnostics=diagnostics)
        server = server_factory(runtime.bind_address, handler)
        # signal handlers run in the serve_forever thread; shutdown must run in
        # another thread. The server then joins all request workers before close.
        def stop(_signal, _frame):
            threading.Thread(target=server.shutdown, daemon=True).start()
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, stop)
        if ready:
            ready(runtime)
        print('private_beta_ready', flush=True)
        server.serve_forever(poll_interval=0.2)
    finally:
        configuration.clear_secrets()
        failed = False
        if server:
            try:
                server.server_close()
            except BaseException:
                failed = True
        if runtime:
            try:
                runtime.close(retain_ownership=failed)
            except BaseException:
                failed = True
        for signum, value in previous.items():
            try:
                signal.signal(signum, value)
            except BaseException:
                failed = True
        if failed:
            raise WorkOSAuthKitStagingError('shutdown_incomplete') from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--logs', required=True, help='Existing private parent for bounded per-run logs.')
    args = parser.parse_args(argv)
    try:
        from wahojobs.request_diagnostics import diagnostic_log
        parent = Path(args.logs)
        if not parent.is_absolute() or not parent.is_dir() or parent != parent.resolve():
            raise ValueError('invalid_log_parent')
        if len(list(parent.glob('run-*'))) >= 32:
            raise ValueError('log_archive_required')
        directory = parent / ('run-' + uuid.uuid4().hex)
        directory.mkdir(mode=0o700)
        with diagnostic_log(str(directory)) as diagnostics:
            run(args.config, diagnostics=diagnostics)
        print('private_beta_stopped', flush=True)
        return 0
    except WorkOSAuthKitStagingError as exc:
        print('private_beta_failed:' + exc.code, file=sys.stderr)
    except Exception:
        print('private_beta_failed:runtime_unavailable', file=sys.stderr)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
