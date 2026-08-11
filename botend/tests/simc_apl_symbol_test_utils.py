"""无版本 APL 字段及职业/专精归属的测试夹具。"""

from botend.models import SimcAplSymbol, SimcAplSymbolScope, SimcBackendBinary


SCOPE_FIELDS = {
    field.name for field in SimcAplSymbolScope._meta.fields
    if field.name not in {'id', 'symbol', 'created_at', 'updated_at'}
}


def symbol_scope(**values):
    """创建未保存的归属；字段主体会按 token + 类型复用。

    旧测试夹具传入的版本不再写入字段，仅用于补齐同版本测试后端的
    严格校验上下文，便于逐步迁移现有测试。
    """
    values = dict(values)
    revision = str(values.pop('simc_revision', None) or '').strip()
    game_build = str(values.pop('wow_build', None) or '').strip()
    if revision and game_build:
        SimcBackendBinary.objects.filter(
            current_version=revision, game_build='',
        ).update(game_build=game_build)
    token = values.pop('token')
    kind = values.pop('symbol_kind', SimcAplSymbol.KIND_ACTION)
    for key in ('class_key', 'spec_key', 'hero_tree_key'):
        values.pop(key, None)
    active = bool(values.get('is_active', True))
    symbol, _created = SimcAplSymbol.objects.get_or_create(
        token=token, symbol_kind=kind,
    )
    if active and not symbol.is_active:
        symbol.is_active = True
        symbol.save(update_fields=['is_active'])
    scope_values = {key: value for key, value in values.items() if key in SCOPE_FIELDS}
    return SimcAplSymbolScope.prepare(
        SimcAplSymbolScope(symbol=symbol, **scope_values),
    )


def create_symbol_scope(**values):
    scope = symbol_scope(**values)
    scope.save(force_insert=True)
    return scope


def get_or_create_symbol_scope(defaults=None, **values):
    candidate = symbol_scope(**dict(defaults or {}, **values))
    lookup = {
        'symbol': candidate.symbol,
        'class_key': candidate.class_key,
        'spec_key': candidate.spec_key,
        'hero_tree_key': candidate.hero_tree_key,
    }
    scope, created = SimcAplSymbolScope.objects.get_or_create(
        **lookup,
        defaults={
            field: getattr(candidate, field)
            for field in SCOPE_FIELDS
            if field not in {'class_key', 'spec_key', 'hero_tree_key'}
        },
    )
    return scope, created


def bulk_create_symbol_scopes(scopes):
    return SimcAplSymbolScope.objects.bulk_create(list(scopes))
