"""Hypothesis profile registration for the property-based test suite.

Property tests live in ``tests/property/`` and run under two Hypothesis
profiles (see ``.kiro/steering/testing.md``):

- ``dev`` – 100 examples, the fast local default.
- ``ci`` – 500 examples, the thorough run used in CI.

The active profile is chosen from the ``HYPOTHESIS_PROFILE`` environment
variable (defaulting to ``dev``). Set ``HYPOTHESIS_PROFILE=ci`` in CI to run
the larger example count.

Individual tests may still pin their own ``max_examples`` with an explicit
``@settings`` decorator; those override the profile-wide default. The profiles
make the default example counts consistent and selectable for tests that do not
pin their own.
"""

from __future__ import annotations

import os

from hypothesis import HealthCheck, settings

# Fast local default: enough examples to find most issues quickly.
settings.register_profile(
    "dev",
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)

# Thorough CI run: more examples for better coverage of the input space.
settings.register_profile(
    "ci",
    max_examples=500,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)

# Select the active profile; default to the fast ``dev`` profile locally.
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))
