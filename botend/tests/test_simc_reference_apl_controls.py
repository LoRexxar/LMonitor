from django.test import TestCase
from django.template.loader import render_to_string
from botend.models import SimcAplSymbol, SimcAplSymbolScope
from botend.services.simc_apl.readable import display_symbols, readable_apl
from botend.services.simc_apl.translation import CONTROL_ACTIONS

class AplControlDisplayTests(TestCase):
    def test_control_actions_never_become_game_spell_tooltips_even_with_spell_ids(self):
        for index,token in enumerate(sorted(CONTROL_ACTIONS)):
            symbol=SimcAplSymbol.objects.create(token=token,symbol_kind='action')
            SimcAplSymbolScope.objects.create(symbol=symbol,class_name='warrior',spec='fury',spell_id=500000+index,source=SimcAplSymbol.SOURCE_SIMC_MANIFEST,name_zh='不应展示的技能名')
        symbols=display_symbols('warrior','fury')
        for token in CONTROL_ACTIONS:
            with self.subTest(token=token):
                self.assertNotIn(('action',token),symbols)
        data=readable_apl('actions=/auto_attack\nactions+=/use_items\nactions+=/call_action_list,name=cooldowns\n',symbols)
        rows=data['groups'][0]['rows']
        self.assertTrue(all(row['reference']is None for row in rows))
        self.assertEqual(rows[0]['label'],'开始自动攻击')
        html=render_to_string('portal/_simc_reference_apl.html',{'apl_display':data})
        self.assertIn('开始自动攻击',html)
        self.assertNotIn('reference-spell-placeholder',html)
        self.assertNotIn('当前资料尚未提供说明',html)

    def test_all_verified_operations_have_no_game_spell_reference(self):
        tokens = ('retarget', 'cancel_action', 'strict_sequence', 'pick_up_fragment',
                  'apply_poison', 'summon_pet')
        for index, token in enumerate(tokens):
            symbol = SimcAplSymbol.objects.create(token=token, symbol_kind='action')
            SimcAplSymbolScope.objects.create(symbol=symbol, class_name='warrior', spec='fury',
                                            spell_id=600000 + index, source='simc_manifest')
        source = '\n'.join('actions' + ('=' if i == 0 else '+=') + '/' + token
                           for i, token in enumerate(tokens))
        rows = readable_apl(source, display_symbols('warrior', 'fury'))['groups'][0]['rows']
        self.assertEqual(len(rows), len(tokens))
        for row, token in zip(rows, tokens):
            with self.subTest(token=token):
                self.assertIn(token, CONTROL_ACTIONS)
                self.assertTrue(row['control'])
                self.assertIsNone(row['reference'])
                self.assertNotEqual(row['label'], token)
        html = render_to_string('portal/_simc_reference_apl.html', {'apl_display': {'groups': [{'rows': rows}]}})
        self.assertNotIn('reference-spell-placeholder', html)

    def test_same_named_aura_is_not_filtered_as_control_action(self):
        symbol=SimcAplSymbol.objects.create(token='auto_attack',symbol_kind='buff')
        SimcAplSymbolScope.objects.create(symbol=symbol,class_name='warrior',spec='fury',spell_id=550000,source=SimcAplSymbol.SOURCE_SIMC_MANIFEST,name_zh='测试光环')
        self.assertIn(('buff','auto_attack'),display_symbols('warrior','fury'))
