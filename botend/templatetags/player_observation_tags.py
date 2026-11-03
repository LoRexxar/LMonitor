"""Read-only public presentation; keep full analysis in its original DTO."""
from django import template

register = template.Library()


@register.filter
def confirmed_player_observation(observation):
    if not isinstance(observation, dict):
        return {}
    from botend.services.player_character_labels import confirmed_character_judgements

    judgements = confirmed_character_judgements(observation, include_stale=True)
    codes = {item['code'] for item in judgements}
    return {**observation,
            'display_judgements': judgements,
            'raid_judgement': None,
            'has_supported_judgements': bool(judgements),
            'label_reviews': [item for item in observation.get('label_reviews', []) if item['code'] in codes],
            'carrying_frequency': observation.get('carrying_frequency') if 'carrying' in codes else None,
            'investigation': None,
            'has_investigation': False}
