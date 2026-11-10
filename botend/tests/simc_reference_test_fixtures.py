"""测试显式发布资料，不在业务逻辑中提供自动全选行为。"""
from botend.models import SimcReferencePublication


def publish_resources(spec_key, *, profiles=(), talents=(), apls=()):
    row, _ = SimcReferencePublication.objects.get_or_create(spec_key=spec_key)
    row.is_published = True
    row.save(update_fields=['is_published'])
    row.profiles.add(*profiles)
    row.talents.add(*talents)
    row.apls.add(*apls)
    return row


def update_scenario_params(settings, **changes):
    """Change central scenario inputs without obsolete settings scalar fields."""
    for scene in settings.scenarios.all():
        scene.simulation_params = dict(scene.simulation_params, **changes)
        scene.save(update_fields=['simulation_params'])


def scenario_settings_payload(settings, backend, template, conditions):
    """Build a real inline-formset POST, retaining existing stable row IDs."""
    existing = list(settings.scenarios.order_by('display_order', 'pk'))
    payload = {'action': 'save', 'branch': 'ptr' if settings.use_ptr else 'retail',
               'backend': backend.pk, 'template': template.pk, 'is_enabled': 'on',
               'scenarios-TOTAL_FORMS': str(len(conditions)),
               'scenarios-INITIAL_FORMS': str(len(existing)),
               'scenarios-MIN_NUM_FORMS': '0', 'scenarios-MAX_NUM_FORMS': '1000'}
    for index, (targets, duration, iterations) in enumerate(conditions):
        prefix = f'scenarios-{index}-'
        payload.update({prefix + 'id': existing[index].pk if index < len(existing) else '',
                        prefix + 'name': f'{targets}目标 / {duration}秒',
                        prefix + 'desired_targets': str(targets), prefix + 'max_time': str(duration),
                        prefix + 'iterations': str(iterations), prefix + 'is_enabled': 'on',
                        prefix + 'display_order': str(index)})
    return payload
