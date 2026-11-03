"""全局状态不构成下表的伤害维度，原生技能触发前提独立保留。"""


def global_damage_state_identities(actor, effects=()):
    """使用原生状态声明，不用名字名单或数值相等推断作用域。"""
    states = list(actor.get('global_damage_states') or [])
    for effect in effects:
        if isinstance(effect, dict) and effect.get('scope_evidence') == 'declared_global_damage_state':
            states.extend(effect.get('runtime_conditions') or [])
    return {
        (state.get('token'), state.get('scope'), state.get('spell_id'))
        for state in states if isinstance(state, dict) and state.get('token')
        and state.get('scope') in {'self', 'target'} and type(state.get('spell_id')) is int
        and state['spell_id'] > 0
    }


def uses_global_damage_scenario(row, identities):
    """排除整条实算场景，禁止删掉状态文字后继续使用其加成数值。"""
    variant = row.get('variant') or {}
    scenario_tokens = set(variant.get('scenario_tokens') or [])
    conditions = [state for state in variant.get('runtime_conditions') or [] if isinstance(state, dict)]
    physical = {(scope, spell_id) for _, scope, spell_id in identities}
    global_tokens = {identity[0] for identity in identities}
    for token in scenario_tokens:
        metadata = [state for state in conditions if state.get('token') == token]
        if any((state.get('scope'), state.get('spell_id')) in physical for state in metadata):
            return True
        if not metadata and token in global_tokens:
            return True
    required = {
        (state.get('token'), state.get('scope'), state.get('spell_id'))
        for state in variant.get('activation_conditions') or [] if isinstance(state, dict)
    }
    return any(
        (state.get('scope'), state.get('spell_id')) in physical
        and (state.get('token'), state.get('scope'), state.get('spell_id')) not in required
        for state in conditions
    )
