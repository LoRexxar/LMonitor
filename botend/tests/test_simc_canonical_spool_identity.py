"""The selection-only consumer must not repeatedly decode large action graphs."""
import copy,gc,tempfile,weakref
from unittest import TestCase,mock
from botend.services.simc_skill_damage import _CanonicalActorSpool

class WeakAction(dict):
    pass

class CanonicalSpoolIdentityTests(TestCase):
    def test_identity_read_uses_only_frozen_selection_and_no_full_decode(self):
        with tempfile.TemporaryDirectory() as root:
            spool=_CanonicalActorSpool(root)
            actor={'name':'physical','selected_trait_ids':[117722,117723], 'actions':[{'token':'large','scenarios':[{'n':n} for n in range(100)]}]}
            original=copy.deepcopy(actor);spool.store_many(100,[actor])
            with mock.patch.object(spool,'load',side_effect=AssertionError('Unexpected complete actor JSON decode')):
                self.assertEqual(spool.load_identity(100,'physical'),{'selected_trait_ids':[117722,117723]})
            self.assertEqual(actor,original)
            returned=spool.load_identity(100,'physical');returned['selected_trait_ids'].append(9)
            self.assertEqual(spool.load_identity(100,'physical'),{'selected_trait_ids':[117722,117723]})
            self.assertIsNone(spool.load_identity(34,'physical'))

    def test_identity_manifest_does_not_pin_actor_or_action_graphs(self):
        with tempfile.TemporaryDirectory() as root:
            spool=_CanonicalActorSpool(root)
            action=WeakAction(token='root',scenarios=[{'layers':[1,2,3]}]);ref=weakref.ref(action)
            actor={'name':'physical','selected_trait_ids':[1,2],'actions':[action]}
            spool.store_many(100,[actor]);del actor,action;gc.collect()
            self.assertIsNone(ref())
            self.assertEqual(spool.load_identity(100,'physical'),{'selected_trait_ids':[1,2]})

    def test_missing_selection_field_remains_missing_not_invented(self):
        with tempfile.TemporaryDirectory() as root:
            spool=_CanonicalActorSpool(root);spool.store_many(100,[{'name':'physical','actions':[]}])
            self.assertEqual(spool.load_identity(100,'physical'),{})
