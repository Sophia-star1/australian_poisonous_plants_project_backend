"""Tests for the two conventions `plants_v2` introduces.

Both are easy to get wrong in a way that does not fail — which is the reason
these exist rather than being folded into the existing review tests.

1. **A gap is the string `'Unknown'`, never NULL.** Every column in the table
   is NOT NULL. So the ordinary emptiness test is not merely imprecise, it is
   inverted: `if not plant.health_effects` is `False` for every entry that
   records nothing, because `'Unknown'` is a truthy string. Anything that asks
   "was this filled in?" has to go through `not_recorded`.

2. **Toxicity is per-species, and 'Unknown' is not a finding.** Across the 100
   curated rows, 1,056 of 1,100 species entries say `'Unknown'` and only 44 say
   `'Toxic'`. Reading "not Unknown" as toxic, or treating the presence of a
   species key as a finding, would mark nearly the whole database as toxic to
   nearly everything.
"""

from __future__ import annotations

import pytest

from app.models.plant import TOXICITY_SPECIES, UNKNOWN, Plant, not_recorded
from app.services.submission_service import _unknown_defaults

# ── 1. 'Unknown' means absent ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "   ",
        UNKNOWN,
        "  Unknown  ",
        [],
        [UNKNOWN],
        [UNKNOWN, UNKNOWN],
        {},
        {"primary_class": UNKNOWN, "secondary_class": UNKNOWN},
        {"flowering_time": [UNKNOWN], "height_m": UNKNOWN},
    ],
)
def test_these_all_count_as_not_recorded(value: object) -> None:
    assert not_recorded(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "Toxic",
        "Vomiting and diarrhoea.",
        ["Seeds"],
        [UNKNOWN, "Seeds"],
        {"humans": "Toxic"},
        {"humans": UNKNOWN, "horses": "Toxic"},
        {"height_m": 1.5},
        1.5,
        False,
    ],
)
def test_these_all_count_as_recorded(value: object) -> None:
    """One real value anywhere is enough — a profile naming only humans counts."""
    assert not_recorded(value) is False


def test_the_naive_emptiness_test_would_be_wrong() -> None:
    """The bug this guards against, stated as an assertion.

    Every one of these is truthy, so `if not value` would report all of them as
    present. That is what made the reviewer warnings stop firing.
    """
    for value in (UNKNOWN, [UNKNOWN], {"primary_class": UNKNOWN}):
        assert bool(value) is True
        assert not_recorded(value) is True


# ── 2. derived toxicity ──────────────────────────────────────────────────────


def _plant(profile: dict[str, str]) -> Plant:
    plant = Plant()
    plant.botanical_name = "Abrus precatorius"
    plant.toxicity_by_species = profile
    return plant


def test_humans_toxic_is_reported() -> None:
    assert _plant({"humans": "Toxic"}).toxic_to_humans is True


def test_humans_unknown_is_not_toxic() -> None:
    """The commonest case by far: 'Unknown' is not a positive finding."""
    assert _plant({"humans": UNKNOWN}).toxic_to_humans is False


def test_a_missing_humans_key_is_not_toxic() -> None:
    assert _plant({"horses": "Toxic"}).toxic_to_humans is False


def test_an_all_unknown_profile_is_toxic_to_nothing() -> None:
    """What 56 of the 100 curated rows look like."""
    plant = _plant({key: UNKNOWN for key in (*TOXICITY_SPECIES, "notes")})
    assert plant.toxic_to_humans is False
    assert plant.toxic_to_animals == []


def test_animals_lists_only_established_species() -> None:
    plant = _plant({"humans": "Toxic", "horses": "Toxic", "dogs": UNKNOWN, "cats": "Toxic"})
    assert plant.toxic_to_animals == ["horses", "cats"]


def test_humans_is_never_listed_as_an_animal() -> None:
    assert _plant({"humans": "Toxic"}).toxic_to_animals == []


def test_notes_is_never_listed_as_a_species() -> None:
    """`notes` shares the object but is commentary, not a species."""
    plant = _plant({"notes": "Toxic", "dogs": "Toxic"})
    assert plant.toxic_to_animals == ["dogs"]


def test_animals_follow_the_canonical_species_order() -> None:
    """Stable output, so a client rendering the list does not see it reshuffle."""
    plant = _plant({"cats": "Toxic", "cattle": "Toxic", "horses": "Toxic"})
    assert plant.toxic_to_animals == ["cattle", "horses", "cats"]


# ── 3. the NOT NULL defaults ─────────────────────────────────────────────────


def test_defaults_cover_every_not_null_column() -> None:
    """An approval carrying only a botanical_name must still satisfy the table.

    Checked against the ORM's own column list rather than a hand-written one,
    so adding a column to the model without a default fails here instead of
    raising IntegrityError on the next approval.

    Columns the database fills itself are excluded: the primary key, and
    `created_at`, which carries a `server_default` of `now()`. Those are the
    only NOT NULL columns a writer legitimately omits.
    """
    supplied = set(_unknown_defaults()) | {"botanical_name"}
    required = {
        column.name
        for column in Plant.__table__.columns
        if not column.nullable
        and not column.primary_key
        and column.default is None
        and column.server_default is None
    }
    missing = required - supplied
    assert missing == set(), f"no default for NOT NULL column(s): {sorted(missing)}"


def test_botanical_name_is_deliberately_not_defaulted() -> None:
    """Defaulting it would publish an unnamed plant rather than fail."""
    assert "botanical_name" not in _unknown_defaults()


def test_the_defaults_read_as_not_recorded() -> None:
    """Otherwise the reviewer warnings would not fire on an empty approval."""
    for name, value in _unknown_defaults().items():
        assert not_recorded(value) is True, name


def test_each_call_returns_fresh_structures() -> None:
    """A module-level constant would be shared between every approved plant."""
    first, second = _unknown_defaults(), _unknown_defaults()
    first["toxicity_by_species"]["humans"] = "Toxic"
    first["toxic_parts"].append("Seeds")
    assert second["toxicity_by_species"]["humans"] == UNKNOWN
    assert second["toxic_parts"] == [UNKNOWN]


def test_the_species_default_covers_every_species_plus_notes() -> None:
    assert set(_unknown_defaults()["toxicity_by_species"]) == {*TOXICITY_SPECIES, "notes"}
