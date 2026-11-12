"""场景展示只保留一份目标数与时长，不改动模拟参数或比较签名。"""

from decimal import Decimal, InvalidOperation
import re


def _number(value):
    if value is None or value == '':
        return ''
    try:
        number = Decimal(str(value))
        if number.is_finite():
            return format(number.normalize(), 'f')
    except InvalidOperation:
        pass
    return str(value)


def scenario_label(name, targets, duration):
    targets, duration = _number(targets), _number(duration)
    conditions = [f'{targets} 目标' if targets else '', f'{duration} 秒' if duration else '']
    extras = []
    for part in re.split(r'[·/|,，;；]+', str(name or '')):
        part = part.strip()
        if not part:
            continue
        target = re.fullmatch(r'(\d+(?:\.\d+)?)\s*目标', part)
        seconds = re.fullmatch(r'(\d+(?:\.\d+)?)\s*秒', part)
        combined = re.fullmatch(r'(\d+(?:\.\d+)?)\s*目标\s+(\d+(?:\.\d+)?)\s*秒', part)
        # 参数是展示依据，旧自动名称中的数字也不能覆盖更新后的条件。
        if (target and targets or seconds and duration or
                part in ('单体', '单目标') and targets or combined and targets and duration):
            continue
        extras.append(part)
    return ' · '.join([*extras, *(value for value in conditions if value)])
