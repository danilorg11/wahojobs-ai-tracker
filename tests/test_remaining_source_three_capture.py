"""Bounded capture transport gates; fixtures do not claim employer evidence."""
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from scripts import remaining_source_three_capture as capture


class CaptureSafetyTests(unittest.TestCase):
    def test_unlinked_handshake_asset_cannot_use_a_request(self):
        with TemporaryDirectory() as directory:
            recorder = capture.Recorder('handshake', Path(directory),
                                        dict.fromkeys(capture.LIMITS, 0))
            with self.assertRaisesRegex(ValueError, 'out_of_scope'):
                recorder.request('https://framerusercontent.com/sites/unlinked.mjs')
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_cross_host_surge_detail_cannot_use_a_request(self):
        with TemporaryDirectory() as directory:
            recorder = capture.Recorder('surge', Path(directory),
                                        dict.fromkeys(capture.LIMITS, 0))
            recorder.details.add('https://surgehq.ai/workforce/role')
            with self.assertRaisesRegex(ValueError, 'out_of_scope'):
                recorder.request('https://elsewhere.example/workforce/role')
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_redirect_is_retained_once_and_not_followed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            recorder = capture.Recorder('surge', root, dict.fromkeys(capture.LIMITS, 0))
            requested = 'https://surgehq.ai/workforce'
            redirected = 'https://elsewhere.example/landing'
            response = HTTPError(requested, 301, 'Moved', {'Location': redirected},
                                 io.BytesIO(b'moved'))

            class Opener:
                def open(self, request, timeout):
                    raise response

            with patch.object(capture, 'build_opener', return_value=Opener()):
                with self.assertRaisesRegex(ValueError, 'not_qualified'):
                    recorder.request(requested)
            self.assertEqual(recorder.ordinal, 1)
            saved = json.loads((root/'completion-001.json').read_text())
            self.assertEqual(saved['status'], 301)
            self.assertEqual(saved['response_headers']['Location'], redirected)
            self.assertEqual((root/'response-001.raw').read_bytes(), b'moved')

    def test_aggregate_cap_blocks_before_request(self):
        with TemporaryDirectory() as directory:
            prior = {'handshake': 100, 'outlier': 99, 'surge': 1}
            recorder = capture.Recorder('surge', Path(directory), prior)
            with self.assertRaisesRegex(ValueError, 'cap_reached'):
                recorder.request('https://surgehq.ai/workforce')
            self.assertEqual(recorder.ordinal, 0)


if __name__ == '__main__':
    unittest.main()
