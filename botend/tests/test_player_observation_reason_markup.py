"""Published-label provenance and historical reason presentation contract."""
from datetime import datetime, timezone
from django.test import SimpleTestCase
from django.template.loader import render_to_string


class PublishedObservationReasonMarkupTests(SimpleTestCase):
    def render(self, **changes):
        observation = dict(status='stale', source_kind='ai', analysis_id=730,
            publication_current=False, stale_reason='source_changed',
            generated_at=datetime(2026,10,28,14,16,1,tzinfo=timezone.utc),
            has_supported_judgements=True, display_judgements=[dict(code='joint_progress',
                supported=True, label='行为线索', value='共同提高成绩线索', reason='原发布的冻结分析理由',
                evidence=[dict(id='run:123', text='同一发布的冻结证据',
                    run_url='https://raider.io/mythic-plus-runs/season-mn-2/123-example')])])
        observation.update(changes)
        return render_to_string('portal/spec_detail/_player_observation.html', {'observation':observation})

    def test_changed_source_keeps_frozen_reason_and_identifies_historical_analysis(self):
        html=self.render()
        for value in ('原发布的冻结分析理由','同一发布的冻结证据','资料已更新','上次已发布分析','待重新检查',
                'data-analysis-id="730"','data-publication-current="false"','2026-10-28T'):
            self.assertIn(value,html)
        self.assertIn('<details',html)
        self.assertIn('https://raider.io/mythic-plus-runs/season-mn-2/123-example',html)
        self.assertNotIn('data-publication-current="true"',html)

    def test_current_publication_is_not_called_historical(self):
        html=self.render(status='ready',publication_current=True,stale_reason='')
        self.assertIn('data-publication-current="true"',html)
        self.assertIn('原发布的冻结分析理由',html)
        self.assertNotIn('资料已更新',html)

    def test_hidden_rejected_and_no_history_remain_absent(self):
        for changes in ({'status':'pending'},{'status':'hidden'},{'is_published':False},
                {'review_status':'rejected'},{'has_supported_judgements':False,'display_judgements':[]}):
            with self.subTest(changes=changes):
                self.assertNotIn('id="player-observation"',self.render(**changes))

    def test_other_stale_reasons_keep_existing_failure_notice(self):
        html=self.render(stale_reason='source_failed')
        self.assertIn('已过期',html)
        self.assertNotIn('资料已更新',html)

    def test_reason_and_evidence_are_escaped_at_the_existing_template_boundary(self):
        html=self.render(display_judgements=[dict(code='joint_progress',supported=True,label='行为线索',
            value='共同提高成绩线索',reason='<script>bad()</script>',
            evidence=[dict(text='A & B < C',run_url='javascript:alert(1)')])])
        self.assertIn('&lt;script&gt;bad()&lt;/script&gt;',html)
        self.assertIn('A &amp; B &lt; C',html)
        self.assertNotIn('<script>',html)
        self.assertNotIn('href="javascript:',html)
