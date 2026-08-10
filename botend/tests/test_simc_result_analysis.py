from django.test import SimpleTestCase

from botend.services.simc_result_analysis import localize_report_summary, parse_simc_html_report


class SimcResultAnalysisTests(SimpleTestCase):
    def test_preserves_full_ability_and_buff_metrics_from_deferred_report(self):
        ability_rows = ''.join(
            f'''<tbody><tr class="toprow right">
              <td><a href="https://www.wowhead.com/spell={1000 + index}">Ability {index}</a></td>
              <td>{20000 - index}</td><td>{20 - index / 10:.1f}%</td><td>{10 + index:.1f}</td>
              <td>2.{index}s</td><td>12.{index}s</td><td>{3000 + index}</td><td>{4000 + index}</td>
              <td>Direct</td><td>{30 + index}</td><td>{5000 + index}</td><td>{9000 + index}</td>
              <td>{6000 + index}</td><td>25.{index}%</td><td>1.{index}%</td><td>80.{index}%</td>
            </tr><tr class="details hide"><td colspan="16">
              <table class="details"><tr><th>Type</th><th>Executes</th><th>Direct Results</th><th>Ticks</th><th>Tick Results</th><th>Refreshes</th><th>Execute Time per Execution</th><th>Tick Time per Tick</th><th>Actual Amount</th><th>Raw Amount</th><th>Mitigated</th><th>Amount per Total Time</th><th>Amount per Total Execute Time</th></tr>
              <tr><td>damage</td><td>{10 + index}.25</td><td>{9 + index}.00</td><td>{8 + index}.00</td><td>{7 + index}.00</td><td>{index}.50</td><td>1.25</td><td>2.50</td><td>123,456.00</td><td>150,000.00</td><td>17.70%</td><td>411.52</td><td>900.10</td></tr></table>
            </td></tr></tbody>'''
            for index in range(13)
        )
        html = f'''<html><body>
          <div class="player"><h2>Tester: 95,132 dps</h2><div class="toggle-content">
          <script type="text/x-deferred-html">
            <table class="sc sort stripetoprow"><thead><tr>
              <th>Damage Stats</th><th>DPS</th><th>DPS%</th><th>Execute</th><th>Interval</th>
              <th>Total Time</th><th>DPE</th><th>DPET</th><th>Type</th><th>Count</th>
              <th>Hit</th><th>Crit</th><th>Avg</th><th>Crit%</th><th>Avoid%</th><th>Up%</th>
            </tr></thead>{ability_rows}</table>
            <table class="sc sort stripebody"><thead>
              <tr><th></th><th colspan="3">Trigger Count</th><th colspan="2">Interval</th></tr>
              <tr><th>Dynamic Buffs</th><th>Start</th><th>Refresh</th><th>Total</th><th>Start</th><th>Trigger</th><th>Duration</th><th>Uptime</th><th>Benefit</th><th>Overflow</th><th>Expiry</th></tr>
            </thead><tbody>
              <tr class="right"><td><a href="https://www.wowhead.com/spell=1719">Recklessness</a></td><td>1.0</td><td>2.0</td><td>3.0</td><td>60.0s</td><td>40.0s</td><td>12.0s</td><td>42.50%</td><td>91.20%</td><td>0.3 (0.4)</td><td>1.5</td></tr>
              <tr class="details hide"><td colspan="11"><div><h4>Buff Details</h4><ul class="label"><li><span>max_stacks:</span>2</li><li><span>base duration:</span>12.00</li></ul></div><div><h4>Trigger Details</h4><ul class="label"><li><span>trigger_pct:</span>97.50%</li><li><span>uptime_min/max:</span>10.00% / 80.00%</li></ul><h4>Stack Uptimes</h4><ul><li><span class="label">recklessness_1:</span>30.00%</li><li><span class="label">recklessness_2:</span>12.50%</li></ul></div></td></tr>
            </tbody></table>
            <table class="sc stripebody"><thead><tr><th>Constant Buffs</th></tr></thead><tbody>
              <tr><td><a href="https://www.wowhead.com/spell=6673">Battle Shout</a></td></tr>
              <tr class="details hide"><td><h4>Buff Details</h4><ul class="label"><li><span>max_stacks:</span>1</li><li><span>base duration:</span>3600.00</li></ul></td></tr>
            </tbody></table>
          </script></div></div>
        </body></html>'''

        report = parse_simc_html_report(html)

        self.assertEqual(len(report['abilities']), 13)
        self.assertEqual(len(report['top_abilities']), 12)
        ability = report['abilities'][0]
        self.assertEqual(ability['spell_id'], '1000')
        self.assertEqual(ability['execute'], '10.0')
        self.assertEqual(ability['interval'], '2.0s')
        self.assertEqual(ability['crit_percent'], '25.0%')
        self.assertEqual(ability['uptime_percent'], '80.0%')
        self.assertEqual(ability['details']['executes'], '10.25')
        self.assertEqual(ability['details']['ticks'], '8.00')
        self.assertEqual(ability['details']['refreshes'], '0.50')
        self.assertEqual(ability['details']['actual_amount'], '123,456.00')

        self.assertEqual(len(report['buffs']['dynamic']), 1)
        buff = report['buffs']['dynamic'][0]
        self.assertEqual(buff['spell_id'], '1719')
        self.assertEqual(buff['trigger_count_total'], '3.0')
        self.assertEqual(buff['uptime'], '42.50%')
        self.assertEqual(buff['benefit'], '91.20%')
        self.assertEqual(buff['details']['trigger_pct'], '97.50%')
        self.assertEqual(buff['stack_uptimes'], [
            {'stack': 'recklessness_1', 'uptime': '30.00%'},
            {'stack': 'recklessness_2', 'uptime': '12.50%'},
        ])
        self.assertEqual(report['buffs']['constant'][0]['name'], 'Battle Shout')
        self.assertEqual(report['buffs']['constant'][0]['details']['base_duration'], '3600.00')

    def test_parses_sample_skill_sequence_table_from_deferred_report(self):
        html = '''<html><body>
          <div class="player"><h2>Tester: 95,132 dps</h2><div class="toggle-content">
          <script type="text/x-deferred-html">
            <h3>Sample Sequence Table</h3>
            <div class="toggle-content hide">
              <table class="sc"><thead><tr>
                <th>Time</th><th>#</th><th>Name [List]</th><th>Target</th><th>Resources</th><th>Buffs</th>
              </tr></thead><tbody>
                <tr class="left"><td class="right">Pre</td><td>1</td><td><b>berserker_stance</b><br/>[precombat]</td><td>Tester</td><td>0.0/130 <b>0%</b> rage</td><td></td></tr>
                <tr class="left"><td class="right">0:08.320</td><td>W</td><td><b>rampage</b><br/>[thane]</td><td>Fluffy Pillow</td><td>79.8/130 <b>61%</b> rage</td><td>avatar, enrage, frenzy(3)</td></tr>
              </tbody></table>
            </div>
          </script></div></div>
        </body></html>'''

        report = parse_simc_html_report(html)

        self.assertEqual(report['sample_sequence'], [
            {
                'time': 'Pre',
                'marker': '1',
                'action': 'berserker_stance',
                'action_list': 'precombat',
                'target': 'Tester',
                'resources': '0.0/130 0% rage',
                'buffs': '',
            },
            {
                'time': '0:08.320',
                'marker': 'W',
                'action': 'rampage',
                'action_list': 'thane',
                'target': 'Fluffy Pillow',
                'resources': '79.8/130 61% rage',
                'buffs': 'avatar, enrage, frenzy(3)',
            },
        ])

    def test_projects_complete_report_sections_as_bounded_plain_text(self):
        sections = '''
          <div class="player-section"><h3>Results, Spec and Gear</h3><table class="sc">
            <tr><th>DPS</th><th>DPS Error</th><th>DPS Range</th><th>DPR</th></tr>
            <tr><td>91,082.8</td><td>1,976.3 / 2.170%</td><td>38,484.4 / 42.3%</td><td>7,594.4</td></tr>
          </table></div>
          <div class="player-section"><h3>Procs, Uptimes &amp; Benefits</h3><table class="sc">
            <tr><th>Proc</th><th>Count</th><th>Interval</th></tr><tr><td>Tactician</td><td>4.0</td><td>4.9s</td></tr>
          </table></div>
          <div class="player-section"><h3>Parsed Player Effects</h3><table class="sc">
            <tr><th>Passive Effects</th><th>Spell</th><th>ID</th><th>Value</th></tr>
            <tr><td>All Crit</td><td>Cruel Strikes</td><td>392777</td><td>2.00%</td></tr>
            <tr class="details hide"><td><table><tr><td>must not leak nested detail</td></tr></table></td></tr>
          </table></div>
          <div class="player-section"><h3>Cooldown waste details</h3><table class="sc">
            <tr><th>Ability</th><th>Average</th></tr><tr><td>Mortal Strike</td><td>0.903</td></tr>
          </table></div>
          <div class="player-section"><h3>Resources</h3><table class="sc">
            <tr><th>Gains</th><th>Type</th><th>Total</th><th>Overflow</th></tr>
            <tr><td>Bloodsurge</td><td>Rage</td><td>13.64</td><td>0.00</td></tr>
          </table></div>
          <div class="player-section"><h3>Statistics &amp; Data Analysis</h3><div><table class="layout"><tr><td>
            <table class="sc"><tr><th>DPS</th><th>Mean</th><th>Min</th><th>Max</th></tr><tr><td>Distribution</td><td>91,082.8</td><td>70,000</td><td>110,000</td></tr></table>
          </td></tr></table></div></div>
          <div class="player-section"><h3>Action Priority List</h3><table class="sc">
            <tr><th></th><th></th><th>actions.default</th></tr><tr><th>#</th><th>count</th><th>action,conditions</th></tr>
            <tr><td>A</td><td>4.91</td><td>mortal_strike,if=rage&gt;30</td></tr>
          </table></div>
          <div class="player-section"><h3>Stats</h3><table class="sc">
            <tr><th></th><th>Raid-Buffed</th><th>Unbuffed</th><th>Gear Amount</th></tr>
            <tr><th>Strength</th><td>2277</td><td>2198</td><td>1243 (878)</td></tr>
          </table></div>
          <div class="player-section"><h3>Gear</h3><table class="sc">
            <tr><th>Source</th><th>Slot</th><th>Average Item Level: 288.00</th></tr>
            <tr><th>Local</th><th>Head</th><td>Night Ender's Tusks</td></tr>
            <tr><th colspan="2"></th><td>ilevel: 289, stats: { +115 Mastery }</td></tr>
          </table></div>
          <div class="player-section"><h3>Talents</h3><table class="sc talents">
            <tr><th></th><th colspan="2">Warrior Talents [34]</th></tr><tr><th>1</th><td>Battle Stance [1]</td><td></td></tr>
          </table></div>
          <div class="player-section"><h3>Profile</h3><pre>warrior="Tester"\nlevel=90\nactions=/mortal_strike</pre></div>
        '''
        html = f'''<html><body><div class="player"><h2>Tester: 91,083 dps</h2><div class="toggle-content">
          <script type="text/x-deferred-html">{sections}</script>
        </div></div></body></html>'''

        report = parse_simc_html_report(html)

        projected = {section['key']: section for section in report['sections']}
        self.assertEqual(list(projected), [
            'results', 'procs', 'effects', 'cooldown_waste', 'resources',
            'statistics', 'action_priority', 'stats', 'gear', 'talents', 'profile',
        ])
        self.assertEqual(projected['results']['tables'][0]['rows'][1][0]['text'], '91,082.8')
        self.assertEqual(projected['resources']['tables'][0]['rows'][1][0]['text'], 'Bloodsurge')
        self.assertEqual(projected['statistics']['tables'][0]['rows'][1][1]['text'], '91,082.8')
        self.assertEqual(projected['gear']['tables'][0]['rows'][2][1]['text'], 'ilevel: 289, stats: { +115 Mastery }')
        self.assertEqual(projected['talents']['tables'][0]['rows'][0][1]['colspan'], 2)
        self.assertEqual(projected['profile']['text_blocks'], [
            'warrior="Tester"\nlevel=90\nactions=/mortal_strike',
        ])
        self.assertNotIn('must not leak nested detail', str(report['sections']))

    def test_localizes_report_with_existing_apl_pairs_and_preserves_english(self):
        report = {
            'character': {'class': 'Warrior', 'spec': 'Fury', 'race': 'Orc'},
            'simulation': {'fight_style': 'Patchwerk'},
            'abilities': [
                {'name': 'Rampage', 'spell_id': '184367'},
                {'name': 'Unknown Strike', 'spell_id': ''},
            ],
            'top_abilities': [{'name': 'Rampage'}],
            'buffs': {
                'dynamic': [{'name': 'Recklessness', 'spell_id': '1719'}],
                'constant': [{'name': 'Battle Shout', 'spell_id': '6673'}],
            },
            'sample_sequence': [{
                'action': 'rampage', 'action_list': 'precombat',
                'buffs': 'avatar, enrage, frenzy(3)',
                'resources': '79.8/130 61% rage',
            }],
        }
        pairs = [
            ('action', 'rampage', '暴怒'),
            ('buff', 'recklessness', '鲁莽'),
            ('buff', 'avatar', '天神下凡'),
            ('buff', 'enrage', '激怒'),
            ('buff', 'frenzy', '狂乱'),
            ('action', 'battle_shout', '战斗怒吼'),
        ]

        localized = localize_report_summary(report, pairs)

        self.assertEqual(localized['character'], {
            'class': '战士', 'class_en': 'Warrior',
            'spec': '狂怒', 'spec_en': 'Fury',
            'race': '兽人', 'race_en': 'Orc',
        })
        self.assertEqual(localized['simulation']['fight_style'], '木桩战')
        self.assertEqual(localized['simulation']['fight_style_en'], 'Patchwerk')
        self.assertEqual(localized['abilities'][0]['name'], '暴怒')
        self.assertEqual(localized['abilities'][0]['name_en'], 'Rampage')
        self.assertEqual(localized['abilities'][1]['name'], 'Unknown Strike')
        self.assertNotIn('name_en', localized['abilities'][1])
        self.assertEqual(localized['top_abilities'][0]['name'], '暴怒')
        self.assertEqual(localized['buffs']['dynamic'][0]['name'], '鲁莽')
        self.assertEqual(localized['buffs']['constant'][0]['name'], '战斗怒吼')
        self.assertEqual(localized['sample_sequence'][0]['action'], '暴怒')
        self.assertEqual(localized['sample_sequence'][0]['action_en'], 'rampage')
        self.assertEqual(localized['sample_sequence'][0]['action_list'], '战前')
        self.assertEqual(localized['sample_sequence'][0]['buffs'], '天神下凡, 激怒, 狂乱(3)')
        self.assertEqual(localized['sample_sequence'][0]['resources'], '79.8/130 61% 怒气')
