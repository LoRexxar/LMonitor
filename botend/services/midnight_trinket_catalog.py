"""Pure, auditable parser for Bloodmallet MID1 trinket documents."""
from __future__ import annotations
import hashlib, re
from dataclasses import dataclass
from typing import Any
from botend.services.simc_player_config import SUPPORTED_SIMC_SPEC_IDENTITIES
MID1='MID1'
MID1_EXCLUDED_SPEC_KEYS = frozenset({
 'druid_restoration', 'evoker_augmentation', 'evoker_preservation',
 'monk_mistweaver', 'paladin_holy', 'priest_discipline', 'priest_holy',
 'shaman_restoration',
})
MID1_DEFAULT_SCENARIOS = (
 {'key':'castingpatchwerk','name':'Casting Patchwerk (1 Target / 300s)','simulation_params':{'iterations':10000,'fight_style':'CastingPatchwerk','desired_targets':1,'max_time':300}},
 {'key':'castingpatchwerk5','name':'Casting Patchwerk (5 Targets / 40s)','simulation_params':{'iterations':10000,'fight_style':'CastingPatchwerk','desired_targets':5,'max_time':40}},
 {'key':'castingpatchwerk20','name':'Casting Patchwerk (20 Targets / 40s)','simulation_params':{'iterations':10000,'fight_style':'CastingPatchwerk','desired_targets':20,'max_time':40}},
)
_SCALAR=re.compile(r'^[a-z][a-z0-9_.-]{0,79}=[a-zA-Z0-9_./+:-]{0,120}$')
SPECIAL_BONUS_IDS={250462:{'crit':606,'haste':604,'mastery':605,'versatility':607},248583:{'crit':13183,'haste':13184,'mastery':13185,'versatility':13186}}
SPECIAL_OPTION_LABELS={'crit':'暴击','haste':'急速','mastery':'精通','versatility':'全能'}
SPECIAL_BONUS_LABELS={item_id:{bonus_id:SPECIAL_OPTION_LABELS[option] for option,bonus_id in options.items()} for item_id,options in SPECIAL_BONUS_IDS.items()}
SPECIAL_OPTIONS={264507:{'violence':('midnight.crucible_of_erratic_energies_violence=1',),'sustenance':('midnight.crucible_of_erratic_energies_sustenance=1',),'predation':('midnight.crucible_of_erratic_energies_predation=1',),'predation+sustenance+violence+':('midnight.crucible_of_erratic_energies_predation=1','midnight.crucible_of_erratic_energies_sustenance=1','midnight.crucible_of_erratic_energies_violence=1')}}
MID1_SOURCE_LEVELS={
 'Delve': (308,321), 'World Quest': (308,321), 'World Boss': (308,321),
 'Dungeon': (321,334), 'Raid': (321,334), 'Profession': (308,321),
 'Reputation': (308,321), 'High PvP': (308,321),
}
@dataclass(frozen=True)
class TrinketVariant:
 candidate_key:str; item_id:int; name:str; option_key:str; item_level:int; spec_keys:tuple[str,...]; source_label:str; bonus_id:int|None=None; simc_options:tuple[str,...]=()
