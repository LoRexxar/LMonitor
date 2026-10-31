/* Authoritative browser + Node skill query semantics, extracted from the renderer. */
(function(root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    else root.SimcSkillQuery = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function() {
'use strict';
const PRESENTATION_VERSION = 'actor_page_v1';
const hasFiniteSimcSkillDamageNumber = value => typeof value === 'number' && Number.isFinite(value);
    const renderSimcTalentProbeCondition = (runtimeCondition, scenarioTokens, talentName, runtimeConditions = [], preserveRuntimeCondition = false) => {
        const condition = String(runtimeCondition || '').trim();
        const tokens = [...(Array.isArray(scenarioTokens) ? scenarioTokens : []),
            ...runtimeConditions.map(item => item && item.token).filter(Boolean)];
        const name = String(talentName || '').trim();
        // Legacy scenario markers are rendered below from localized state facts.
        const parts = condition && !/^启用 (?:buff|debuff)\.[a-z0-9_]+$/.test(condition)
            && (preserveRuntimeCondition || !condition.startsWith('启用 ')) ? [condition] : [];
        const talentLabel = name.endsWith('天赋') ? name : `${name}天赋`;
        // Talent selection and runtime state are independent conditions.
        // A health/Buff label must not suppress the talent whose formula this is.
        const selectedTalent = `点出${talentLabel}`;
        if (name && name !== '基础技能' && !preserveRuntimeCondition
            && !parts.some(part => part.includes(selectedTalent))) {
            parts.unshift(selectedTalent);
        } else if (!parts.length && tokens.length && name && name !== '基础技能') {
            parts.push(selectedTalent);
        }
        [...new Set(tokens.map(token => String(token || '').trim()).filter(Boolean))].forEach(token => {
            const separatorIndex = token.indexOf('.');
            const scope = separatorIndex >= 0 ? token.slice(0, separatorIndex) : '';
            const stateToken = separatorIndex >= 0 ? token.slice(separatorIndex + 1) : token;
            if (!stateToken) return;
            const metadata = runtimeConditions.find(item => item && item.token === token) || {};
            const stateName = metadata.display_name || metadata.name_zh || metadata.name || stateToken;
            const owner = scope === 'debuff' ? '目标' : '自身';
            const stacks = Number(metadata.stacks);
            const stackLabel = Number.isInteger(stacks) && stacks > 1 ? `（${stacks}层）` : '';
            const stateLabel = `${stateName}${stackLabel}`;
            if (stackLabel) {
                const escapePattern = value => String(value).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
                const statePattern = new RegExp(`(${owner}存在\\s*)(?:${escapePattern(stateName)}|${escapePattern(stateToken)})(\\s*效果)`, 'g');
                parts.forEach((part, index) => { parts[index] = part.replace(statePattern, `$1${stateLabel}$2`); });
            }
            // 兼容服务端已写入状态说明的快照，不能重复追加同一个 Buff。
            const compact = parts.join('，').replace(/\s+/g, '');
            if (!compact.includes(`${owner}存在${stateLabel}效果`)
                && !compact.includes(`${owner}存在${stateName}效果`)
                && !compact.includes(`${owner}存在${stateToken}效果`)) {
                parts.push(`${owner}存在 ${stateLabel} 效果时`);
            }
        });
        if (condition.includes('35%') && !parts.some(part => part.includes('35%'))) parts.push('血量低于35%');
        return parts.join('，');
    };

function createFacets() {
 return {talentConditions:new Map(),buffConditions:new Map(),talentConditionDescriptions:new Map(),buffConditionDescriptions:new Map()};
}
function scanCandidate(action, options, facets) {
 const targetCount=String(options.target_count), selectedHeroTree=String(options.hero_tree_id);
    const talentConditions = facets.talentConditions;
    const buffConditions = facets.buffConditions;
    const talentConditionDescriptions = facets.talentConditionDescriptions;
    const buffConditionDescriptions = facets.buffConditionDescriptions;
    if (!action || typeof action !== 'object' || action.player_skill === false) return null;
    const variant = action.variant && typeof action.variant === 'object' ? action.variant : {};
    // 使用导出器按完整施法验证的目标数，条件选项与伤害行保持一致。
    if (Array.isArray(action.affected_target_counts)
        && !action.affected_target_counts.some(count => String(count) === targetCount)) return;
    // DB2 uses 0 for ordinary class/spec talents, not a hero tree.
    if (variant.hero_subtree_id != null && Number(variant.hero_subtree_id) !== 0
        && String(variant.hero_subtree_id) !== selectedHeroTree) return;
    const heroSubtreeIds = Array.isArray(action.hero_subtree_ids) ? action.hero_subtree_ids : [];
    if (heroSubtreeIds.length && !heroSubtreeIds.some(id => String(id) === selectedHeroTree)) return;

    const conditionKeys = [];
    if (variant.talent_id != null && Number(variant.talent_id) > 0) {
        const conditionKey = `talent:${variant.talent_id}`;
        const talentLabel = variant.talent_name_zh || variant.talent_name || `天赋 ${variant.talent_id}`;
        conditionKeys.push(conditionKey);
        if (!talentConditions.has(conditionKey)) talentConditions.set(conditionKey, String(talentLabel));
        const talentDescription = variant.talent_description_zh || variant.talent_description || '';
        if (talentDescription && !talentConditionDescriptions.has(conditionKey)) {
            talentConditionDescriptions.set(conditionKey, talentDescription);
        }
    }
    const runtimeConditions = Array.isArray(variant.runtime_conditions)
        ? variant.runtime_conditions.filter(condition => condition && typeof condition === 'object')
        : [];
    runtimeConditions.forEach(condition => {
        const scope = String(condition.scope || '');
        const spellId = Number(condition.spell_id);
        const token = String(condition.token || '').trim();
        const stacksValue = Number(condition.stacks);
        const stacks = Number.isInteger(stacksValue) && stacksValue > 0 ? stacksValue : 1;
        if (!token && !(Number.isInteger(spellId) && spellId > 0)) return;
        const stackValues = Array.isArray(condition.stack_values) ? condition.stack_values : [];
        const conditionKey = `state:${JSON.stringify([
            token,
            scope,
            Number.isInteger(spellId) && spellId > 0 ? spellId : 0,
            stacks, stackValues,
        ])}`;
        const fallbackToken = token.includes('.') ? token.slice(token.indexOf('.') + 1) : token;
        const conditionName = condition.name_zh || condition.name || fallbackToken || spellId;
        const stackLabel = stackValues.length > 1
            ? `（${stackValues.join('/')}层等伤害）`
            : (stacks > 1 ? `（${stacks}层）` : '');
        const conditionLabel = `${['target', 'debuff'].includes(scope) ? '目标' : '自身'}：${conditionName}${stackLabel}`;
        if (!conditionKeys.includes(conditionKey)) conditionKeys.push(conditionKey);
        if (!buffConditions.has(conditionKey)) buffConditions.set(conditionKey, String(conditionLabel));
        const conditionDescription = condition.description_zh || condition.description || '';
        if (conditionDescription && !buffConditionDescriptions.has(conditionKey)) {
            buffConditionDescriptions.set(conditionKey, conditionDescription);
        }
    });
    return {action, variant, rowConditionKeys: conditionKeys};

}
function matchCandidate(candidate, options, ordinal) {
 const {action,rowConditionKeys}=candidate;
 const query=String(options.q || '').trim().toLowerCase(), targetCount=String(options.target_count);
 const excludedFilterKeys=options.exclude instanceof Set ? options.exclude : new Set(options.exclude || []);
        const variant = action.variant && typeof action.variant === 'object' ? action.variant : {};
        const conditionLabel = renderSimcTalentProbeCondition(variant.runtime_condition, variant.scenario_tokens,
            variant.talent_name_zh || variant.talent_name, Array.isArray(variant.runtime_conditions) ? variant.runtime_conditions : []);
        const haystack = `${action.display_name || ''} ${action.name || ''} ${action.spell_id || ''} ${variant.talent_name || ''} ${variant.talent_name_zh || ''} ${conditionLabel}`.toLowerCase();
        if (query && !haystack.includes(query)) return null;
        if (rowConditionKeys.some(key => excludedFilterKeys.has(key))) return null;
        const product = action.product && typeof action.product === 'object' ? action.product : {};
        const damageByTarget = product.final_normalized_damage_by_target;
        const selectedFinalDamage = targetCount === '1'
            ? product.final_normalized_damage
            : (damageByTarget && typeof damageByTarget === 'object' ? damageByTarget[targetCount] : null);
        const finalSortValue = hasFiniteSimcSkillDamageNumber(selectedFinalDamage)
            ? selectedFinalDamage
            : Number.NEGATIVE_INFINITY;
        return {action, product, selectedFinalDamage, finalSortValue, sourceIndex: ordinal};

}
function compareRows(left, right, options) {
 const sortMode=options.sort, sortDirection=options.direction;
        const leftName = String(left.action.display_name || left.action.name || '');
        const rightName = String(right.action.display_name || right.action.name || '');
        const nameDelta = leftName.localeCompare(rightName, 'zh-CN', {numeric: true, sensitivity: 'base'});
        if (sortMode === 'name' && nameDelta) {
            return sortDirection === 'asc' ? nameDelta : -nameDelta;
        }
        if (sortMode === 'final') {
            const damageDelta = left.finalSortValue - right.finalSortValue;
            if (damageDelta) return sortDirection === 'asc' ? damageDelta : -damageDelta;
        }
        if (nameDelta) return nameDelta;
        return left.sourceIndex - right.sourceIndex;

}
function cursorFor(row) {
 return {name:String(row.action.display_name || row.action.name || ''), final:Number.isFinite(row.finalSortValue) ? row.finalSortValue : null, ordinal:row.sourceIndex};
}
function cursorRow(cursor) {
 return {action:{display_name:cursor.name}, finalSortValue:cursor.final === null ? -Infinity : cursor.final, sourceIndex:cursor.ordinal};
}
function serializeFacets(facets) {
 const entries=(map,descriptions)=>Array.from(map,([key,label])=>({key,label,description:descriptions.get(key)||''}));
 return {talents:entries(facets.talentConditions,facets.talentConditionDescriptions),buffs:entries(facets.buffConditions,facets.buffConditionDescriptions)};
}
return {PRESENTATION_VERSION,renderSimcTalentProbeCondition,createFacets,scanCandidate,matchCandidate,compareRows,cursorFor,cursorRow,serializeFacets};
});
