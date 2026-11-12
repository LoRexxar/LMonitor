"""Real ORM + manifest-bound CSV regression for intrinsic vs display talent spells."""
import csv,io,json,tempfile,hashlib,pathlib
from django.test import TestCase
from botend.models import SimcAplSymbol,SimcAplSymbolScope,WowTalentVersion,WowTalentNodeMetadata,WowSpellSnapshot
from botend.services.simc_apl.readable import display_symbols

class TalentDefinitionDisplayTests(TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=pathlib.Path(self.temp.name)
  self.version=WowTalentVersion.objects.create(key='definition-display',branch='retail',current_build='12.1.5.70783',source_dir=str(self.root),is_active=True)
  symbol=SimcAplSymbol.objects.create(token='malevolence',symbol_kind='talent')
  SimcAplSymbolScope.objects.create(symbol=symbol,class_name='warlock',spec='destruction',trait_id=94842,spell_id=430014,source='simc_manifest')
  WowTalentNodeMetadata.objects.create(talent_version=self.version,class_name='Warlock',spec_name='Destruction',talent_id=94842,node_id=117439,spell_id=442726,display_spell_id=442726,name_zh='恶意',icon='verified_icon',source='db2_backfill')
  WowSpellSnapshot.objects.create(branch='wow',locale='zhCN',spell_id=442726,name_zh='恶意',description='正确显示法术的机制说明。',icon='verified_icon')
  self.write_source()
 def write_source(self,duplicate=False,build='12.1.5.70783'):
  manifest={};tables=[('TraitNodeEntry','TraitNodeEntry.csv',['ID','TraitDefinitionID'],[[117439,122451]]+([[999001,999002]]if duplicate else[])),('TraitDefinition.enUS','TraitDefinition_enUS.csv',['ID','SpellID'],[[122451,430014]]+([[999002,430014]]if duplicate else[]))]
  for key,name,fields,rows in tables:
   out=io.StringIO(newline='');writer=csv.writer(out);writer.writerow(fields);writer.writerows(rows);body=out.getvalue().encode();(self.root/name).write_bytes(body);manifest[key]={'sha256':hashlib.sha256(body).hexdigest(),'rows':len(rows),'identity':{'branch':'retail','build':build,'locale':'enUS'if key.endswith('.enUS')else'zhCN','table':key.split('.')[0]}}
  (self.root/'manifest.json').write_text(json.dumps(manifest))
 def display(self):return display_symbols('warlock','destruction',self.version)[('talent','malevolence')]
 def test_intrinsic_definition_spell_resolves_to_explicit_current_display_spell(self):
  value=self.display();self.assertEqual(value['spell_id'],442726);self.assertEqual(value['description'],'正确显示法术的机制说明。');self.assertIn('verified_icon',value['icon'])
 def test_bad_hash_does_not_fall_back_to_the_only_node(self):
  (self.root/'TraitDefinition_enUS.csv').write_text('ID,SpellID\n122451,430014\nextra,0\n');self.assertIsNone(self.display()['spell_id'])
 def test_other_build_source_does_not_prove_this_entry(self):
  self.write_source(build='wrong-build');self.assertIsNone(self.display()['spell_id'])
 def test_two_entries_with_the_same_intrinsic_spell_remain_ambiguous(self):
  WowTalentNodeMetadata.objects.create(talent_version=self.version,class_name='Warlock',spec_name='Destruction',talent_id=94842,node_id=999001,spell_id=999003,display_spell_id=999003,name_zh='另一选项',source='db2_backfill');self.write_source(duplicate=True);self.assertIsNone(self.display()['spell_id'])
