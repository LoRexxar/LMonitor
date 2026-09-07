// 使用最小 SimC 类型替身执行补丁中的真实分类和场景枚举函数。
#include <algorithm>
#include <cassert>
#include <iostream>
#include <map>
#include <regex>
#include <string>
#include <vector>
#include <tuple>

constexpr int WARRIOR = 1;
constexpr unsigned NUM_CLASS_FAMILY_FLAGS = 4;
enum { E_NONE, E_APPLY_AURA, E_SCHOOL_DAMAGE };
enum { A_MOD_DAMAGE_PERCENT_DONE, A_MOD_DAMAGE_FROM_CASTER, A_MOD_DAMAGE_PERCENT_TAKEN };
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
  int type() const { return type_value; }
  int subtype() const { return subtype_value; }
  double percent() const { return bonus; }
  unsigned class_flags(unsigned) const { return mask; }
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
  int class_family() const { return WARRIOR; }
  bool flags(spell_attribute) const { return passive; }
  unsigned proc_flags() const { return procs; }
  const auto& effects() const { return rows; }
};
struct player_t;
struct action_t;
struct buff_t {
  player_t *source = nullptr, *player = nullptr;
  std::string name_str;
  spell_data_t spell;
  int stacks = 1;
  double default_chance = 1.0;
  const auto& data() const { return spell; }
  int max_stack() const { return stacks; }
};
struct action_priority_list_t {
  struct entry_t { std::string action_; };
  std::vector<entry_t> action_list;
};
struct player_t {
  virtual ~player_t() = default;
  int type = WARRIOR;
  std::vector<std::tuple<int, unsigned, unsigned>> player_traits;
  bool is_ptr() const { return false; }
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
  spelleffect_data_t global;
  assert(skill_damage_global_aura(global, false));
  auto masked = global; masked.mask = 1;
  assert(!skill_damage_global_aura(masked, false));
  auto physical = global; physical.schools = 1;
  assert(!skill_damage_global_aura(physical, false));
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
  buff_t enrage{&player, &player, "enrage", {184362, false, true, 0, {}}, 5000};
  buff_t vulnerability{&player, &target, "colossus_smash", {208086, false, true, 0, {}}, 5000};
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
  caster.player_multiplier_effects.push_back({&scripted, 127, 0.25, false});
  assert(skill_damage_excluded_global_buff(caster, &scripted, false));
  caster.player_multiplier_effects[0].opt_enum = 4;
  assert(!skill_damage_excluded_global_buff(caster, &scripted, false));

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
