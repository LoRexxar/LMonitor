"""按 C++ 语法树检查探测方法读取的成员初始化及执行阶段写入，不使用技能名称。"""
import json
from pathlib import Path
import re
from tree_sitter import Language, Parser
import tree_sitter_cpp

PARSER = Parser(Language(tree_sitter_cpp.language()))


def nodes(node):
    yield node
    for child in node.named_children:
        yield from nodes(child)


def name_of(node, source):
    while node and node.type not in ('identifier', 'field_identifier', 'operator_name', 'destructor_name'):
        node = node.child_by_field_name('declarator')
    return source[node.start_byte:node.end_byte].decode() if node else ''


def member_writes(body, source):
    result = set()
    for n in nodes(body):
        if n.type != 'assignment_expression':
            continue
        left = n.child_by_field_name('left')
        if left.type == 'identifier':
            result.add(source[left.start_byte:left.end_byte].decode())
        elif left.type == 'field_expression':
            argument = left.child_by_field_name('argument')
            field = left.child_by_field_name('field')
            if source[argument.start_byte:argument.end_byte] == b'this':
                result.add(source[field.start_byte:field.end_byte].decode())
    return result


def audit_source(source_root):
    reports = []
    counts = {'源码文件': 0, '类': 0, '带探测方法的类': 0}
    for path in sorted(p for p in source_root.rglob('*') if p.suffix in {'.cpp', '.hpp', '.inc'}):
        source = path.read_bytes()
        tree = PARSER.parse(source)
        counts['源码文件'] += 1
        for cls in nodes(tree.root_node):
            if cls.type not in ('struct_specifier', 'class_specifier'):
                continue
            body, name = cls.child_by_field_name('body'), cls.child_by_field_name('name')
            if not body or not name:
                continue
            counts['类'] += 1
            classname = source[name.start_byte:name.end_byte].decode()
            fields, initialized, methods, constructors = {}, set(), {}, []
            for child in body.named_children:
                if child.type == 'template_declaration':
                    child = next((n for n in child.named_children if n.type == 'function_definition'), child)
                if child.type == 'field_declaration':
                    type_node = child.child_by_field_name('type')
                    if not type_node:
                        continue
                    typename = source[type_node.start_byte:type_node.end_byte].decode()
                    for d in child.children_by_field_name('declarator'):
                        # 类类型的默认构造不等于未初始化；只检查原始数值、枚举和指针成员。
                        scalar = type_node.type in ('primitive_type', 'sized_type_specifier') or typename.endswith('_e')
                        pointer = d.type == 'pointer_declarator'
                        if not scalar and not pointer:
                            continue
                        field = name_of(d, source)
                        if not field:
                            continue
                        fields[field] = {'类型': typename, '行': d.start_point.row + 1}
                        if d.type == 'init_declarator' or child.child_by_field_name('default_value'):
                            initialized.add(field)
                elif child.type == 'function_definition':
                    if child.child_by_field_name('body') is None:
                        continue
                    function = name_of(child.child_by_field_name('declarator'), source)
                    head = source[child.start_byte:child.child_by_field_name('body').start_byte].decode()
                    macro_ctor = re.match(r'\s*[A-Z_]+\s*\(\s*' + re.escape(classname) + r'\s*,', head)
                    if function == classname or macro_ctor:
                        constructors.append(child)
                    elif function:
                        methods[function] = child
            roots = {n for n in methods if re.match(r'^(action_multiplier|composite_|calculate_|snapshot_state|n_targets|cost$|ready$|execute_time|travel_time|tick_time)', n)}
            if not roots:
                continue
            counts['带探测方法的类'] += 1
            for ctor in constructors:
                head = source[ctor.start_byte:ctor.child_by_field_name('body').start_byte].decode()
                initialized.update(field for field in fields if re.search(r'\b' + re.escape(field) + r'\s*\(', head))
                for item in ctor.named_children:
                    if item.type == 'field_initializer_list':
                        initialized.update(source[n.named_children[0].start_byte:n.named_children[0].end_byte].decode()
                                           for n in item.named_children if n.type == 'field_initializer')
                initialized.update(member_writes(ctor.child_by_field_name('body'), source))
            for stage in ('init', 'init_finished', 'reset'):
                if stage in methods:
                    initialized.update(member_writes(methods[stage].child_by_field_name('body'), source))
            reachable, pending = set(), list(roots)
            while pending:
                method = pending.pop()
                if method in reachable:
                    continue
                reachable.add(method)
                for n in nodes(methods[method].child_by_field_name('body')):
                    if n.type == 'call_expression':
                        fn = n.child_by_field_name('function')
                        if fn and fn.type == 'identifier':
                            called = source[fn.start_byte:fn.end_byte].decode()
                            if called in methods:
                                pending.append(called)
            reads = {}
            for method in reachable:
                block = methods[method].child_by_field_name('body')
                local_names = {name_of(n, source) for n in nodes(block) if n.type == 'init_declarator'}
                for n in nodes(block):
                    if n.type != 'identifier':
                        continue
                    field = source[n.start_byte:n.end_byte].decode()
                    if field in fields and field not in initialized and field not in local_names:
                        reads.setdefault(field, []).append({'方法': method, '行': n.start_point.row + 1})
            if not reads:
                continue
            class_text = source[cls.start_byte:cls.end_byte].decode()
            for field, sites in reads.items():
                writes = [method for method, node in methods.items()
                          if field in member_writes(node.child_by_field_name('body'), source)]
                reports.append({'文件': str(path.relative_to(source_root)), '类': classname, '成员': field,
                                **fields[field], '读取': sites, '执行期写入': writes,
                                '已有上下文标记': bool(re.search(r'snapshot_state_requires_parent_execute_state\s*=\s*true', class_text))})
    return counts, reports


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    counts, reports = audit_source(args.source / "engine/class_modules")
    result = {"统计": counts, "发现": reports, "未声明上下文数": sum(not r["已有上下文标记"] for r in reports)}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False), flush=True)
    if result["未声明上下文数"]:
        raise ValueError("存在未初始化且未声明执行上下文的探测成员，禁止据此发布技能数值")


if __name__ == "__main__":
    main()
