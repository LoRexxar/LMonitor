"""防止新增通用补丁破坏版本专用追加链，或生成器与交付规则脱节。"""
import difflib
import hashlib
import json
from pathlib import Path
import sys
from unittest import TestCase

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts'))
from build_simc_semantic_rules import render_rules


class SemanticPatchBundleTests(TestCase):
    def test_version_tail_remains_appendable_after_new_common_patch(self):
        directory=ROOT/'simc_patches/compatibility/30c4b061f2103132a34952d80c963303b82bf368'
        manifest=json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
        previous=max(p.name for p in (ROOT/'simc_patches').glob('*.patch'))
        for entry in manifest['append_patches']:
            self.assertGreater(entry['name'],previous)
            self.assertFalse((ROOT/'simc_patches'/entry['name']).exists())
            canonical=(directory/entry['path']).read_text(encoding='utf-8').encode('utf-8')
            self.assertEqual(hashlib.sha256(canonical).hexdigest(),entry['sha256'])
            previous=entry['name']

    def test_delivered_selector_tail_matches_the_reviewed_generator(self):
        patch=(ROOT/'simc_patches/0071-base-semantic-scope-and-state-lattice.patch').read_text(encoding='utf-8')
        name='engine/skill_damage_semantic_rules.inc'
        block=patch[patch.index('diff --git a/'+name):]
        before=''.join(line[1:] for line in block.splitlines(True) if line.startswith('+') and not line.startswith('+++'))
        expected='diff --git a/'+name+' b/'+name+'\n'+''.join(difflib.unified_diff(
            before.splitlines(True),render_rules().splitlines(True),fromfile='a/'+name,tofile='b/'+name))
        tail=ROOT/'simc_patches/compatibility/30c4b061f2103132a34952d80c963303b82bf368/0074-semantic-selectors-30c4b06.patch'
        self.assertEqual(tail.read_text(encoding='utf-8'),expected)
