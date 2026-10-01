"""Export disposable anonymous HTML for the opt-in, intercepted Edge check."""
import json
from pathlib import Path
import sys

from tests.test_public_catalog_reader import PublicCatalogReaderTests
from wahojobs import public_catalog_analytics as analytics


if __name__ == '__main__':
    fixture = PublicCatalogReaderTests()
    fixture.setUp()
    try:
        catalog = fixture.get('/jobs')
        detail = fixture.get('/jobs/opportunity-9002')
        frame = fixture.get(analytics.FRAME_PATH)
        Path(sys.argv[1]).write_text(json.dumps({
            'script': analytics.SCRIPT,
            'csp': dict(catalog.headers)['Content-Security-Policy'],
            'frameCsp': dict(frame.headers)['Content-Security-Policy'],
            'frame': frame.body.decode(), 'catalog': catalog.body.decode(),
            'detail': detail.body.decode()}), encoding='utf-8')
    finally:
        fixture.doCleanups()
