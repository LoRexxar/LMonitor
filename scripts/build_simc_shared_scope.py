"""补充跨职业团队光环的统一作用域，实际归零与展示使用同一份身份。"""
import argparse,difflib,hashlib,json,re,subprocess
from pathlib import Path
from scripts.simc_scope_resolution import load_dbc,REVIEWED_REVISION
from scripts.build_simc_specialization_scope import BUILD

def extend_shared_contract(contract, *, effect_id, base_value):
    if 'shared_aura:1126' in contract:raise ValueError('拒绝重复应用团队光环补充。')
    if effect_id!=404 or base_value!=3:raise ValueError('野性印记身份或数值漂移。')
    part={'spell_id':1126,'effect_index':1,'effect_id':404}
    fact={'effect_id':'shared_aura:1126','source_type':'reviewed_scope','source_kind':'passive',
          'source_name':'Mark of the Wild','display_name':'野性印记','source_spell_ids':[1126],
          'scope_evidence':'reviewed_dbc_native_effect_scope','excluded_before_probe':True,
          'partial_state':False,'global_components':[part],'local_components':[],
          'local_skill_bindings':[],'projections':[],
          'lower_skill_policy':'exclude_global_keep_explicit_local',
          'runtime_condition':'团队光环；覆盖所有职业',
          'effect_details':[{'label':'全能','source_spell_id':1126,'effect_index':1,
                             'base_value':3,'value_kind':'percentage_points','conditional':False,'evidence':'dbc_native'}]}
    result=contract.replace('bool global; };','bool global; bool shared = false; };')
    result=result.replace('const char* json; };','const char* json; bool shared = false; };')
    for marker,row in [('skill_damage_scope_effects[] = {','  {1126, 1, 404, 7, true, true},'),
                       ('skill_damage_scope_display[] = {','  {7, "", '+json.dumps(json.dumps(fact,ensure_ascii=False,separators=(',',':')),ensure_ascii=True)+', true},')]:
        end=result.index('\n};',result.index(marker));result=result[:end]+'\n'+row+result[end:]
    digest=hashlib.sha256((contract+json.dumps(fact,ensure_ascii=False,sort_keys=True)).encode()).hexdigest()
    return re.sub(r'(skill_damage_scope_sha256 = ")[a-f0-9]+',lambda m:m[1]+digest,result)

def extend_exporter(source):
    s=source.replace('fact.global && fact.family == static_cast<unsigned>(family)',
                     'fact.global && (fact.shared || fact.family == static_cast<unsigned>(family))')
    s=s.replace('if (fact.family == static_cast<unsigned>(dbc::get_class_spell_family(player->type)) &&',
                'if ((fact.shared || fact.family == static_cast<unsigned>(dbc::get_class_spell_family(player->type))) &&')
    s=s.replace('!fact.global || fact.family != static_cast<unsigned>(dbc::get_class_spell_family(player->type))',
                '!fact.global || (!fact.shared && fact.family != static_cast<unsigned>(dbc::get_class_spell_family(player->type)))')
    marker='void skill_damage_normalize_crit( player_t& player )'
    function='''// 归一化全能为零，避免默认团队属性或装备全能改变技能比较标尺。
void skill_damage_normalize_versatility( player_t& player )
{
  player.current.versatility -= player.cache.damage_versatility();
  player.invalidate_cache(CACHE_VERSATILITY);
  if (std::abs(player.cache.damage_versatility()) > 1.0e-9)
    throw std::runtime_error("Skill damage versatility normalization failed");
}

'''
    if marker not in s:raise ValueError('暴击归一化入口漂移。')
    s=s.replace(marker,function+marker)
    s=s.replace('  const auto amount = skill_damage_runtime_amount( action );',
                '  skill_damage_normalize_versatility(*action.player);\n  const auto amount = skill_damage_runtime_amount( action );')
    s=s.replace('\\\"crit_percent\\\":20,\\\"mastery_percent\\\":50}',
                '\\\"crit_percent\\\":20,\\\"mastery_percent\\\":50,\\\"versatility_percent\\\":0}')
    return s

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('source','base-contract','base-exporter','patch'):parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args()
    revision=subprocess.check_output(['git','-C',str(args.source),'rev-parse','HEAD'],text=True).strip()
    if revision!=REVIEWED_REVISION or f'"{BUILD}"' not in (args.source/'engine/dbc/generated/client_data_version.inc').read_text():raise ValueError('版本不匹配。')
    spells,effects=load_dbc(args.source);e=effects[404]
    if (e['_spell_id'],e['_index'],e['_type'],e['_subtype'],spells[1126]['_class_flags_family'])!=(1126,0,6,471,7):raise ValueError('团队全能效果结构漂移。')
    player=(args.source/'engine/player/player.cpp').read_text(encoding='utf-8')
    if 'sim->overrides.mark_of_the_wild' not in player or 'sim->auras.mark_of_the_wild->data().id()' not in player:raise ValueError('团队光环公共初始化入口漂移。')
    patch=''
    for path,base,convert in [('engine/skill_damage_scope_contract.inc',args.base_contract,lambda s:extend_shared_contract(s,effect_id=e['_id'],base_value=e['_base_value'])),
                             ('engine/sc_main.cpp',args.base_exporter,extend_exporter)]:
        old=base.read_text(encoding='utf-8');new=convert(old)
        patch+=f'diff --git a/{path} b/{path}\n'+''.join(difflib.unified_diff(old.splitlines(True),new.splitlines(True),fromfile='a/'+path,tofile='b/'+path))
    args.patch.write_text(patch,encoding='utf-8',newline='\n')
    print('跨职业野性印记排除及全能标尺补丁已生成。')

if __name__=='__main__':main()
