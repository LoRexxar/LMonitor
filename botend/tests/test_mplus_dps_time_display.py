"""Exercise the actual JS method-description branch for legacy and weighted snapshots."""
import json
import subprocess
from pathlib import Path
from django.conf import settings
from django.test import SimpleTestCase


class MplusDpsTimeDisplayTests(SimpleTestCase):
    def test_description_follows_the_published_method_not_the_deployed_js_version(self):
        source = (Path(settings.BASE_DIR) / 'static/portal/js/mplus-dps-rankings.js').read_text()
        start = source.index('        const weighting = payload.method?.time_weighting;')
        end = source.index("        const explanation = element('dl', 'mplus-rank-explanation');", start)
        function = source[start:end] + '\nreturn averageExplanation;'
        inputs = [{}, {'method': {}}, {'method': {'time_weighting': {
            'kind': 'exponential', 'half_life_days': 7, 'timestamp': 'fight_started_at'}}},
            {'method': {'time_weighting': {'kind': 'exponential', 'half_life_days': 14}}}]
        program = ('const describe = new Function("payload", ' + json.dumps(function) + ');'
                   'console.log(JSON.stringify(' + json.dumps(inputs) + '.map(describe)));')
        result = subprocess.run(['node', '-e', program], check=True, text=True, capture_output=True)
        descriptions = json.loads(result.stdout)
        for old in descriptions[:2]:
            self.assertIn('算术平均', old)
            self.assertNotIn('半衰期', old)
        self.assertIn('半衰期为 7 天', descriptions[2])
        self.assertIn('真实战斗开始时间', descriptions[2])
        self.assertIn('Σ（DPS × 时间权重）÷ Σ时间权重', descriptions[2])
        self.assertNotIn('算术平均', descriptions[2])
        self.assertIn('半衰期为 14 天', descriptions[3])
        self.assertIn('最高 DPS 为样本中的原始最大值，不做时间折减', source)
        self.assertIn('各副本的最终样本数加权', source)
