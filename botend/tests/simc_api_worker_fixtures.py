"""ORM-only prerequisites shared by API/worker contracts (not a TestCase)."""
from botend.models import SimcBackendBinary, SimcTalentString, WowItemSnapshot


def execution_backend():
    backend, _ = SimcBackendBinary.objects.get_or_create(
        identifier='api-worker-fixture', defaults={'name': 'API worker fixture', 'is_active': True},
    )
    return backend


def independent_talent(user_id, *, spec='fury', talent='BASE'):
    return SimcTalentString.objects.create(
        owner_user_id=user_id, name='Independent execution talent', spec=spec,
        talent=talent, is_active=True, is_selectable=True,
    )


def plate_head(item_id):
    return WowItemSnapshot.objects.create(
        item_id=item_id, name='Eligible plate helmet', inventory_type=1,
        item_class_id=4, item_subclass_id=4,
        metadata={'primary_stat_options': ['strength']},
    )
