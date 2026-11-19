"""Frozen scope inputs should instantiate only the requested actor."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import SimpleTestCase

from scripts.prepare_simc_native_inputs import main


class NativeScopeInputsTests(SimpleTestCase):
    def test_augmentation_does_not_spawn_synthetic_allies_in_either_audit(self):
        with TemporaryDirectory() as tmp:
            source = Path(tmp)
            output = source / 'inputs'
            with patch('sys.argv', ['prepare', '--source', str(source), '--output', str(output)]):
                main()

            for kind in ('base', 'talents'):
                with self.subTest(kind=kind):
                    evoker = (output / kind / 'evoker_augmentation.simc').read_text().splitlines()
                    self.assertIn('evoker.make_simplified_if_alone=0', evoker)
                    for other in (output / kind).glob('*.simc'):
                        if other.stem != 'evoker_augmentation':
                            self.assertNotIn('evoker.make_simplified_if_alone', other.read_text())