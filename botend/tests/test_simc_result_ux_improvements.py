from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.template.loader import render_to_string

from botend.models import SimcTask, SimcTaskBatch


class SimcResultUXTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='result_ux_user', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)

    def _render_attribute_batch(self, name, candidates):
        """Render the result UX from real attribute-sweep batch members.

        Attribute searches are batches of normal reference tasks now; the old
        task_type=2 row with a comma-separated result_file is not created.
        """
        batch = SimcTaskBatch.objects.create(
            user_id=self.user.id, name=name, batch_type='attribute_sweep', status=2,
        )
        results = []
        for index, (crit, haste, dps) in enumerate(candidates, 1):
            task = SimcTask.objects.create(
                user_id=self.user.id, name=f'{name} · 候选 {index}',
                simc_profile_id=0, task_type=1, mode='attribute_sweep', batch=batch,
                candidate_label=f'crit={crit}, haste={haste}', current_status=2,
                mode_params={'ratings': {'crit': crit, 'haste': haste}},
                result_summary={'dps': dps},
            )
            results.append({
                'task_id': task.id, 'attr1_name': '暴击', 'attr1_value': crit,
                'attr2_name': '急速', 'attr2_value': haste, 'dps': dps,
            })
        max_dps = max(row['dps'] for row in results)
        min_dps = min(row['dps'] for row in results)
        for row in results:
            row['delta_from_best_abs'] = max_dps - row['dps']
            row['delta_from_best_percent'] = (max_dps - row['dps']) * 100 / max_dps
        budgets = [row['attr1_value'] + row['attr2_value'] for row in results]
        stats = {
            'max_dps': max_dps, 'min_dps': min_dps,
            'avg_dps': sum(row['dps'] for row in results) / len(results),
            'above_avg': sum(row['dps'] > sum(r['dps'] for r in results) / len(results) for row in results),
            'count': len(results), 'best': max(results, key=lambda row: row['dps']),
            'worst': min(results, key=lambda row: row['dps']),
            'improvement_abs': max_dps - min_dps,
            'improvement_percent': (max_dps - min_dps) * 100 / min_dps,
            'total_budget': budgets[0], 'budget_is_fixed': len(set(budgets)) == 1,
            'near_optimal_count': sum(row['delta_from_best_percent'] <= 0.2 for row in results),
            'spread_narrow': (max_dps - min_dps) * 100 / min_dps <= 0.5,
        }
        return render_to_string('simc_attribute_analysis_ssr.html', {
            'task_id': batch.id, 'task_name': batch.name, 'results': results,
            'results_by_dps': sorted(results, key=lambda row: row['dps'], reverse=True),
            'stats': stats,
        })

    def test_attribute_ssr_uses_chinese_labels_and_honest_delta(self):
        html = self._render_attribute_batch(
            '属性标签与差距', [(1000, 2000, 100000), (1050, 1950, 101000)],
        )
        self.assertIn('暴击', html)
        self.assertIn('急速', html)
        self.assertIn('距最佳', html)
        self.assertNotIn('相对性能', html)
        self.assertNotIn('"attr1Name: "gear_crit"', html)
        self.assertNotIn('"attr2Name: "gear_haste"', html)

    def test_attribute_ssr_marks_near_optimal_and_fixed_budget(self):
        html = self._render_attribute_batch(
            '近似最优', [(1000, 2000, 100000), (1050, 1950, 100150), (1100, 1900, 100200)],
        )
        self.assertIn('近似最优', html)
        self.assertIn('固定总量', html)
        self.assertIn('最高DPS', html)
        self.assertIn('差异极小', html)

    def test_result_templates_keep_discrete_attribute_chart_and_safe_proxy(self):
        with open('templates/simc_attribute_analysis_ssr.html', encoding='utf-8') as f:
            ssr = f.read()
        with open('templates/simc_attribute_analysis.html', encoding='utf-8') as f:
            client = f.read()
        with open('templates/simc_result_view.html', encoding='utf-8') as f:
            result = f.read()

        self.assertIn('budget_is_fixed', ssr)
        self.assertIn("type: 'scatter'", ssr)
        self.assertIn('showLine: false', ssr)
        self.assertIn('距最佳不超过', client)
        self.assertIn('maxDeltaPercent > 0 && deltaPercent > maxDeltaPercent', client)
        self.assertNotIn('最低相对性能', client)
        self.assertIn("replace(/<[^>]*>/g, '')", result)
        self.assertIn('结果摘要可用，但没有可识别的技能明细', result)
        self.assertIn('/api/simc-result-proxy/', result)
        self.assertNotIn('raw_simc_code', result)

    def test_result_page_keeps_full_native_report_in_a_sandboxed_reader(self):
        with open('templates/simc_result_view.html', encoding='utf-8') as f:
            result = f.read()

        self.assertIn('原始 SimC 完整报告', result)
        self.assertIn('id="native-report-frame"', result)
        self.assertIn('sandbox="allow-same-origin"', result)
        self.assertIn('nativeReportFrame.srcdoc = html', result)
        self.assertIn('buildNativeReportOutline', result)

    def test_task_result_opens_owned_artifact_as_standalone_report(self):
        with open('static/dashboard/js/simc-workbench.js', encoding='utf-8') as f:
            workbench_js = f.read()
        with open('templates/simc_result_view.html', encoding='utf-8') as f:
            result = f.read()

        self.assertIn('href="${esc(artifact.preview_url)}"', workbench_js)
        self.assertNotIn('data-artifact-preview', workbench_js)
        self.assertNotIn('renderSimcArtifactFrame', workbench_js)
        self.assertNotIn('window.open(', workbench_js)
        self.assertIn('id="native-report-frame"', result)
        self.assertIn('sandbox="allow-same-origin"', result)

    def test_attribute_page_renders_four_stat_search_context_not_two_stat_curve_only(self):
        with open('templates/simc_attribute_analysis.html', encoding='utf-8') as f:
            client = f.read()

        self.assertIn('四属性 50 rating 局部寻优', client)
        self.assertIn('initial_ratings', client)
        self.assertIn('search_path', client)
        self.assertIn('all_candidates', client)
        self.assertIn('local_optimum_50_pairwise', client)
