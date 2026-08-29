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

__all__ = ["MODEL_CLASSES", "MODEL_CLASS_RANK", "class_rank", "meets_minimum", "required_class_for_purpose"]

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
