"""Tests for the botanical record a reviewer fills in when approving.

The three JSONB columns were `dict[str, Any]` until now, which meant two
reviewers could record the same fact under different keys and no client could
rely on finding either. The keys asserted here are the ones the ten entries
already in `plants` use, so existing data and new data describe themselves the
same way.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.submission import HealthEffects, PlantIdentification, ToxinProfile


def _identification(**overrides: object) -> PlantIdentification:
    return PlantIdentification(botanical_name="Abrus precatorius", **overrides)  # type: ignore[arg-type]


# ── what stays required ──────────────────────────────────────────────────────


def test_only_the_botanical_name_is_required() -> None:
    """The one NOT NULL column, and the only thing a reviewer must supply.

    Everything else is optional on purpose: blocking a review on a full
    toxicology profile pushes reviewers to invent values. The approval
    response warns about what is missing instead.
    """
    identification = PlantIdentification(botanical_name="Abrus precatorius")
    assert identification.botanical_name == "Abrus precatorius"
    assert identification.toxin_profile is None
    assert identification.health_effects is None


def test_a_blank_botanical_name_is_refused() -> None:
    with pytest.raises(ValidationError):
        PlantIdentification(botanical_name="")


def test_unknown_top_level_fields_are_refused() -> None:
    """A typo should fail loudly rather than being silently dropped."""
    with pytest.raises(ValidationError):
        _identification(botanicl_name="typo")


# ── the closed structures ────────────────────────────────────────────────────


def test_toxin_profile_keys_match_the_existing_entries() -> None:
    profile = ToxinProfile(
        toxins="Abrin", chemical_class="toxalbumin / plant lectin", onset="delayed 6-24 hours"
    )
    assert profile.model_dump(exclude_none=True) == {
        "toxins": "Abrin",
        "chemical_class": "toxalbumin / plant lectin",
        "onset": "delayed 6-24 hours",
    }


def test_health_effects_keeps_symptoms_as_a_list() -> None:
    """Symptom search matches against items, not against a sentence."""
    effects = HealthEffects(symptoms=["vomiting", "diarrhoea"], toxidrome="gastrointestinal")
    assert effects.symptoms == ["vomiting", "diarrhoea"]


@pytest.mark.parametrize("field", ["toxin_profile", "health_effects"])
def test_the_toxicity_structures_are_closed(field: str) -> None:
    """These two are what triage reads; the keys have to stay predictable."""
    with pytest.raises(ValidationError):
        _identification(**{field: {"invented_key": "value"}})


def test_morphology_stays_open() -> None:
    """Describing a plant is not a closed set.

    A reviewer who needs to record a rhizome should not have to wait for a
    schema change, so extra keys are kept as given.
    """
    identification = _identification(
        morphological_attributes={"flower": "small pale", "rhizome": "thick and creeping"}
    )
    assert identification.morphological_attributes is not None
    stored = identification.morphological_attributes.model_dump(exclude_none=True)
    assert stored["rhizome"] == "thick and creeping"
    assert stored["flower"] == "small pale"


# ── empty is absent ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "field", ["toxin_profile", "health_effects", "morphological_attributes"]
)
def test_an_empty_structure_is_treated_as_missing(field: str) -> None:
    """Storing `{}` would look answered while saying nothing.

    The approval endpoint warns when `health_effects` is empty, and a stored
    `{}` is truthy — the entry would silence the warning without carrying any
    information.
    """
    assert getattr(_identification(**{field: {}}), field) is None


def test_a_structure_with_only_nulls_is_also_missing() -> None:
    empty = _identification(health_effects={"symptoms": None, "toxidrome": None})
    assert empty.health_effects is None


def test_a_structure_with_one_real_value_is_kept() -> None:
    identification = _identification(health_effects={"symptoms": ["nausea"]})
    assert identification.health_effects is not None
    assert identification.health_effects.symptoms == ["nausea"]


# ── what reaches the database ────────────────────────────────────────────────


def test_nested_values_dump_to_plain_json() -> None:
    """The columns are jsonb; a Pydantic model would not survive the driver."""
    import json

    identification = _identification(
        toxin_profile={"toxins": "Abrin"},
        health_effects={"symptoms": ["nausea"]},
        morphological_attributes={"growth_habit": "vine"},
    )
    dumped = identification.model_dump(exclude_unset=True)

    assert isinstance(dumped["toxin_profile"], dict)
    assert json.loads(json.dumps(dumped)) == dumped


def test_omitted_fields_stay_omitted() -> None:
    """`exclude_unset` is what stops an approval blanking columns it never mentioned."""
    dumped = _identification(family_name="Fabaceae").model_dump(exclude_unset=True)
    assert set(dumped) == {"botanical_name", "family_name"}
