import html
import re


def build_wow_skill_diff_fallback_html(report, page_title='', server_title=''):
    """Build an inline HTML fallback from saved report content without Markdown rendering."""
    title = page_title or _report_title(report, server_title)
    from_build = (getattr(report, 'display_from_build', '') or getattr(report, 'from_build', '') or '').strip()
    to_build = (getattr(report, 'display_to_build', '') or getattr(report, 'to_build', '') or '').strip()
    spell_count = int(getattr(report, 'spell_count', 0) or 0)
    class_count = int(getattr(report, 'class_count', 0) or 0)
    content_md = getattr(report, 'content_md', '') or ''
    changed_tables = _parse_changed_tables(getattr(report, 'changed_tables_json', '') or '')
    summary = _extract_plain_summary(content_md)
    content_html = _render_markdown_report_content(content_md)

    metrics = [
        ('版本', f'{from_build} → {to_build}' if from_build or to_build else ''),
        ('技能数', str(spell_count) if spell_count else ''),
        ('职业数', str(class_count) if class_count else ''),
        ('服务器', server_title or (getattr(report, 'branch', '') or '')),
    ]
    metric_html = ''.join(
        f"<div class='metric'><span>{_esc(label)}</span><strong>{_esc(value)}</strong></div>"
        for label, value in metrics
        if value
    )

    table_items = ''.join(f"<li><code>{_esc(t)}</code></li>" for t in changed_tables[:80])
    if not table_items:
        table_items = "<li class='muted'>暂无可展示的 DB2 表摘要</li>"

    summary_html = f"<p>{_esc(summary)}</p>" if summary else "<p class='muted'>完整静态 HTML 报告暂不可用，当前展示由报告数据库内容直接生成。</p>"
    if content_html:
        content_panel = f"""
  <div class="panel report-content-panel" style="margin-top:14px;">
    <h2>技能变更内容</h2>
    {content_html}
  </div>"""
    else:
        content_panel = """
  <div class="panel report-content-panel" style="margin-top:14px;">
    <h2>技能变更内容</h2>
    <p class="muted">当前报告没有保存可解析的技能变更正文，只能展示摘要和 DB2 表诊断信息。</p>
  </div>"""

    return f"""
<section class="wow-skill-diff-fallback-html">
  <style>
    .wow-skill-diff-fallback-html {{ color:#0f172a; }}
    .wow-skill-diff-fallback-html .notice {{ border:1px solid #c7d2fe; background:#eef2ff; color:#3730a3; border-radius:12px; padding:12px 14px; font-size:13px; margin:0 0 18px; }}
    .wow-skill-diff-fallback-html h1 {{ font-size:28px; line-height:1.25; font-weight:800; margin:0 0 14px; }}
    .wow-skill-diff-fallback-html h2 {{ font-size:18px; font-weight:800; margin:22px 0 10px; }}
    .wow-skill-diff-fallback-html .metrics {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin:16px 0 20px; }}
    .wow-skill-diff-fallback-html .metric {{ border:1px solid #e2e8f0; border-radius:12px; padding:12px; background:#f8fafc; }}
    .wow-skill-diff-fallback-html .metric span {{ display:block; color:#64748b; font-size:12px; margin-bottom:4px; }}
    .wow-skill-diff-fallback-html .metric strong {{ display:block; color:#0f172a; font-size:15px; word-break:break-word; }}
    .wow-skill-diff-fallback-html .panel {{ border:1px solid #e2e8f0; border-radius:12px; background:#fff; padding:14px 16px; }}
    .wow-skill-diff-fallback-html .report-content-panel {{ background:#fbfdff; }}
    .wow-skill-diff-fallback-html .class-section {{ margin-top:18px; border:1px solid #cbd5e1; border-radius:14px; overflow:hidden; background:#fff; }}
    .wow-skill-diff-fallback-html .class-title {{ margin:0; padding:12px 14px; background:#0f172a; color:#fff; font-size:18px; font-weight:800; }}
    .wow-skill-diff-fallback-html .spec-section {{ padding:12px 14px 14px; border-top:1px solid #e2e8f0; }}
    .wow-skill-diff-fallback-html .spec-title {{ margin:0 0 10px; color:#334155; font-size:15px; font-weight:800; }}
    .wow-skill-diff-fallback-html .spell-card {{ border:1px solid #e2e8f0; border-radius:12px; padding:11px 12px; margin:10px 0; background:#fff; box-shadow:0 1px 2px rgba(15,23,42,.04); }}
    .wow-skill-diff-fallback-html .spell-title {{ font-weight:800; color:#111827; margin-bottom:6px; overflow-wrap:anywhere; }}
    .wow-skill-diff-fallback-html .spell-id {{ color:#64748b; font-size:12px; font-weight:700; }}
    .wow-skill-diff-fallback-html .spell-desc {{ color:#475569; font-size:13px; line-height:1.55; margin:6px 0 8px; white-space:pre-wrap; }}
    .wow-skill-diff-fallback-html .changes {{ margin:8px 0 0; padding-left:18px; color:#1f2937; font-size:13px; line-height:1.55; }}
    .wow-skill-diff-fallback-html .changes li {{ margin:3px 0; overflow-wrap:anywhere; }}
    .wow-skill-diff-fallback-html .tables {{ list-style:none; display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:8px; padding-left:0; margin:0; }}
    .wow-skill-diff-fallback-html .tables li {{ min-width:0; }}
    .wow-skill-diff-fallback-html code {{ display:block; background:#f1f5f9; border-radius:6px; padding:5px 7px; white-space:normal; overflow-wrap:anywhere; word-break:break-word; }}
    .wow-skill-diff-fallback-html .muted {{ color:#64748b; }}
  </style>
  <div class="notice">静态 HTML 文件暂不可用；此处直接用已保存的报告正文生成 HTML 展示，不再经过 Markdown 渲染。</div>
  <h1>{_esc(title)}</h1>
  <div class="metrics">{metric_html}</div>
  <div class="panel">
    <h2>摘要</h2>
    {summary_html}
  </div>
  {content_panel}
  <div class="panel" style="margin-top:14px;">
    <h2>变更 DB2 表</h2>
    <ul class="tables">{table_items}</ul>
  </div>
</section>
""".strip()


