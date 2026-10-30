import json
from django.db import connection, OperationalError
from django.test import TransactionTestCase
from botend.models import SimcSkillDamageSnapshot, SimcSkillDamageSnapshotActor
from botend.services.simc_skill_damage import SimcSkillDamageSnapshotService
from botend.dashboard.api import SimcSkillDamageSnapshotAPIView


class SkillSnapshotPacketTests(TransactionTestCase):
    def test_large_spec_publishes_under_bounded_parameters_and_roundtrips_wire(self):
        snapshot = SimcSkillDamageSnapshot.objects.create(
            simc_revision='a'*40, game_build='12.1.5.70575', schema_revision=51,
            status='running', payload={'total_spec_count':1},
        )
        actor = {'class':'druid','specialization':'balance', 'hero_talent_trees':[],
                 'actions':[{'token':f'action_{i}', 'description':'量化事实🌟'*1800,
                             'variant':{'runtime_conditions':[{'token':'a','stacks':i}]}}
                            for i in range(180)],
                 'specialization_passive_diagnostics':[{'reason':'诊断'*3000} for _ in range(100)]}
        unresolved = [{'reason':'未解析🌟'*2000} for _ in range(160)]
        largest = 0
        def packet_guard(execute, sql, params, many, context):
            nonlocal largest
            if sql.lstrip().upper().startswith(('INSERT','UPDATE')):
                for value in params or ():
                    if not isinstance(value,str):continue
                    size=len(value.encode('utf8'))+sum(value.count(c) for c in '\0\n\r\x1a\"\'\\')
                    largest=max(largest,size)
                    if size>4*1024*1024:
                        raise OperationalError(1153,'Regression packet budget exceeded')
            return execute(sql,params,many,context)
        service=SimcSkillDamageSnapshotService(snapshot)
        with connection.execute_wrapper(packet_guard):
            service._publish_profile_product_actor(actor, ordinal=0,
                profile_identity=('druid','balance'), unresolved=unresolved,
                raw_action_count=180,generated_spec_count=1,generated_action_count=180)
        self.assertLessEqual(largest,4*1024*1024)
        snapshot.refresh_from_db()
        payload=service.materialize_snapshot_payload(snapshot)
        self.assertEqual(payload['actors'],[actor])
        self.assertEqual(payload['unresolved'],[unresolved[0]])
        response=SimcSkillDamageSnapshotAPIView._sharded_snapshot_response(
            snapshot,job=None,can_generate=False)
        try:
            self.assertEqual(response.status_code,200)
            data=json.loads(b''.join(response.streaming_content))['data']['snapshot']
        finally:response.close()
        self.assertEqual(data['actors'],[actor])
        self.assertEqual(data['unresolved'],[unresolved[0]])
        self.assertEqual(snapshot.generated_spec_count,1)

    def test_legacy_inline_actor_remains_readable_without_rewrite(self):
        snapshot=SimcSkillDamageSnapshot.objects.create(simc_revision='b'*40,game_build='12.1.5.70575',schema_revision=49)
        actor={'class':'druid','specialization':'balance','actions':[{'token':'legacy'}]}
        row=SimcSkillDamageSnapshotActor.objects.create(snapshot=snapshot,ordinal=0,class_name='druid',specialization='balance',actor_payload=actor,unresolved_payload=[])
        self.assertEqual(SimcSkillDamageSnapshotService.materialize_snapshot_payload(snapshot)['actors'],[actor])
        row.refresh_from_db();self.assertEqual(row.actor_payload,actor)

    def test_late_progress_failure_rolls_back_all_new_parts_but_keeps_prior_spec(self):
        from botend.models import SimcSkillDamageSnapshotPart
        snapshot=SimcSkillDamageSnapshot.objects.create(simc_revision='c'*40,game_build='12.1.5.70575',schema_revision=51,generated_spec_count=1,generated_action_count=5)
        old=SimcSkillDamageSnapshotActor.objects.create(snapshot=snapshot,ordinal=0,class_name='druid',specialization='legacy',actor_payload={'facts':'unchanged'},unresolved_payload=[])
        def fail_progress(execute,sql,params,many,context):
            if sql.lstrip().upper().startswith('UPDATE') and 'generated_spec_count' in sql:
                raise OperationalError('fixture: final progress write failed')
            return execute(sql,params,many,context)
        with self.assertRaises(OperationalError),connection.execute_wrapper(fail_progress):
            SimcSkillDamageSnapshotService(snapshot)._publish_profile_product_actor(
                {'class':'druid','specialization':'balance','actions':[{'text':'量'*400000}]},
                ordinal=1,profile_identity=('druid','balance'),unresolved=[],raw_action_count=1,
                generated_spec_count=2,generated_action_count=6)
        self.assertEqual(snapshot.actor_rows.count(),1)
        self.assertFalse(SimcSkillDamageSnapshotPart.objects.exists())
        snapshot.refresh_from_db();old.refresh_from_db()
        self.assertEqual((snapshot.generated_spec_count,snapshot.generated_action_count),(1,5))
        self.assertEqual(old.actor_payload,{'facts':'unchanged'})

    def test_missing_or_unknown_parts_cannot_be_published_or_read_as_empty(self):
        from botend.services.simc_skill_snapshot_storage import SkillSnapshotActorStorage as Store
        snapshot=SimcSkillDamageSnapshot.objects.create(simc_revision='d'*40,game_build='12.1.5.70575',schema_revision=51)
        service=SimcSkillDamageSnapshotService(snapshot)
        snapshot.payload=service._storage_payload(total_spec_count=1);snapshot.save(update_fields=['payload'])
        service._publish_profile_product_actor({'class':'druid','specialization':'balance','actions':[{'text':'x'*700000}]},ordinal=0,profile_identity=('druid','balance'),unresolved=[],raw_action_count=1,generated_spec_count=1,generated_action_count=1)
        snapshot.refresh_from_db();row=snapshot.actor_rows.get()
        self.assertTrue(service._is_complete_wire_snapshot(snapshot))
        row.payload_parts.order_by('ordinal').last().delete()
        self.assertFalse(service._is_complete_wire_snapshot(snapshot))
        with self.assertRaises(ValueError):Store.read_document(row,'actor_payload')
        row.payload_storage={'format':'unknown'};row.save(update_fields=['payload_storage'])
        self.assertFalse(Store.is_complete(row))
        with self.assertRaises(ValueError):Store.read_document(row,'actor_payload')

    def test_reverse_migration_refuses_to_drop_fragmented_facts(self):
        import importlib
        from django.db.migrations.loader import MigrationLoader
        from django.db.migrations.exceptions import IrreversibleError
        from botend.services.simc_skill_snapshot_storage import SkillSnapshotActorStorage as Store
        snapshot=SimcSkillDamageSnapshot.objects.create(simc_revision='e'*40,game_build='12.1.5.70575',schema_revision=51)
        actor={'class':'druid','specialization':'balance','actions':[{'text':'x'*700000}]}
        row=Store.create_actor(snapshot=snapshot,ordinal=0,class_name='druid',specialization='balance',actor_payload=actor,unresolved_payload=[])
        count=row.payload_parts.count();manifest=row.payload_storage
        before=MigrationLoader(None).project_state([('botend','0261_talent_server_slots')])
        Migration=importlib.import_module('botend.migrations.0262_simc_skill_snapshot_parts').Migration
        with self.assertRaises(IrreversibleError):
            with connection.schema_editor() as editor:
                Migration('0262_simc_skill_snapshot_parts','botend').unapply(before,editor)
        row.refresh_from_db()
        self.assertEqual(row.payload_parts.count(),count)
        self.assertEqual(row.payload_storage,manifest)
        self.assertEqual(Store.read_document(row,'actor_payload'),actor)
