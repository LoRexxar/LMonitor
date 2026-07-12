import re


_ATTRIBUTE_RESULT_FILE_RE = re.compile(
    r'(?P<task_id>\d+)_(?P<attr1_name>gear_(?:crit|haste|mastery|versatility))_'
    r'(?P<attr1_value>\d+)_(?P<attr2_name>gear_(?:crit|haste|mastery|versatility))_'
    r'(?P<attr2_value>\d+)\.html'
)


def parse_attribute_result_filename(result_file):
    """严格解析属性任务受控生成的结果文件名。"""
    match = _ATTRIBUTE_RESULT_FILE_RE.fullmatch(str(result_file or ''))
    if not match:
        return None
    parsed = match.groupdict()
    return {
        'task_id': int(parsed['task_id']),
        'attr1_name': parsed['attr1_name'],
        'attr1_value': int(parsed['attr1_value']),
        'attr2_name': parsed['attr2_name'],
        'attr2_value': int(parsed['attr2_value']),
    }