def _render_markdown_report_content(content):
    sections = _parse_markdown_report(content)
    if not sections:
        return ''
    return ''.join(_render_class_section(section) for section in sections)


def _parse_markdown_report(content):
    sections = []
    current_class = None
    current_spec = None
    current_spell = None

    def ensure_spec():
        nonlocal current_class, current_spec
        if current_class is None:
            current_class = {'title': '未分组', 'specs': []}
            sections.append(current_class)
        if current_spec is None:
            current_spec = {'title': '通用', 'spells': []}
            current_class['specs'].append(current_spec)
        return current_spec

    for raw_line in str(content or '').splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith('## ') and not line.startswith('### '):
            current_class = {'title': line[3:].strip(), 'specs': []}
            sections.append(current_class)
            current_spec = None
            current_spell = None
            continue
        if line.startswith('### '):
            if current_class is None:
                current_class = {'title': '未分组', 'specs': []}
                sections.append(current_class)
            current_spec = {'title': line[4:].strip(), 'spells': []}
            current_class['specs'].append(current_spec)
            current_spell = None
            continue
        # The saved report uses single-# lines for actual spell/effect changes, so do not treat them as headings.
        if line.startswith('# ') and current_class is None:
            continue
        match = re.match(r'^(.+?)\((\d+)\)\s*：\s*(.*)$', line)
        if match:
            spec = ensure_spec()
            current_spell = {
                'name': match.group(1).strip(),
                'spell_id': match.group(2).strip(),
                'description': match.group(3).strip(),
                'changes': [],
            }
            spec['spells'].append(current_spell)
            continue
        if current_spell is not None:
            current_spell['changes'].append(_clean_change_line(line))

    return [
        {
            'title': section['title'],
            'specs': [
                {'title': spec['title'], 'spells': [spell for spell in spec['spells'] if spell['name'] or spell['changes'] or spell['description']]}
                for spec in section['specs']
                if spec['spells']
            ],
        }
        for section in sections
        if any(spec['spells'] for spec in section['specs'])
    ]


def _render_class_section(section):
    specs_html = ''.join(_render_spec_section(spec) for spec in section.get('specs', []))
    return f"<section class='class-section'><h3 class='class-title'>{_esc(section.get('title', ''))}</h3>{specs_html}</section>"


def _render_spec_section(spec):
    spells_html = ''.join(_render_spell_card(spell) for spell in spec.get('spells', []))
    return f"<section class='spec-section'><h4 class='spec-title'>{_esc(spec.get('title', ''))}</h4>{spells_html}</section>"


def _render_spell_card(spell):
    desc = spell.get('description') or ''
    desc_html = f"<div class='spell-desc'>{_esc(desc)}</div>" if desc else ''
    changes = spell.get('changes') or []
    if changes:
        changes_html = '<ul class="changes">' + ''.join(f"<li>{_esc(change)}</li>" for change in changes) + '</ul>'
    else:
        changes_html = '<div class="muted">暂无结构化变更行</div>'
    return (
        "<article class='spell-card'>"
        f"<div class='spell-title'>{_esc(spell.get('name', ''))} <span class='spell-id'>#{_esc(spell.get('spell_id', ''))}</span></div>"
        f"{desc_html}{changes_html}"
        "</article>"
    )


def _clean_change_line(line):
    line = str(line or '').strip()
    line = re.sub(r'^#+\s*', '', line)
    line = re.sub(r'^[*\-+]\s+', '', line)
    return line.strip()


def _report_title(report, server_title):
    from_build = (getattr(report, 'display_from_build', '') or getattr(report, 'from_build', '') or '').strip()
    to_build = (getattr(report, 'display_to_build', '') or getattr(report, 'to_build', '') or '').strip()
    prefix = server_title or (getattr(report, 'branch', '') or 'Wago')
    if from_build or to_build:
        return f'{prefix} 职业技能变更报告：{from_build} → {to_build}'
    return f'{prefix} 职业技能变更报告'


def _parse_changed_tables(raw):
    import json

    try:
        value = json.loads(raw or '[]')
    except Exception:
        value = []
    if not isinstance(value, list):
        return []
    out = []
    seen = set()
    for item in value:
        name = str(item or '').strip()
        key = name.lower()
        if not name or key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def _extract_plain_summary(content):
    text = str(content or '').strip()
    if not text:
        return ''
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith('## '):
            break
        if line.startswith('#'):
            line = line.lstrip('#').strip()
        line = re.sub(r'^[*\-+]\s+', '', line)
        line = re.sub(r'`([^`]+)`', r'\1', line)
        lines.append(line)
        if len(lines) >= 4:
            break
    return '；'.join(lines)


def _esc(value):
    return html.escape(str(value or ''))
