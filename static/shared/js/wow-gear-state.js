/* 刷新分支当前资料，保留装备选择、锁定部位和外部装备。 */
(() => {
  "use strict";
  async function refresh(state, request) {
    const reference = (entry) => entry?.item?.item_id && entry?.variant?.key
      ? [Number(entry.item.item_id), String(entry.variant.key)] : 0;
    const entries = Object.entries(state.equipment || {}).filter(([, entry]) => entry && !entry.external && reference(entry)
      && [entry.embellishment, ...(entry.gems || []), entry.enchant].filter(Boolean).every(reference));
    if (!entries.length) return state;
    const payload = await request("/portal/api/gear-builder/resolve-share/", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({v: 4, c: state.className, s: state.specName, b: state.batchKey || '', current: true,
        e: entries.map(([slot, entry]) => [slot, Number(entry.item.item_id), String(entry.variant.key),
          entry.selectedStats || [], reference(entry.embellishment), (entry.gems || []).map(reference),
          reference(entry.enchant), entry.addedSocket ? 1 : 0])}),
    });
    return {...state, batchKey: payload.batch_key || state.batchKey,
      equipment: {...state.equipment, ...(payload.equipment || {})}};
  }
  globalThis.WowGearState = {refresh};
})();
