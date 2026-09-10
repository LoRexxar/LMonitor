// 使用最小 SimC 类型替身执行补丁中的真实分类和场景枚举函数。
#include <algorithm>
#include <cassert>
#include <iostream>
#include <map>
#include <regex>
#include <string>
#include <vector>
#include <tuple>
#include <array>
#include <cmath>
#include <set>

constexpr int WARRIOR = 1;
constexpr unsigned NUM_CLASS_FAMILY_FLAGS = 4;
enum { E_NONE=0, E_APPLY_AURA=6, E_SCHOOL_DAMAGE=2, E_APPLY_AREA_AURA_PARTY=35, E_APPLY_AURA_PLAYER_AND_PET=174 };
enum { A_MOD_DAMAGE_PERCENT_DONE=79, A_MOD_DAMAGE_FROM_CASTER=270, A_MOD_DAMAGE_PERCENT_TAKEN=87, A_MOD_DAMAGE_FROM_CASTER_SPELLS=271, A_MOD_DAMAGE_FROM_CASTER_SPELLS_LABEL=537, A_ADD_PCT_MODIFIER=108, A_ADD_PCT_LABEL_MODIFIER=218 };
enum { P_GENERIC=0, P_TICK_DAMAGE=22 };
enum school_e { SCHOOL_PHYSICAL, SCHOOL_HOLY, SCHOOL_FIRE, SCHOOL_NATURE, SCHOOL_FROST, SCHOOL_SHADOW, SCHOOL_ARCANE };
enum class spell_attribute { SX_PASSIVE };
template<class T, class U> T as(U value) { return static_cast<T>(value); }
namespace dbc { int get_class_spell_family(int value) { return value; } }
namespace util { int class_id(int value) { return value; } std::string tokenize_fn(const std::string& value) { return value; } }
struct trait_data_t {
  unsigned id_class, id_spell, id_trait_node_entry; std::string name;
  static std::vector<trait_data_t> rows;
  static const auto& data(bool) { return rows; }
};
std::vector<trait_data_t> trait_data_t::rows;

struct spelleffect_data_t {
  int type_value = E_APPLY_AURA, subtype_value = A_MOD_DAMAGE_PERCENT_DONE, schools = 127;
  double bonus = 0.2;
  unsigned mask = 0, trigger_id = 0;
  unsigned effect_index = 0; int label = 0; std::array<unsigned,4> family{};
  unsigned index() const { return effect_index; }
  double raw_base = 0; bool use_raw_base = false;
  double base_value() const { return use_raw_base ? raw_base : bonus * 100; }
  int misc_value2() const { return label; }
  int type() const { return type_value; }
  int subtype() const { return subtype_value; }
  double percent() const { return bonus; }
  unsigned class_flags(unsigned i) const { return family[i] | mask; }
  int misc_value1() const { return schools; }
  unsigned trigger_spell_id() const { return trigger_id; }
};
struct spell_data_t {
  unsigned spell_id = 0;
  bool passive = true, valid = true;
  unsigned procs = 0;
  std::vector<spelleffect_data_t> rows;
  bool ok() const { return valid; }
  unsigned id() const { return spell_id; }
  int family = WARRIOR;
  int class_family() const { return family; }
  bool flags(spell_attribute) const { return passive; }
  unsigned proc_flags() const { return procs; }
  const auto& effects() const { return rows; }
  unsigned effect_count() const { return rows.size(); }
  const auto& effectN(unsigned n) const { return rows.at(n-1); }
};
struct player_t;
struct action_t;
struct text_data_t {
  std::string description, tip;
  const char* desc() const { return description.c_str(); }
  const char* tooltip() const { return tip.c_str(); }
};
struct fake_dbc_t {
  std::map<unsigned, text_data_t> texts;
  bool is_specialization_ability(unsigned) const { return false; }
  bool is_specialization_ability(int, unsigned) const { return false; }
  const text_data_t& spell_text(unsigned id) const {
    static text_data_t empty;
    auto it = texts.find(id); return it == texts.end() ? empty : it->second;
  }
};
using dbc_t = fake_dbc_t;
struct sim_t { fake_dbc_t* dbc; };
fake_dbc_t fake_dbc;
namespace dbc {
  std::map<unsigned, spell_data_t> spells;
  const spell_data_t* find_spell(const player_t*, unsigned id) { return &spells[id]; }
  const spell_data_t* find_spell(const sim_t*, unsigned id) { return &spells[id]; }
}
struct buff_t {
  player_t *source = nullptr, *player = nullptr;
  std::string name_str;
  spell_data_t spell;
  int stacks = 1;
  double default_chance = 1.0;
  int current_stack = 0;
  double current_value = 0, default_value = .2;
  void invalidate_cache() {}
  const auto& data() const { dbc::spells[spell.id()] = spell; return spell; }
  int max_stack() const { return stacks; }
};
struct action_priority_list_t {
  struct entry_t { std::string action_; };
  std::vector<entry_t> action_list;
};
struct player_t {
  virtual ~player_t() = default;
  sim_t* sim = nullptr;
  int type = WARRIOR;
  fake_dbc_t* dbc = &fake_dbc;
  double composite_player_multiplier(school_e) const { return 1; }
  double composite_player_target_multiplier(player_t*, school_e) const { return 1; }
  std::vector<std::tuple<int, unsigned, unsigned>> player_traits;
  bool is_ptr() const { return false; }
  int specialization() const { return 1; }
  player_t* target = nullptr;
  action_t* main_action = nullptr;
  std::vector<buff_t*> buff_list;
  std::vector<action_priority_list_t*> action_priority_list;
  action_t* find_action(const std::string&) { return main_action; }
};
struct parse_player_effects_t : player_t {
  struct effect_t { buff_t* buff; unsigned opt_enum; double value; bool mastery; };
  std::vector<effect_t> player_multiplier_effects;
};
struct action_t {
  player_t *player, *target;
  std::string name_str;
};

