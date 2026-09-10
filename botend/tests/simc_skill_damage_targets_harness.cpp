#include <algorithm>
#include <array>
#include <cassert>
#include <cmath>
#include <map>
#include <stdexcept>
#include <vector>

// 外部类型替身只提供接口，目标选择和期望聚合使用实际导出器函数。
template<class T, class U> T as(U value) { return static_cast<T>(value); }
enum result_amount_type { DMG_DIRECT, DMG_OVER_TIME };
enum { RESULT_HIT, RESULT_CRIT };
struct player_t { double multiplier = 1, chance = 0.2; };
struct target_vector {
  std::vector<player_t*> values;
  const auto& data() const { return values; }
  void assign_without_callbacks(const std::vector<player_t*>& next) { values = next; }
};
struct sim_t {
  int desired_targets = 1, active_enemies = 1;
  target_vector target_non_sleeping_list;
};
struct action_state_t {
  player_t* target = nullptr;
  unsigned n_targets = 0, chain_target = 0;
  int result = RESULT_HIT;
  double attack_power = 0, spell_power = 0, result_total = 0;
  double composite_crit_chance() { return target->chance; }
  static void release(action_state_t* state) { delete state; }
};
struct action_t {
  sim_t* sim;
  player_t* target;
  player_t* fail_target = nullptr;
  bool may_crit = true, tick_may_crit = true;
  int snapshots = 0;
  bool secondary_only = false;
  struct { bool is_valid = true; } target_cache;
  int n_targets() { return 3; }
  std::vector<player_t*> target_list() {
    assert(!target_cache.is_valid);
    target_cache.is_valid = true;
    auto targets = sim->target_non_sleeping_list.data();
    if (secondary_only) targets.erase(std::remove(targets.begin(), targets.end(), target), targets.end());
    return targets;
  }
  action_state_t* get_state() { return new action_state_t; }
  void snapshot_state(action_state_t*, result_amount_type) {
    assert(sim->active_enemies == sim->desired_targets);
    ++snapshots;
  }
  double calculate_direct_amount(action_state_t* state) {
    if (state->target == fail_target) throw std::runtime_error("测试目标异常");
    return state->result_total = 100 * state->target->multiplier;
  }
  double calculate_crit_damage_bonus(action_state_t* state) {
    if (state->result == RESULT_CRIT) state->result_total *= 2;
    return state->result_total;
  }
  double calculate_tick_amount(action_state_t* state, double) {
    state->result_total = 10 * state->target->multiplier;
    return calculate_crit_damage_bonus(state);
  }
};
struct skill_damage_amount_t {
  struct component_t {
    double hit = 0, crit = 0, crit_chance = 0;
    bool single_target_eligible = true;
    std::map<int,double> target_hit, target_crit, target_expected;
    std::map<int,double> target_noncrit_contribution, target_crit_contribution;
  } direct_amount, tick_amount;
  bool direct = true, periodic = true;
};
std::vector<player_t*> skill_damage_probe_targets;
void skill_damage_reset_probe_rng(action_t&) {}

// SOURCE_FUNCTIONS

void close(double actual, double expected) { assert(std::abs(actual - expected) < 1e-9); }
int main() {
  std::array<player_t,20> targets;
  // 只有主目标有增伤和额外暴击减益，其他目标保持独立。
  targets[0].multiplier = 2;
  targets[0].chance = 0.6;
  for (auto& target : targets) skill_damage_probe_targets.push_back(&target);
  sim_t sim;
  sim.target_non_sleeping_list.values = {&targets[0]};
  action_t action{&sim, &targets[0]};
  skill_damage_amount_t amount;
  amount.direct_amount.hit = 200; amount.direct_amount.crit = 400; amount.direct_amount.crit_chance = 0.6;
  amount.tick_amount.hit = 20; amount.tick_amount.crit = 40; amount.tick_amount.crit_chance = 0.6;
  skill_damage_populate_target_scenarios(action, amount);
  close(amount.direct_amount.target_expected[1], 320);
  close(amount.direct_amount.target_expected[2], 440);
  close(amount.direct_amount.target_expected[5], 560);
  close(amount.direct_amount.target_expected[20], 560);
  close(amount.tick_amount.target_expected[2], 44);
  close(amount.tick_amount.target_expected[20], 56);
  assert(action.snapshots == 22);
  assert(sim.active_enemies == 1 && sim.desired_targets == 1);
  assert(sim.target_non_sleeping_list.values == std::vector<player_t*>{&targets[0]});
  assert(!action.target_cache.is_valid);

  // 第一滴血这类分量只命中副目标：单目标必须为零，双目标仍有伤害。
  action.secondary_only = true;
  skill_damage_populate_target_scenarios(action, amount);
  assert(!amount.direct_amount.single_target_eligible);
  close(amount.direct_amount.target_expected[1], 0);
  close(amount.direct_amount.target_expected[2], 120);
  action.secondary_only = false;

  action.fail_target = &targets[1];
  bool thrown = false;
  try { skill_damage_populate_target_scenarios(action, amount); }
  catch (const std::runtime_error&) { thrown = true; }
  assert(thrown && sim.active_enemies == 1 && sim.desired_targets == 1);
  assert(sim.target_non_sleeping_list.values == std::vector<player_t*>{&targets[0]});
  assert(!action.target_cache.is_valid);
}
