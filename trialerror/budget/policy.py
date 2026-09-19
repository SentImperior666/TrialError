"""Model-policy checks. Design Section 5.4: "Model policy enforcement
(Section 1.11): ``trialerror.toml [models]`` maps purposes -> minimum model
class (ideation/gates -> top; mechanical -> small-eligible). ``book_launch``
refuses a top-tier-required purpose on a cheap model unless the booking
cites an override ruling id; when the pool can't afford top-tier, it
returns state ``DEFERRED``."

TRIALERROR-DEV-NOTE (policy table shape): the design names the mapping
("purposes -> minimum model class") but not its exact TOML key shape. This
module reads it as a flat ``{purpose: min_class}`` dict - i.e.
``ProgramConfig.models`` (``trialerror.util.config``, M0) is expected to hold a
``[models]`` table like::

    [models]
    ideation = "top"
    gates = "top"
    mechanical = "small"

read generically via ``ProgramConfig.models`` (already a plain dict per
M0). Closest-faithful reading of "purposes -> minimum model class" as a
literal TOML table; documented here since M0 shipped the loader without
consumers.
"""

from __future__ import annotations

import re

__all__ = [
    "MODEL_CLASSES",
    "MODEL_CLASS_RANK",
    "DEFAULT_MODEL_FAMILY_CLASSES",
    "NO_CLAIM_MODEL_VALUES",
    "class_rank",
    "meets_minimum",
    "required_class_for_purpose",
    "classify_model",
]

#: Design Section 4.3: ``budget_pool.model_class CHECK (model_class IN
#: ('top','mid','small'))`` - the same three-value enum governs policy
#: comparisons here.
MODEL_CLASSES: tuple[str, ...] = ("small", "mid", "top")
MODEL_CLASS_RANK: dict[str, int] = {name: i for i, name in enumerate(MODEL_CLASSES)}


def class_rank(model_class: str) -> int:
    """Ordinal rank of a model class (``small`` < ``mid`` < ``top``).
    Unknown classes rank below ``small`` (fail closed: an unrecognized
    class never satisfies a minimum-class requirement)."""
    return MODEL_CLASS_RANK.get(model_class, -1)


def meets_minimum(model_class: str, minimum: str | None) -> bool:
    """Whether ``model_class`` satisfies a ``minimum`` requirement.
    ``minimum=None`` (purpose not present in the policy table) always
    satisfies - an unconfigured purpose has no floor."""
    if minimum is None:
        return True
    return class_rank(model_class) >= class_rank(minimum)


def required_class_for_purpose(policy: dict[str, str] | None, purpose: str) -> str | None:
    """The configured minimum model class for ``purpose``, or ``None`` if
    the policy table doesn't mention it (no floor)."""
    if not policy:
        return None
    return policy.get(purpose)


# ---------------------------------------------------------------------------
# The other half of the policy pair: which class a MODEL is.
#
# ``[models]`` answers "what class does this purpose need"; the spawn gate's
# ``agent_model_matches_booking`` guard also needs "what class is the model
# this subagent was actually spawned with", or booking `top` and spawning
# something cheaper stays free. Model names are not a closed set and never
# will be, so this is a FAMILY map read against the tokens of a name --
# `haiku`, `claude-3-5-haiku-20241022` and `Claude Haiku 4.5` all resolve to
# the same class -- with `trialerror.toml`'s own `[model_classes]` table
# extending and overriding it for anything the families do not cover.
# ---------------------------------------------------------------------------

#: Built-in family -> class map. Deliberately small: a family token, not a
#: version list, so a new point release of a known family needs no edit here.
DEFAULT_MODEL_FAMILY_CLASSES: dict[str, str] = {
    "haiku": "small",
    "sonnet": "mid",
    "opus": "top",
    "fable": "top",
}

#: Model values that assert nothing about class: an agent that inherits its
#: parent's model, or a field left empty. These resolve to ``None`` --
#: "no claim made" -- which is NOT the same answer as "unknown model", and
#: the gate treats the two differently (no claim cannot be a mismatch).
NO_CLAIM_MODEL_VALUES: frozenset[str] = frozenset({"", "inherit", "inherited", "default", "none", "null"})

_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def classify_model(model: str | None, *, model_classes: dict[str, str] | None = None) -> str | None:
    """The model class ``model`` belongs to, or ``None`` when the value
    makes no claim (:data:`NO_CLAIM_MODEL_VALUES`, or ``None``) or names no
    family this map knows.

    Resolution order: an exact (case-insensitive) entry in ``model_classes``
    -- the program's own ``[model_classes]`` table -- then an exact entry in
    :data:`DEFAULT_MODEL_FAMILY_CLASSES`, then a family TOKEN inside the
    name from either map, program entries first. A name matching two
    families is not resolved by guessing: the longest matching token wins,
    which is the only tie-break that does not depend on dict order.

    ``None`` for an unrecognised name is deliberate and is not the same as
    "fine": the caller decides what an unclassifiable model means. The spawn
    gate refuses it, because a spawn it cannot verify is a spawn it cannot
    let through, and the fix is one line in ``[model_classes]``."""
    if model is None:
        return None
    normalized = str(model).strip().lower()
    if normalized in NO_CLAIM_MODEL_VALUES:
        return None

    program = {str(k).strip().lower(): str(v) for k, v in (model_classes or {}).items()}
    for table in (program, DEFAULT_MODEL_FAMILY_CLASSES):
        hit = table.get(normalized)
        if hit is not None:
            return hit

    tokens = {t for t in _TOKEN_SPLIT.split(normalized) if t}
    for table in (program, DEFAULT_MODEL_FAMILY_CLASSES):
        matches = sorted((family for family in table if family in tokens), key=len, reverse=True)
        if matches:
            return table[matches[0]]
    return None