// SOURCE_FUNCTIONS

int main() {
  // 外部职业状态即使错误地使用接收者作为 source，也不属于自身施加。
  player_t external_receiver;
  buff_t external; external.source = &external_receiver; external.player = &external_receiver;
  external.spell.family = 6; external.spell.spell_id = 10060;
  assert(!skill_damage_available_buff(external_receiver, external));
  spelleffect_data_t reduction; reduction.bonus = -0.1;
  assert(skill_damage_global_aura(reduction, false));
  reduction.mask = 1;
  assert(!skill_damage_global_aura(reduction, false));
  // DBC_FIXTURE_CASES
  // 同语法换任意 ID 仍应命中；局部技能、学校、条件分支和复合天赋不能借用全局句。
  for (const auto& text : {
      "Damage dealt increased by $s1%.", "Damage done increased by $76856s3%.",
      "Damage, healing, and critical strike chance increased by $w2%.",
      "Critical chance increased.$?a123[ Mastery increased.]?a987[ Spell damage increased by $w3%][].",
      "Your damage is increased by 20%.", "You and your pet's damage is increased by $s1%.",
      "Taking $w1% additional damage from $@auracaster.",
      "Damage taken from $@auracaster increased by $w1%.",
      "All attack and ability damage is increased by $s3%.",
      "Damage and healing increased by $w1%."}) {
    if (!skill_damage_text_declares_global_damage(text)) { std::cerr << text; return 1; }
  }
  for (const auto& text : {
      "Blood Plague damage is increased by $s1%.",
      "$?c1[Mortal Strike][Rampage] damage increased by $w1%.",
      "$?a450193[Void Blast][Smite] damage increased by $w1%.",
      "Frost damage increased by 20%.", "Damage taken reduced by 20%.",
      "Taking $w1% increased damage from $@auracaster's next Holy Power ability.",
      "Taking $w1% increased Shadowfrost damage from $@auracaster.",
      "Your next Blood Boil deals $w1% increased damage.",
      "Your damage with Fire spells is increased by 20%.",
      "Auto-attack damage increased by 20%."}) {
    if (skill_damage_text_declares_global_damage(text)) { std::cerr << text; return 2; }
  }

  spelleffect_data_t global;
  assert(skill_damage_global_aura(global, false));
  auto masked = global; masked.mask = 1;
  assert(!skill_damage_global_aura(masked, false));
  auto physical = global; physical.schools = 1;
  assert(skill_damage_global_aura(physical, false));
  auto target_effect = global; target_effect.subtype_value = A_MOD_DAMAGE_FROM_CASTER;
  assert(skill_damage_global_aura(target_effect, true));
  assert(!skill_damage_global_aura(target_effect, false));
  spell_data_t passive{42, true, true, 0, {global}};
  assert(skill_damage_pure_global_talent(&passive));
  auto mixed = passive; auto attack = global; attack.type_value = E_SCHOOL_DAMAGE;
  mixed.rows.push_back(attack);
  assert(!skill_damage_pure_global_talent(&mixed));
  auto triggered = passive; triggered.rows[0].trigger_id = 99;
  assert(!skill_damage_pure_global_talent(&triggered));
  auto active = passive; active.passive = false;
  assert(!skill_damage_pure_global_talent(&active));
  auto direct = masked; direct.subtype_value = A_ADD_PCT_MODIFIER; direct.schools = P_GENERIC;
  auto periodic = direct; periodic.schools = P_TICK_DAMAGE;
  spell_data_t declared_passive{99200, true, true, 0, {direct, periodic}};
  fake_dbc.texts[99200].description = "Increases your damage by 20%.";
  assert(skill_damage_pure_global_talent(&declared_passive, &fake_dbc));
  declared_passive.rows[1].mask = 2;
  assert(!skill_damage_pure_global_talent(&declared_passive, &fake_dbc));

  player_t player, target, other;
  buff_t disabled; disabled.default_chance = 0.0;
  assert(!skill_damage_available_buff(player, disabled));
  buff_t unselected; unselected.name_str = "foreign_spec_talent"; unselected.spell.spell_id = 909;
  trait_data_t::rows.push_back({WARRIOR, 909, 123, "foreign_spec_talent"});
  assert(!skill_damage_available_buff(player, unselected));
  buff_t selected = unselected;
  player.player_traits.emplace_back(0, 123, 1);
  assert(skill_damage_available_buff(player, selected));
  buff_t ordinary; ordinary.spell.spell_id = 910;
  assert(skill_damage_available_buff(player, ordinary));
  player.target = &target;
  action_t action{&player, &target, "attack"};
  player.main_action = &action;
  // 全局状态故意超过原枚举上限，验证在展开前已被剔除。
  buff_t avatar{&player, &player, "avatar", {107574, false, true, 0, {global}}, 5000};
  auto zero = masked; zero.subtype_value = A_ADD_PCT_MODIFIER; zero.schools = P_GENERIC; zero.bonus = 0;
  buff_t enrage{&player, &player, "arbitrary_dynamic", {99101, false, true, 0, {zero}}, 5000};
  fake_dbc.texts[99101].tip = "Damage done increased by $w1%.";
  auto caster_spells = masked; caster_spells.subtype_value = A_MOD_DAMAGE_FROM_CASTER_SPELLS;
  buff_t vulnerability{&player, &target, "arbitrary_target", {99102, false, true, 0, {caster_spells}}, 5000};
  fake_dbc.texts[99102].tip = "Taking $w1% increased damage from $@auracaster.";
  // 单技能 buff 的上游天赋还包含全局效果，不能沿整段描述串台。
  fake_dbc.texts[99103] = {"Enemies take 20% more damage from you and Blood Plague damage is increased.", "Blood Plague damage is increased by 20%."};
  spell_data_t local_spell{99103, false, true, 0, {zero}};
  assert(!skill_damage_declared_global_spell(player, local_spell, false));
  fake_dbc.texts[99104].tip = "Damage done increased by $99105s3%.";
  dbc::spells[99105] = {99105, false, true, 0, {zero, zero, zero}};
  spell_data_t referenced_spell{99104, false, true, 0, {}};
  assert(skill_damage_declared_global_spell(player, referenced_spell, false));
  fake_dbc.texts[99106] = {"$@spelldesc99107", ""};
  fake_dbc.texts[99107].description = "$@spelldesc99106";
  spell_data_t cycle{99106, false, true, 0, {zero}};
  assert(!skill_damage_declared_global_spell(player, cycle, false));
  player.buff_list = {&avatar, &enrage};
  target.buff_list = {&vulnerability};
  assert(skill_damage_scenarios(action).empty());
  assert(skill_damage_excluded_buffs(player).size() == 3);
  auto foreign = avatar; foreign.source = &other;
  assert(!skill_damage_excluded_global_buff(player, &foreign, false));

  // 其他职业可通过原生玩家乘区声明全局效果，不依赖战士名称或 buff 自身 aura。
  parse_player_effects_t caster;
  caster.type = 8;
  buff_t scripted{&caster, &caster, "scripted_global", {88, false, true, 0, {}}, 5000};
  scripted.spell.family = caster.type;
  caster.player_multiplier_effects.push_back({&scripted, 127, 0.25, false});
  assert(skill_damage_excluded_global_buff(caster, &scripted, false));
  caster.player_multiplier_effects[0].opt_enum = 4;
  skill_damage_global_scope_cache.clear();
  // 全火焰伤害仍属于全局作用域。
  assert(skill_damage_excluded_global_buff(caster, &scripted, false));

  buff_t local{&player, &player, "local", {77, false, true, 0, {masked}}, 2};
  buff_t whirlwind{&player, &player, "whirlwind", {85739, false, true, 0, {}}, 4};
  player.buff_list.push_back(&local);
  player.buff_list.push_back(&whirlwind);
  action_priority_list_t apl{{{"attack,if=buff.avatar.up&buff.local.up&buff.whirlwind.up"}}};
  player.action_priority_list.push_back(&apl);
  auto scenarios = skill_damage_scenarios(action);
  // 局部两层、顺劈启用以及二者的两种组合，共五个有效场景。
  assert(scenarios.size() == 5);
  for (const auto& scenario : scenarios) for (const auto& condition : scenario) {
    assert(condition.buff == &local || condition.buff == &whirlwind);
    if (condition.buff == &whirlwind) assert(condition.stacks == 1);
  }
  // 后创建的目标效果仍会进入排除集合，不受首次缓存时机影响。
  buff_t lazy{&player, &target, "lazy", {88, false, true, 0, {target_effect}}, 1};
  target.buff_list.push_back(&lazy);
  assert(skill_damage_excluded_buffs(player).size() == 4);
  std::cout << "C++ 分类、前置剪枝、局部组合、顺劈状态和延迟效果断言通过。\n";
}
