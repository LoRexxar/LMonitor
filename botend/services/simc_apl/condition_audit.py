"""供文件和数据库审计复用的只读 APL 条件覆盖检查。"""

import hashlib
import re

from botend.services.simc_apl.ast import ActionAssignment, ActionEntry, Option
from botend.services.simc_apl.parser import parse
from botend.services.simc_apl.condition_variables import identifiers
from botend.services.simc_apl.condition_rendering import diagnostics
from botend.services.simc_apl.readable import readable_apl


def structural_symbols(document):
    """仅为语法审计填充引用；不冒充真实技能 ID、中文名或资料覆盖。"""
    symbols = {}
    for line in document.lines:
        if not isinstance(line, ActionAssignment):
            continue
        for action in line.actions:
            if not isinstance(action, ActionEntry):
                continue
            symbols['action', action.name] = {'name': action.name}
            for option in action.options:
                if not isinstance(option, Option) or option.expression is None:
                    continue
                for name in identifiers(option.expression):
                    parts = name.split('.')
                    if parts[0] in ('action', 'buff', 'debuff', 'dot', 'cooldown', 'talent', 'pet') and len(parts) > 1:
                        symbols[parts[0], parts[1]] = {'name': parts[1]}
                    elif parts[0] in ('prev', 'prev_off_gcd', 'spell_targets') and len(parts) == 2:
                        symbols['action', parts[1]] = {'name': parts[1]}
                    elif parts[0] == 'prev_gcd' and len(parts) == 3:
                        symbols['action', parts[2]] = {'name': parts[2]}
                    elif parts[0] in ('active_dot', 'active_dots', 'dot_refreshable_count') and len(parts) == 2:
                        symbols['dot', parts[1]] = {'name': parts[1]}
    return symbols


def audit_source(source, name, symbols=None):
    document = parse(source)
    display = readable_apl(source, structural_symbols(document) if symbols is None else symbols)
    failures, identities, expressions = [], [], 0
    option_ranges = {(line.range.start.line, action.name, opt.name)
        for line in document.lines if isinstance(line, ActionAssignment)
        for action in line.actions if isinstance(action, ActionEntry)
        for opt in action.options if isinstance(opt, Option) and opt.expression is not None}
    for group in display['groups']:
        for row in group['rows']:
            for field in row.get('fields', []):
                if (row['line'], row.get('name'), field['name']) not in option_ranges:
                    continue
                expressions += 1
                checks = diagnostics(field['segments'])
                if symbols is not None:
                    for part in field['segments']:
                        ref = part.get('reference')
                        if ref and not re.search(r'[\u3400-\u9fff]', str(ref.get('name', ''))):
                            checks.append({'reason': 'unlocalized_identity', 'token': ref.get('token') or ref.get('name', '')})
                for item in checks:
                    entry = {**item, 'line': row['line'], 'list': group['name'],
                             'action': row['name'], 'option': field['name'], 'source': row['raw']}
                    target = failures if item['reason'] in ('unknown_semantics', 'undefined_variable') else identities
                    target.append(entry)
    return {'name': name, 'sha256': hashlib.sha256(source.encode()).hexdigest(),
            'expressions': expressions, 'rows': display['count'], 'parse_issues': len(document.issues),
            'parse_diagnostics': [{'code': i.code, 'line': i.range.start.line, 'message': i.message} for i in document.issues],
            'identity_mode': '真实展示资料' if symbols is not None else '语法审计引用，不代表中文资料覆盖',
            'unknown_expressions': len({(r['line'], r['option']) for r in failures}),
            'unknown_fields': failures, 'identity_fallbacks': identities,
            'variable_uses': sum(len(r.get('variables', [])) for g in display['groups'] for r in g['rows'])}
