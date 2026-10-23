"""同一范围的玩家展示摘要只保存一份，节点和方案用局部索引引用。"""
import json

REFERENCE = '_stats_player_ref'


def pack_players(detail):
    players, indices = [], {}
    collision = False

    def visit(value):
        nonlocal collision
        if isinstance(value, list):
            return [visit(child) for child in value]
        if not isinstance(value, dict):
            return value
        if REFERENCE in value:
            collision = True
        if (all(isinstance(value.get(key), str) for key in ('name', 'realm', 'region'))
                and ('dps' in value or 'score' in value)):
            key = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
            if key not in indices:
                indices[key] = len(players)
                players.append(value)
            return {REFERENCE: indices[key]}
        return {key: visit(child) for key, child in value.items()}

    compact = visit(detail)
    return (detail, []) if collision else (compact, players)


def unpack_players(detail, players):
    if not isinstance(players, list) or any(not isinstance(row, dict) for row in players):
        raise ValueError('统计玩家引用表无效')

    def visit(value):
        if isinstance(value, list):
            return [visit(child) for child in value]
        if not isinstance(value, dict):
            return value
        if set(value) == {REFERENCE}:
            index = value[REFERENCE]
            if type(index) is not int or not 0 <= index < len(players):
                raise ValueError('统计玩家引用越界')
            return players[index]
        return {key: visit(child) for key, child in value.items()}

    return visit(detail)
