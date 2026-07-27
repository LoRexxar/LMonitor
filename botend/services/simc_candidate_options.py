"""Server-owned policy for candidate-specific SimulationCraft scalar options."""

import re


CONTROLLED_SIMC_OPTIONS = frozenset({
    'midnight.crucible_of_erratic_energies_predation=1',
    'midnight.crucible_of_erratic_energies_sustenance=1',
    'midnight.crucible_of_erratic_energies_violence=1',
})
_SCALAR_ASSIGNMENT = re.compile(r'^[a-z][a-z0-9_.-]{0,79}=[a-zA-Z0-9_./+:-]{0,120}$')


def normalize_controlled_simc_options(value, *, allow_absent=True):
    """Return a canonical list, rejecting every option not owned by this service.

    Syntax validation is intentionally not authorization: only exact assignments in
    ``CONTROLLED_SIMC_OPTIONS`` may cross the configuration/task/worker boundary.
    """
    if value is None and allow_absent:
        return None
    if not isinstance(value, list) or not value:
        raise ValueError('simc_options must be a non-empty list')
    if any(not isinstance(item, str) or not _SCALAR_ASSIGNMENT.fullmatch(item)
           for item in value):
        raise ValueError('simc_options must contain scalar assignments')
    if any(item not in CONTROLLED_SIMC_OPTIONS for item in value):
        raise ValueError('simc_options contains an option that is not allowlisted')
    if len(value) != len(set(value)):
        raise ValueError('simc_options contains duplicates')
    return sorted(value)
