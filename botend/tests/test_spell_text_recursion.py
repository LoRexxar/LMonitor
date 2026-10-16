"""Bounded, real-ORM regressions for terminal spell-reference cleanup."""
from functools import wraps
import multiprocessing
import traceback
from unittest.mock import patch

from django.test import TestCase

from botend.models import WowSpellEffectSnapshot, WowSpellSnapshot
from botend.wow.spell_text import SpellTextResolver


def bounded_resolution(test):
    """Fork the real SQLite test DB; the parent can kill even stuck fallbacks."""
    @wraps(test)
    def run(self):
        context = multiprocessing.get_context('fork')
        receiver, sender = context.Pipe(duplex=False)

        def child():
            receiver.close()
            try:
                test(self)
                self.assert_sources_unchanged()
            except BaseException:
                sender.send(traceback.format_exc(limit=10))
            else:
                sender.send(None)
            finally:
                sender.close()

        process = context.Process(target=child)
        process.start()
        sender.close()
        try:
            self.assertTrue(receiver.poll(3), 'Spell reference resolution exceeded 3 seconds')
            error = receiver.recv()
            process.join(3)
            self.assertEqual(process.exitcode, 0)
            self.assertIsNone(error, error)
        finally:
            if process.is_alive():
                process.kill()
                process.join(3)
            receiver.close()
            process.close()
    return run


class SpellTextRecursionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.build = 'spell-recursion-regression'
        for sid, description, aura in (
            (143048, '$@spellaura143048', '$@spellaura143048'),
            (90001, '$@spellaura90001', '$@spellaura90001'),
            (90002, '甲$@spelltooltip90003', ''),
            (90003, '乙$@spelltooltip90002', ''),
            (90010, '外层$@spelltooltip90011', ''),
            (90011, '|cffffffff需要50点能量|r\n\n中层$@spellaura90012', '错误光环'),
            (90012, '错误描述', '$?c1[武器][狂怒]：$?s90013[已知][未知]，'
             '$?a90014[激活][未激活]，伤害提高$s1%。'),
        ):
            WowSpellSnapshot.objects.create(
                branch='wow', locale='zhCN', snapshot_build=cls.build,
                spell_id=sid, description=description, aura_description=aura,
            )
        WowSpellEffectSnapshot.objects.create(
            branch='wow', locale='zhCN', snapshot_build=cls.build,
            spell_id=90012, effect_index=0, base_points='25',
        )

    def setUp(self):
        self.resolver = SpellTextResolver(
            branch='retail', locale='zhCN', snapshot_build=self.build,
            allow_legacy_dump=False,
        )
        self.raw_spells = list(WowSpellSnapshot.objects.order_by('pk').values())
        self.raw_effects = list(WowSpellEffectSnapshot.objects.order_by('pk').values())

    def assert_sources_unchanged(self):
        # Rendered fallback must never overwrite authoritative source templates.
        self.assertEqual(list(WowSpellSnapshot.objects.order_by('pk').values()), self.raw_spells)
        self.assertEqual(list(WowSpellEffectSnapshot.objects.order_by('pk').values()), self.raw_effects)

    @bounded_resolution
    def test_self_referencing_aura_terminates_without_an_id_exception(self):
        for sid in (143048, 90001):
            row = WowSpellSnapshot.objects.get(spell_id=sid)
            self.assertEqual(self.resolver.resolve(row.description, sid), '')
            self.assertEqual(self.resolver.resolve(row.aura_description, sid), '')

    @bounded_resolution
    def test_two_node_tooltip_cycle_keeps_bounded_readable_text(self):
        row = WowSpellSnapshot.objects.get(spell_id=90002)
        self.assertEqual(self.resolver.resolve(row.description, row.spell_id), '甲乙甲乙甲')

    @bounded_resolution
    def test_depth_limit_drops_references_without_reading_or_expanding_them(self):
        raw = '保留正文$@spelldesc90010$@spelltooltip90002$@spellaura143048'
        with self.assertNumQueries(0):
            self.assertEqual(self.resolver.resolve(raw, 90010, depth=4), '保留正文')

    @bounded_resolution
    def test_exception_fallback_is_terminal_even_with_cyclic_references(self):
        row = WowSpellSnapshot.objects.get(spell_id=90002)
        # Inject only the failure that selects the fallback; snapshots and the
        # fallback itself are real, so an accidental re-entry still hits the cycle.
        with patch.object(
            self.resolver, '_resolve_contextual_conditionals', side_effect=ValueError('parse failure'),
        ), self.assertNumQueries(0):
            self.assertEqual(self.resolver.resolve(row.description, row.spell_id), '甲')

    @bounded_resolution
    def test_acyclic_references_keep_source_fields_values_and_all_context(self):
        raw = '$@spelldesc90010'
        self.assertEqual(
            self.resolver.resolve(raw, spec_index=1, known_spell_ids={90013}, active_aura_ids={90014}),
            '外层中层武器：已知，激活，伤害提高25%。',
        )
        self.assertEqual(
            self.resolver.resolve(raw, spec_index=2, known_spell_ids=set(), active_aura_ids=set()),
            '外层中层狂怒：未知，未激活，伤害提高25%。',
        )
