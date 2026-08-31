(function () {
  "use strict";

  const container = document.querySelector(".wow-skill-diff-embedded-html");
  if (!container || container.querySelector(".skill-report")) return;

  const report = container.querySelector(":scope > .card");
  if (!report) return;

  report.classList.add("legacy-skill-report-enhanced");

  const numericValue = (value) => {
    const normalized = String(value || "").trim().replace(/,/g, "").replace(/%$/, "");
    if (!normalized) return null;
    const number = Number(normalized);
    return Number.isFinite(number) ? number : null;
  };

  const impactDimension = (label) => {
    const text = String(label || "");
    if (/PvP系数|PvpMultiplier/i.test(text)) return "PvP 强度";
    if (/法强系数|EffectBonusCoefficient/i.test(text)) return "法术强度收益";
    if (/攻强系数|BonusCoefficientFromAP/i.test(text)) return "攻击强度收益";
    if (/基础数值|BasePoints/i.test(text)) return "基础效果数值";
    if (/冷却|RecoveryTime/i.test(text)) return "冷却节奏";
    if (/施法时间|CastingTime/i.test(text)) return "施法节奏";
    if (/周期|Amplitude|AuraPeriod/i.test(text)) return "周期效果间隔";
    if (/打断|中断|InterruptFlags/i.test(text)) return "效果中断条件";
    if (/持续时间|Duration/i.test(text)) return "持续时间";
    if (/距离|Range/i.test(text)) return "施法距离";
    if (/资源|PowerCost|ManaCost/i.test(text)) return "资源消耗";
    if (/描述|Description/i.test(text)) return "技能说明与机制";
    if (/名称|Name/i.test(text)) return "技能名称";
    if (/天赋|Trait/i.test(text)) return "天赋关联";
    return "技能机制";
  };

  const fieldLabelBefore = (element, fallback) => {
    const text = element.previousSibling?.textContent || "";
    const segment = text.split(/[，,（(]/).pop()?.replace(/[：:]\s*$/, "").trim();
    return segment || fallback || "字段变化";
  };

  const changeTone = (label, before, after) => {
    const oldNumber = numericValue(before);
    const newNumber = numericValue(after);
    if (oldNumber === null || newNumber === null || oldNumber === newNumber) {
      return { tone: "mechanic", label: "机制调整", delta: "" };
    }

    const positive = /系数|BasePoints|基础数值|ProcChance|触发几率|PvpMultiplier|PvP系数/i.test(label);
    const inverse = /冷却|RecoveryTime|施法时间|CastingTime|资源消耗|PowerCost/i.test(label);
    const difference = newNumber - oldNumber;
    let tone = "mechanic";
    if (positive) tone = difference > 0 ? "buff" : "nerf";
    if (inverse) tone = difference > 0 ? "nerf" : "buff";

    let delta = "";
    if (Math.abs(oldNumber) > 1e-12) {
      const percent = (difference / Math.abs(oldNumber)) * 100;
      if (Math.abs(percent) >= 0.05) {
        delta = `${percent > 0 ? "+" : ""}${Math.abs(percent) >= 10 ? percent.toFixed(0) : percent.toFixed(1)}%`;
      }
    }
    return {
      tone,
      label: tone === "buff" ? "增强" : tone === "nerf" ? "削弱" : "数值调整",
      delta,
    };
  };

  const spellTone = (tones) => {
    if (tones.has("buff") && tones.has("nerf")) return "mixed";
    if (tones.has("buff")) return "buff";
    if (tones.has("nerf")) return "nerf";
    return "mechanic";
  };

  const toneLabel = {
    buff: "增强",
    nerf: "削弱",
    mixed: "有增有减",
    mechanic: "机制调整",
  };

  report.querySelectorAll(".spell").forEach((spell) => {
    const lines = Array.from(spell.querySelectorAll(":scope > .line"));
    const impactList = document.createElement("div");
    impactList.className = "legacy-impact-list";
    const tones = new Set();

    lines.forEach((line) => {
      const oldValues = Array.from(line.querySelectorAll(".del"));
      const newValues = Array.from(line.querySelectorAll(".ins"));
      oldValues.forEach((oldValue, index) => {
        const newValue = newValues[index];
        if (!newValue) return;
        const fieldLabel = fieldLabelBefore(oldValue, line.querySelector(".k")?.textContent);
        const direction = changeTone(fieldLabel, oldValue.textContent, newValue.textContent);
        const dimension = impactDimension(fieldLabel);
        tones.add(direction.tone);

        const row = document.createElement("div");
        row.className = `legacy-impact-row ${direction.tone}`;
        row.innerHTML = `
          <div>
            <span class="legacy-impact-label"></span>
            <span class="legacy-impact-evidence"></span>
          </div>
          <div class="legacy-value-flow">
            <span class="legacy-old-value"></span>
            <span class="legacy-change-arrow">→</span>
            <span class="legacy-new-value"></span>
            <span class="legacy-delta-badge ${direction.tone}"></span>
          </div>`;
        row.querySelector(".legacy-impact-label").textContent = dimension;
        row.querySelector(".legacy-impact-evidence").textContent = fieldLabel;
        row.querySelector(".legacy-old-value").textContent = oldValue.textContent.trim() || "空";
        row.querySelector(".legacy-new-value").textContent = newValue.textContent.trim() || "空";
        row.querySelector(".legacy-delta-badge").textContent = direction.delta || direction.label;
        impactList.appendChild(row);
      });
    });

    if (!impactList.children.length) {
      const row = document.createElement("div");
      row.className = "legacy-impact-row mechanic";
      row.innerHTML = "<div><span class='legacy-impact-label'>技能机制</span><span class='legacy-impact-evidence'>当前字段无法可靠换算为直接强弱</span></div><div class='legacy-value-flow'><span class='legacy-delta-badge mechanic'>需实战验证</span></div>";
      impactList.appendChild(row);
      tones.add("mechanic");
    }

    const tone = spellTone(tones);
    spell.dataset.tone = tone;

    const head = spell.querySelector(".spell-head > div") || spell.querySelector(".spell-head");
    if (head) {
      const badge = document.createElement("span");
      badge.className = `legacy-tone-badge ${tone}`;
      badge.textContent = toneLabel[tone];
      head.prepend(badge);
    }

    const impactBlock = document.createElement("section");
    impactBlock.className = "legacy-impact-block";
    impactBlock.innerHTML = "<div class='legacy-impact-block-title'>这条改动可能影响</div>";
    impactBlock.appendChild(impactList);
    const headNode = spell.querySelector(".spell-head");
    if (headNode) headNode.after(impactBlock);

    if (lines.length) {
      const details = document.createElement("details");
      details.className = "legacy-tech-details";
      const summary = document.createElement("summary");
      summary.textContent = `查看 DB2 字段细节（${lines.length}）`;
      details.appendChild(summary);
      lines.forEach((line) => details.appendChild(line));
      spell.appendChild(details);
    }
  });

  const spells = Array.from(report.querySelectorAll(".spell"));
  const counts = spells.reduce(
    (result, spell) => {
      const tone = spell.dataset.tone || "mechanic";
      result[tone] = (result[tone] || 0) + 1;
      return result;
    },
    { buff: 0, nerf: 0, mixed: 0, mechanic: 0 }
  );

  const controls = report.querySelector(".controls");
  if (!controls || !spells.length) return;

  const overview = document.createElement("section");
  overview.className = "legacy-impact-overview";
  overview.innerHTML = `
    <div class="legacy-impact-overview-head">
      <strong>改动影响概览</strong>
      <span>增强/削弱依据可直接判断的字段归类；机制与联动仍需结合实战验证。</span>
    </div>
    <div class="legacy-tone-tabs" role="group" aria-label="按改动方向筛选">
      <button type="button" data-legacy-tone="all" aria-pressed="true">全部 <span>${spells.length}</span></button>
      <button type="button" data-legacy-tone="buff" aria-pressed="false">增强 <span>${counts.buff}</span></button>
      <button type="button" data-legacy-tone="nerf" aria-pressed="false">削弱 <span>${counts.nerf}</span></button>
      <button type="button" data-legacy-tone="other" aria-pressed="false">机制 / 混合 <span>${counts.mechanic + counts.mixed}</span></button>
    </div>`;
  controls.before(overview);

  let activeTone = "all";
  const input = controls.querySelector("input[type='search']");
  const count = controls.querySelector(".count");

  const apply = () => {
    const query = String(input?.value || "").trim().toLowerCase();
    let visible = 0;
    spells.forEach((spell) => {
      const tone = spell.dataset.tone || "mechanic";
      const toneMatch = activeTone === "all" || tone === activeTone || (activeTone === "other" && (tone === "mechanic" || tone === "mixed"));
      const queryMatch = !query || String(spell.dataset.search || "").includes(query) || String(spell.textContent || "").toLowerCase().includes(query);
      const shown = toneMatch && queryMatch;
      spell.classList.toggle("hidden", !shown);
      if (shown) visible += 1;
    });
    report.querySelectorAll(".spec-section").forEach((section) => {
      section.classList.toggle("hidden", !section.querySelector(".spell:not(.hidden)"));
    });
    report.querySelectorAll(".class-section").forEach((section) => {
      section.classList.toggle("hidden", !section.querySelector(".spell:not(.hidden)"));
    });
    if (count) count.textContent = activeTone === "all" && !query ? `全部 ${spells.length} 个技能` : `显示 ${visible} / ${spells.length}`;
  };

  input?.addEventListener("input", apply);
  overview.querySelectorAll("[data-legacy-tone]").forEach((button) => {
    button.addEventListener("click", () => {
      activeTone = button.dataset.legacyTone || "all";
      overview.querySelectorAll("[data-legacy-tone]").forEach((item) => {
        item.setAttribute("aria-pressed", item === button ? "true" : "false");
      });
      apply();
    });
  });
  apply();
})();
