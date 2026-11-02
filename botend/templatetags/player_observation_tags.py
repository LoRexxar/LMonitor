"""Read-only public presentation; keep full analysis in its original DTO."""
from django import template

register = template.Library()


@register.filter
def confirmed_player_observation(observation):
    if not isinstance(observation, dict):
        return {}
    if observation.get('source_kind') == 'manual':
        # The central projection and template retain their editorial gates.
        return observation
    from botend.services.player_observation_badges import high_confidence_badges

    codes = {badge['code'] for badge in high_confidence_badges(observation, include_stale=True)}
    judgements = [item for item in observation.get('display_judgements', []) if item['code'] in codes]
    return {**observation,
            'display_judgements': judgements,
            'has_supported_judgements': bool(judgements),
            'label_reviews': [item for item in observation.get('label_reviews', []) if item['code'] in codes],
            'carrying_frequency': observation.get('carrying_frequency') if 'carrying' in codes else None,
            'investigation': None,
            'has_investigation': False}
