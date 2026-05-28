def _norm(s):
    s = (s or '').strip().lower()
    s = s.replace('_', ' ')
    while '  ' in s:
        s = s.replace('  ', ' ')
    return s


WAGO_REGION_ID_TO_NAME = {
    1: 'Retail US',
    2: 'Retail KR',
    3: 'Retail EU',
    4: 'Retail TW',
    5: 'Retail CN',
    50: 'Retail PTR',
    57: 'Retail XPTR',
    21: 'Arena US',
    22: 'Arena KR',
    23: 'Arena EU',
    24: 'Arena TW',
    25: 'Arena CN',
    26: 'Arena OC',
    35: 'Classic Arena US',
    36: 'Classic Arena KR',
    37: 'Classic Arena EU',
    38: 'Classic Arena TW',
    39: 'Classic Arena CN',
    40: 'Classic PTR',
    41: 'Classic US',
    42: 'Classic KR',
    43: 'Classic EU',
    44: 'Classic TW',
    45: 'Classic CN',
    60: 'Dev 1',
    61: 'Dev 2',
    62: 'Dev 3',
}

WAGO_REGION_NAME_TO_ID = {_norm(v): int(k) for k, v in WAGO_REGION_ID_TO_NAME.items()}


def wago_region_name(region_id):
    try:
        region_id = int(region_id or 0)
    except Exception:
        region_id = 0
    return WAGO_REGION_ID_TO_NAME.get(region_id, '')


def wago_region_id(region_value):
    s = _norm(region_value)
    if not s:
        return 0
    if s.isdigit():
        try:
            return int(s)
        except Exception:
            return 0
    if s in WAGO_REGION_NAME_TO_ID:
        return WAGO_REGION_NAME_TO_ID[s]
    if s in ('us', 'eu', 'kr', 'tw', 'cn', 'ptr', 'xptr'):
        return WAGO_REGION_NAME_TO_ID.get(_norm(f'retail {s}'), 0)
    if s.startswith('retail '):
        return WAGO_REGION_NAME_TO_ID.get(s, 0)
    if s.startswith('classic '):
        return WAGO_REGION_NAME_TO_ID.get(s, 0)
    if s.startswith('arena '):
        return WAGO_REGION_NAME_TO_ID.get(s, 0)
    return 0

