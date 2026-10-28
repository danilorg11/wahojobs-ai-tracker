"""Loopback-only stand-in for the Vercel executable, never a production tool."""
from http.client import HTTPConnection
import json
import os
import sys

port = int(os.environ['WAHOJOBS_RELEASE_FIXTURE_PORT'])
connection = HTTPConnection('127.0.0.1', port, timeout=10)
args = sys.argv[1:]
if args[0] == 'api' and args[1].startswith(('/v13/deployments/', '/v9/projects/')):
    connection.request('GET', args[1])
elif args[0] == 'promote':
    connection.request('POST', '/promote/'+args[1], body=b'')
else:
    raise SystemExit('fixture refuses unrelated command')
response = connection.getresponse()
raw = response.read()
connection.close()
if response.status != 200:
    raise SystemExit(1)
print(raw.decode())
