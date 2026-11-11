"""发布管理与现有 SimC 编辑器之间的上下文链接，不复制编辑或执行逻辑。"""
from urllib.parse import urlencode

from django.urls import reverse

from botend.dashboard.permissions import effective_dashboard_permissions


def workflow_context(request, config=None, *, page='publication', branch='retail'):
    config_id = config.pk if config else None
    branch = ('ptr' if config.use_ptr else 'retail') if config else branch
    query = urlencode({'config': config_id}) if config_id else ''
    publication = reverse('simc_reference_publication') + (f'?{query}' if query else '')
    results = reverse('simc_reference_results') + (f'?{query}' if query else '')
    settings = reverse('simc_reference_settings') + '?' + urlencode({
        'branch': branch, **({'config': config_id} if config_id else {}),
    })
    can_open = 'simc.workflow' in effective_dashboard_permissions(request.user)
    can_edit = can_open and (request.user.is_staff or request.user.is_superuser)
    return {
        'publication_url': publication, 'results_url': results, 'settings_url': settings,
        'rankings_url': reverse('simc_reference_rankings_entry') + (f'?{query}' if query else ''),
        'can_edit': can_edit, 'can_open': can_open,
        'editor_urls': {kind: editor_url(kind, config=config, page=page) for kind in ('profile', 'talent', 'apl')}
        if can_open else {},
    }


def editor_url(kind, resource_id=None, *, config=None, page='publication'):
    params = {'section': 'simc-workflow', 'simc_resource': kind,
              'reference_return': 'results' if page == 'results' else 'publication'}
    if config:
        params.update(reference_config=config.pk, simc_spec=config.spec_key)
    if resource_id:
        params['simc_id'] = resource_id
    return reverse('dashboard') + '?' + urlencode(params)


def publication_sections(form, config, workflow):
    sections = []
    for key, kind, description in (
        ('profiles', 'profile', '共享装备按正式服与 PTR 分别参与模拟。'),
        ('talents', 'talent', '已选天赋会与装备、APL 分别组合。'),
        ('apls', 'apl', '每份已选 APL 都会生成独立的模拟组合。'),
    ):
        field = form[key]
        resources = {str(row.pk): row for row in form.fields[key].queryset}
        choices = []
        for choice in field:
            resource = resources.get(str(choice.data['value']))
            choices.append({'choice': choice, 'resource': resource,
                            'editor_url': editor_url(kind, resource.pk, config=config) if resource and workflow['can_edit'] else ''})
        sections.append({'field': field, 'key': key, 'kind': kind, 'description': description,
                         'choices': choices, 'library_url': workflow['editor_urls'].get(kind)})
    return sections
