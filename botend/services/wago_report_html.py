import html
import re


def build_wow_skill_diff_fallback_html(report, page_title='', server_title=''):
    """Build an inline HTML fallback from report metadata without Markdown rendering."""
    title = page_title or _report_title(report, server_title)
    from_build = (getattr(report, 'display_from_build', '') or getattr(report, 'from_build', '') or '').strip()
    to_build = (getattr(report, 'display_to_build', '') or getattr(report, 'to_build', '') or '').strip()
    spell_count = int(getattr(report, 'spell_count', 0) or 0)
    class_count = int(getattr(report, 'class_count', 0) or 0)
    changed_tables = _parse_changed_tables(getattr(report, 'changed_tables_json', '') or '')
    summary = _extract_plain_summary(getattr(report, 'content_md', '') or '')

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

    summary_html = f"<p>{_esc(summary)}</p>" if summary else "<p class='muted'>完整静态 HTML 报告暂不可用，当前展示由报告元数据直接生成。</p>"

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
    .wow-skill-diff-fallback-html .tables {{ list-style:none; display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:8px; padding-left:0; margin:0; }}
    .wow-skill-diff-fallback-html .tables li {{ min-width:0; }}
    .wow-skill-diff-fallback-html code {{ display:block; background:#f1f5f9; border-radius:6px; padding:5px 7px; white-space:normal; overflow-wrap:anywhere; word-break:break-word; }}
    .wow-skill-diff-fallback-html .muted {{ color:#64748b; }}
  </style>
  <div class="notice">静态 HTML 文件暂不可用；此处直接用报告元数据初始化 HTML 展示，不再经过 Markdown 渲染。</div>
  <h1>{_esc(title)}</h1>
  <div class="metrics">{metric_html}</div>
  <div class="panel">
    <h2>摘要</h2>
    {summary_html}
  </div>
  <div class="panel" style="margin-top:14px;">
    <h2>变更 DB2 表</h2>
    <ul class="tables">{table_items}</ul>
  </div>
</section>
""".strip()


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
        if line.startswith('#'):
            line = line.lstrip('#').strip()
        line = re.sub(r'^[*\-+]\s+', '', line)
        line = re.sub(r'`([^`]+)`', r'\1', line)
        lines.append(line)
        if len(lines) >= 3:
            break
    return '；'.join(lines)


def _esc(value):
    return html.escape(str(value or ''))