@dataclass(frozen=True)
class TrinketItem: item_id:int; name:str; source_label:str
@dataclass(frozen=True)
class MidnightTrinketCatalog: tier:str; spec_keys:tuple[str,...]; items:tuple[TrinketItem,...]; variants:tuple[TrinketVariant,...]
def _key(item_id,option,ilevel): return 'trinket-'+hashlib.sha256(f'mid1:{item_id}:{option}:{ilevel}'.encode()).hexdigest()[:24]
def parse_mid1_catalog(payload:dict[str,Any])->MidnightTrinketCatalog:
 docs=payload.get('documents') if isinstance(payload,dict) else None; expected=tuple(sorted(f'{c}_{s}' for c,s in SUPPORTED_SIMC_SPEC_IDENTITIES if f'{c}_{s}' not in MID1_EXCLUDED_SPEC_KEYS))
 if not isinstance(docs,dict) or not set(expected).issubset(docs): raise ValueError('MID1 catalog must contain all enabled specialization documents')
 rows=[]; identities={}; item_records={}
 for spec in expected:
  doc=docs[spec]
  if not isinstance(doc,dict) or doc.get('simc_settings',{}).get('tier')!=MID1: raise ValueError(f'{spec}: missing MID1 metadata')
  data,ids,sources=doc.get('data'),doc.get('item_ids'),doc.get('data_sources')
  if not isinstance(data,dict) or not isinstance(ids,dict) or not isinstance(sources,dict): raise ValueError(f'{spec}: malformed trinket metadata')
  for name,levels in data.items():
   if name=='baseline': continue
   if not isinstance(levels,dict) or name not in ids or name not in sources: raise ValueError(f'{spec}: untrusted or incomplete item identity')
   item_id,source=ids[name],sources[name]
   if type(item_id)is not int or item_id<=0 or not isinstance(source,str) or source.strip() not in {'Dungeon','Delve','Raid','Profession','Reputation','World Boss','High PvP','World Quest'} or not levels or any(not str(x).isdigit() or type(v) not in(int,float) for x,v in levels.items()): raise ValueError(f'{spec}: invalid item identity')
   identity=(item_id, name, source.strip()); previous=item_records.get((item_id, name))
   if previous is not None and previous != identity: raise ValueError(f'{spec}: inconsistent cross-specialization item metadata')
   item_records[(item_id, name)] = identity
   option=name.rsplit(' [',1)[1][:-1].lower() if ' [' in name and name.endswith(']') else ''
   if option and item_id not in SPECIAL_BONUS_IDS and item_id not in SPECIAL_OPTIONS: raise ValueError(f'{spec}: unsupported special variant {name}')
   identities.setdefault((item_id,name),source.strip()); rows.append((spec,item_id,name,option,source.strip()))
 items=tuple(TrinketItem(i,n,s) for (i,n),s in sorted(identities.items())); variants={}
 for spec,item_id,name,option,source in rows:
  levels=MID1_SOURCE_LEVELS.get(source)
  if levels is None: raise ValueError(f'{spec}: unsupported MID1 source')
  for level in levels:
   key=(item_id,option or 'default',level)
   bonus=SPECIAL_BONUS_IDS.get(item_id,{}).get(option); options=SPECIAL_OPTIONS.get(item_id,{}).get(option,())
   if any(not _SCALAR.fullmatch(x) for x in options): raise ValueError('special option is not a controlled scalar assignment')
   variants.setdefault(key,TrinketVariant(_key(*key),item_id,name,option or 'default',level,tuple(sorted({r[0] for r in rows if r[1]==item_id and r[3]==option})),source,bonus,tuple(options)))
 return MidnightTrinketCatalog(MID1,expected,items,tuple(sorted(variants.values(),key=lambda x:x.candidate_key)))

def build_mid1_panel_payload(catalog,user_id,slug='midnight-s1-trinkets'):
 from botend.services.simc_benchmark_config import resolve_default_benchmark_resources
 resources=resolve_default_benchmark_resources(catalog.spec_keys,user_id); specs=[]
 for order,spec_key in enumerate(catalog.spec_keys):
  selected=resources[spec_key]
  specs.append({'class_name':spec_key.split('_',1)[0],'spec_key':spec_key,'label':spec_key.replace('_',' ').title(),'apl_id':selected['apl'].pk,'template_id':selected['template'].pk,'backend_id':selected['backend'].pk,'profiles':[{'profile_id':selected['profile'].pk,'label':selected['profile'].name}],'display_order':order})
 candidates=[]
 for order,variant in enumerate(catalog.variants):
  raw=f'id={variant.item_id},ilevel={variant.item_level}'
  if variant.bonus_id is not None: raw+=f',bonus_id={variant.bonus_id}'
  params={
   'slot':'trinket1','raw_value':raw,
   'benchmark_profile': {
    'kind': 'trinket_standard_reference', 'item_level': 240,
   },
  }
  if variant.simc_options: params['simc_options']=list(variant.simc_options)
  label=variant.name
  if variant.option_key != 'default':
   label=f'{label} · {SPECIAL_OPTION_LABELS.get(variant.option_key,variant.option_key)}'
  candidates.append({'key':variant.candidate_key,'label':label,'candidate_type':'gear_swap','params':params,'spec_keys':list(variant.spec_keys),'source_label':variant.source_label,'display_order':order})
 return {'name':'Midnight Season 1 Trinkets','slug':slug,'description':'Audited MID1 trinket matrix','is_active':True,'is_public':True,'schedule_enabled':False,'interval_seconds':86400,'specs':specs,'scenarios':[dict(scenario, simulation_params=dict(scenario['simulation_params'])) for scenario in MID1_DEFAULT_SCENARIOS],'candidates':candidates}

def mid1_matrix_plan(payload):
 specs=[]; total_cases=total_runs=0
 for spec in payload['specs']:
  applicable=[c['key'] for c in payload['candidates'] if not c['spec_keys'] or spec['spec_key'] in c['spec_keys']]
  cases=len(spec['profiles'])*len(payload['scenarios']); runs=cases*(1+len(applicable)); total_cases+=cases; total_runs+=runs
  specs.append({'spec_key':spec['spec_key'],'apl_id':spec['apl_id'],'template_id':spec['template_id'],'backend_id':spec['backend_id'],'profile_id':spec['profiles'][0]['profile_id'],'candidate_keys':applicable,'case_count':cases,'run_count':runs})
 return {'slug':payload['slug'],'spec_count':len(specs),'scenario_count':len(payload['scenarios']),'candidate_count':len(payload['candidates']),'case_count':total_cases,'run_count':total_runs,'specs':specs}
