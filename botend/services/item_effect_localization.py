"""Language-only projection of complete, matched tooltip text.

Historical artifacts supply templates, never current numbers or missing effects.
The entire English mechanism and its literal constants must match before any
translation is used. Frozen variant/evidence payloads remain untouched.
"""
from collections import Counter
from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path
import re

TOKEN = re.compile(r'\$\{[^{}]+\}|\$\d*proccooldown|\$\d*[sSmMwWtTdDuU]\d*')
NUMBER = r'\d[\d,]*(?:\.\d+)?'
VALUE = rf'(?:\(\s*{NUMBER}(?:\s*[*/+\-]\s*{NUMBER})+\s*\)|{NUMBER}(?:–{NUMBER}（随职责变化）)?)'
COOLDOWN = re.compile(r'\s*\((' + NUMBER + r') (Min|Sec) Cooldown\)\s*$')


def _space(text):
    return re.sub(r'\s+', ' ', text.replace(' · ', ' ').replace('条件说明：', '')).strip()


@lru_cache(maxsize=1)
def _templates():
    from botend.services.simc_benchmark_tooltip_generator import render_spell_description
    root = Path(__file__).resolve().parents[1] / 'data'
    source = json.loads((root / 'ptr_kithix_unbound_12_1_5.json').read_text())
    localized = json.loads((root / 'kithix_localization_12_1_5.json').read_text())
    chinese = {row['ID']: row.get('Description_lang', '') for row in localized['tables']['Spell']}
    result = {}
    for item in source['gear']['items']:
        templates = {}
        for variant in item.get('variants', []):
            for effect in variant.get('effects', []):
                spell_id = effect['spell_id']
                en, _ = render_spell_description(effect['template'], base_spell_id=spell_id, spell_queries={})
                zh, _ = render_spell_description(chinese.get(spell_id, ''), base_spell_id=spell_id, spell_queries={})
                if not re.search(r'[\u3400-\u9fff]', zh) or Counter(TOKEN.findall(en)) != Counter(TOKEN.findall(zh)):
                    continue
                templates[(spell_id, en, zh)] = (spell_id, _space(en), _space(zh), effect.get('trigger_type'))
        result[item['item_id']] = tuple(templates.values())
    return result


def _translate(text, template):
    spell_id, english, chinese, trigger = template
    prefix = 'Use: ' if trigger == 0 else 'Equip: '
    text = _space(text)
    if not text.startswith(prefix):
        return None
    body = text[len(prefix):]
    cooldown = COOLDOWN.search(body)
    suffix = ''
    if cooldown:
        suffix = f'（冷却时间：{cooldown[1]}{"分钟" if cooldown[2] == "Min" else "秒"}）'
        body = body[:cooldown.start()]
    tokens = TOKEN.findall(english)
    fragments = TOKEN.split(english)
    pattern = ''
    for i, fragment in enumerate(fragments):
        pattern += re.escape(fragment)
        if i < len(tokens):
            # Duration macros include their unit; numeric s/proccooldown macros
            # do not. Literal English units outside tokens remain exact matches.
            duration = bool(re.fullmatch(r'\$\d*d', tokens[i]))
            pattern += '(' + VALUE + (r'(?: sec|秒)' if duration else '') + ')'
    match = re.fullmatch(pattern, body)
    if not match:
        return None
    values = {}
    for token, value in zip(tokens, match.groups()):
        value = value.replace(' sec', '秒')
        if token in values and values[token] != value:
            return None
        values[token] = value
    rendered = TOKEN.sub(lambda m: values[m[0]], chinese).replace('秒秒', '秒')
    rendered = ('使用：' if trigger == 0 else '装备：') + rendered + suffix
    if '$' in rendered or re.search(r'[A-Za-z]{3,}', rendered):
        return None
    if Counter(re.findall(NUMBER, rendered)) != Counter(re.findall(NUMBER, text)):
        return None
    return rendered


def localize_item_effects(item_id, effects, *, branch):
    if branch != 'ptr' or not effects:
        return effects
    templates = _templates().get(item_id, ())
    if not templates:
        return effects
    result = deepcopy(effects)
    for effect in result:
        if not isinstance(effect, dict) or re.search(r'[\u3400-\u9fff]', str(effect.get('description_zh') or '')):
            continue
        text = str(effect.get('description') or '')
        matches = {translated for template in templates
                   if (not effect.get('spell_id') or effect['spell_id'] == template[0])
                   and (translated := _translate(text, template))}
        if len(matches) == 1:
            effect['description_zh'] = matches.pop()
    return result
