"""Legacy seekable handoff acceptance at the recursion boundary."""
import io
import json
import sys
from django.test import SimpleTestCase
from botend.services.simc_profile_product_io import read_profile_product


class ProfileReaderRecursionBoundaryTests(SimpleTestCase):
    def test_seekable_acceptance_matches_stdlib_at_same_external_call_depth(self):
        def legacy(text):
            return json.loads(text)

        def streaming(text):
            return read_profile_product(io.StringIO(text))

        def accepts(function, text):
            try:
                function(text)
                return True
            except RecursionError:
                return False

        ceiling = sys.getrecursionlimit()
        accepted = rejected = 0
        mismatches = []
        for depth in range(max(0, ceiling - 120), ceiling + 6):
            text = '[' + '[' * depth + '0' + ']' * depth + ',[],0]'
            old, new = accepts(legacy, text), accepts(streaming, text)
            accepted += old
            rejected += not old
            if old != new:
                mismatches.append((depth, old, new))
        self.assertGreater(accepted, 0)
        self.assertGreater(rejected, 0)
        self.assertEqual(mismatches, [])
