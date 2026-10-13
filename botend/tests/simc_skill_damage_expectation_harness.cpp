#include <algorithm>
#include <cassert>
#include <cmath>
#include <stdexcept>
#include <string_view>

// 只替代外部类型；归一化与目标暴击计算函数从已应用补丁的真实源码提取。
enum result_amount_type { DMG_DIRECT, DMG_OVER_TIME };
enum { RESULT_HIT, RESULT_CRIT, CACHE_ATTACK_CRIT_CHANCE, CACHE_SPELL_CRIT_CHANCE };
struct dbc_t { double all_crit_base(int, int) { return 0.05; } };
struct action_t;
struct player_t {
  dbc_t data, *dbc = &data;
  int type = 1, invalidations = 0;
  double all_crit_multiplier = 1, spell_crit_multiplier = 1, spell_crit_flat = 0;
  struct {
    double armor_coeff = 3430;
    struct { double crit_rating = 500; } stats;
    double attack_crit_chance = 0.15, spell_crit_chance = 0.10;
  } current;
  int level() { return 90; }
  void invalidate_cache(int) { ++invalidations; }
  double get_passive_player_value(double value, std::string_view field) {
    return field == "all_crit" ? value * all_crit_multiplier : (value + spell_crit_flat) * spell_crit_multiplier;
  }
};
struct action_state_t {
  action_t* action = nullptr;
  double target_armor = 0;
  inline static int releases = 0;
  player_t* target = nullptr;
  unsigned n_targets = 0, chain_target = 0;
  int result = RESULT_HIT;
  double attack_power = 0, spell_power = 0, chance = 0, result_total = 0;
  double composite_crit_chance() { return chance; }
  static void release(action_state_t* state) { ++releases; delete state; }
};
struct action_t {
  player_t owner, *player = &owner;
  double armor = 0;
  bool may_crit = true, tick_may_crit = true, fail = false;
  int snapshots = 0, direct_calls = 0, tick_calls = 0, bonus_calls = 0;
  double extra_chance = 0;
  action_state_t* get_state() { return new action_state_t; }
  void snapshot_state(action_state_t* state, result_amount_type) {
    state->action = this;
    state->target_armor = armor;
    ++snapshots;
    state->chance = (state->chain_target == 0 ? 0.2 : 0.8) + extra_chance;
  }
  double calculate_direct_amount(action_state_t* state) {
    ++direct_calls;
    if (fail) throw std::runtime_error("测试计算异常");
    assert(state->attack_power == 100 && state->spell_power == 100);
    return state->result_total = state->chain_target == 0 ? 100 : 50;
  }
  double calculate_crit_damage_bonus(action_state_t* state) {
    ++bonus_calls;
    if (state->result == RESULT_CRIT) state->result_total *= state->chain_target == 0 ? 2 : 3;
    return state->result_total;
  }
  double calculate_tick_amount(action_state_t* state, double) {
    ++tick_calls;
    state->result_total = 10;
    return calculate_crit_damage_bonus(state);
  }
};
void skill_damage_reset_probe_rng(action_t&) {}

// SOURCE_FUNCTIONS

void close(double actual, double expected) { assert(std::abs(actual - expected) < 1e-9); }
int main() {
  player_t player;
  skill_damage_normalize_crit(player);
  close(player.current.attack_crit_chance, 0.30);
  close(player.current.spell_crit_chance, 0.25);
  close(player.current.stats.crit_rating, 0);
  assert(player.invalidations == 2);

  // 被动中既有加法也有乘法时，替换原始基础值后保留两种修正和职业额外值。
  player_t scaled;
  scaled.all_crit_multiplier = 1.2;
  scaled.spell_crit_flat = 0.02;
  scaled.spell_crit_multiplier = 1.5;
  scaled.current.attack_crit_chance = 0.07;
  scaled.current.spell_crit_chance = 0.13;
  skill_damage_normalize_crit(scaled);
  close(scaled.current.attack_crit_chance, 0.25);
  close(scaled.current.spell_crit_chance, 0.40);

  action_t action;
  auto first = skill_damage_target_amount(action, &player, 2, 0, false);
  auto second = skill_damage_target_amount(action, &player, 2, 1, false);
  close(first.hit + second.hit, 150);
  close(first.crit + second.crit, 350);
  close(first.noncrit_contribution + second.noncrit_contribution, 90);
  close(first.crit_contribution + second.crit_contribution, 160);
  // 目标一为 20% 暴击，目标二为 80% 暴击，期望必须分别加权。
  close(first.noncrit_contribution + first.crit_contribution + second.noncrit_contribution + second.crit_contribution, 250);
  assert(action.snapshots == 2 && action.direct_calls == 4 && action.bonus_calls == 2);

  auto tick = skill_damage_target_amount(action, &player, 2, 1, true);
  close(tick.noncrit_contribution + tick.crit_contribution, 26);
  assert(action.snapshots == 3 && action.tick_calls == 2);

  action.may_crit = false;
  auto normal = skill_damage_target_amount(action, &player, 1, 0, false);
  close(normal.hit, 100); close(normal.crit, 100);
  close(normal.crit_contribution, 0);
  assert(action.direct_calls == 5);

  action.may_crit = true;
  action.extra_chance = 2;
  auto guaranteed = skill_damage_target_amount(action, &player, 1, 0, false);
  close(guaranteed.noncrit_contribution, 0); close(guaranteed.crit_contribution, 200);
  action.extra_chance = -2;
  auto zero = skill_damage_target_amount(action, &player, 1, 0, false);
  close(zero.noncrit_contribution, 100); close(zero.crit_contribution, 0);

  action.fail = true;
  bool thrown = false;
  try { skill_damage_target_amount(action, &player, 1, 0, false); }
  catch (const std::runtime_error&) { thrown = true; }
  assert(thrown && action_state_t::releases == 7);

  action.fail = false;
  action.extra_chance = 0;
  action.armor = 1470;
  auto physical = skill_damage_target_amount(action, &player, 1, 0, false);
  close(physical.hit, 70); close(physical.crit, 140);
  close(physical.noncrit_contribution + physical.crit_contribution, 84);
  // 周期分量即便传入残留护甲快照，也不能被护甲削减。
  auto bleed = skill_damage_target_amount(action, &player, 1, 0, true);
  close(bleed.hit, 10); close(bleed.crit, 20);
  // 法术或忽略护甲技能的原生快照中 target_armor 为零。
  action.armor = 0;
  auto ignored = skill_damage_target_amount(action, &player, 1, 0, false);
  close(ignored.hit, 100); close(ignored.crit, 200);
  // 破甲改变护甲值后必须重算，不能固定乘 0.7。
  action.armor = 735;
  auto reduced = skill_damage_target_amount(action, &player, 1, 0, false);
  close(reduced.hit, 100 * 3430.0 / 4165.0);
  // 极高护甲沿用原生 85% 上限。
  action.armor = 1e9;
  close(skill_damage_target_amount(action, &player, 1, 0, false).hit, 15);
  action.may_crit = false;
  action.armor = 1470;
  auto no_crit = skill_damage_target_amount(action, &player, 1, 0, false);
  close(no_crit.hit, 70); close(no_crit.crit, 70); close(no_crit.crit_contribution, 0);
}
