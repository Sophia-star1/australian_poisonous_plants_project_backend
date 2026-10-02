"""Tests for the botanical record a reviewer fills in when approving.

The keys asserted here are `plants_v2`'s, so existing curated data and new
reviewer data describe themselves the same way. Where v2 differs from v1 the
difference is asserted rather than assumed: `toxicity_by_species` in place of a
boolean and a list, `health_effects` as prose, `toxin_profile` with a primary
and secondary chemical class.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.submission import PlantIdentification, ToxicityBySpecies, ToxinProfile


def _identification(**overrides: object) -> PlantIdentification:
    return PlantIdentification(botanical_name="Abrus precatorius", **overrides)  # type: ignore[arg-type]


# ── what stays required ──────────────────────────────────────────────────────


def test_only_the_botanical_name_is_required() -> None:
    """The only thing a reviewer must supply.

    Every column in `plants_v2` is NOT NULL, but that is not the reviewer's
    problem: blocking a review on a full toxicology profile pushes reviewers to
    invent values. `_create_plant` fills the dataset's 'Unknown' in, and the
    approval response warns about what is missing.
    """
    identification = PlantIdentification(botanical_name="Abrus precatorius")
    assert identification.botanical_name == "Abrus precatorius"
    assert identification.toxin_profile is None
    assert identification.health_effects is None
    assert identification.toxicity_by_species is None


def test_a_blank_botanical_name_is_refused() -> None:
    with pytest.raises(ValidationError):
        PlantIdentification(botanical_name="")


def test_unknown_top_level_fields_are_refused() -> None:
    """A typo should fail loudly rather than being silently dropped."""
    with pytest.raises(ValidationError):
        _identification(botanicl_name="typo")


def test_the_v1_toxicity_fields_are_gone() -> None:
    """`toxic_to_humans` and `toxic_to_animals` are no longer accepted.

    They are still *served* as derived fields for the mobile app, but they are
    not columns and a reviewer cannot write them. Accepting them silently would
    drop the reviewer's answer on the floor, which is worse than a 422.
    """
    with pytest.raises(ValidationError):
        _identification(toxic_to_humans=True)
    with pytest.raises(ValidationError):
        _identification(toxic_to_animals=["dogs"])


def test_the_dropped_v1_fields_are_refused() -> None:
    """Columns that no longer exist in v2."""
    for field, value in (
        ("historical_case_records", "a 1998 case"),
        ("distribution_formally_listed", ["QLD"]),
        ("distribution_observed", ["NSW"]),
    ):
        with pytest.raises(ValidationError):
            _identification(**{field: value})


# ── the closed structures ────────────────────────────────────────────────────


def test_toxin_profile_keys_match_the_v2_entries() -> None:
    profile = ToxinProfile(
        primary_class="Lectins / Toxalbumins",
        secondary_class="Unknown",
        toxins_implicated="Abrin- plant lectin, toxalbumin",
    )
    assert profile.model_dump(exclude_none=True) == {
        "primary_class": "Lectins / Toxalbumins",
        "secondary_class": "Unknown",
        "toxins_implicated": "Abrin- plant lectin, toxalbumin",
    }


def test_v1_toxin_profile_keys_are_refused() -> None:
    """`onset` has nowhere to go in v2; it belongs in health_effects prose."""
    with pytest.raises(ValidationError):
        ToxinProfile(onset="delayed 6-24 hours")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        ToxinProfile(chemical_class="toxalbumin")  # type: ignore[call-arg]


def test_toxicity_by_species_covers_the_ten_species_and_notes() -> None:
    profile = ToxicityBySpecies(humans="Toxic", horses="Toxic", notes="seeds only")
    stored = profile.model_dump(exclude_none=True)
    assert stored == {"humans": "Toxic", "horses": "Toxic", "notes": "seeds only"}


def test_a_misspelled_species_is_refused() -> None:
    """Closed on purpose: `dog` would record a finding nothing ever reads."""
    with pytest.raises(ValidationError):
        ToxicityBySpecies(dog="Toxic")  # type: ignore[call-arg]


def test_health_effects_is_prose_not_a_structure() -> None:
    """v2 made this a text column, so a dict is no longer valid here.

    A real loss: v1's `symptoms` was a list, and symptom search matched its
    entries. It now matches substrings of a paragraph. The column is text
    either way — asserting the shape is the honest thing to do.
    """
    identification = _identification(health_effects="Vomiting, diarrhoea, abdominal pain.")
    assert identification.health_effects == "Vomiting, diarrhoea, abdominal pain."
    with pytest.raises(ValidationError):
        _identification(health_effects={"symptoms": ["vomiting"]})


@pytest.mark.parametrize("field", ["toxin_profile", "toxicity_by_species"])
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
        morphological_attributes={"growth_form": "climber_herbaceous", "rhizome": "thick"}
    )
    assert identification.morphological_attributes is not None
    stored = identification.morphological_attributes.model_dump(exclude_none=True)
    assert stored["rhizome"] == "thick"
    assert stored["growth_form"] == "climber_herbaceous"


def test_morphology_accepts_the_v2_numeric_height() -> None:
    """The curated rows store `height_m` as a number, not a string."""
    identification = _identification(morphological_attributes={"height_m": 1.5})
    assert identification.morphological_attributes is not None
    assert identification.morphological_attributes.height_m == 1.5


def test_morphology_also_accepts_height_as_text() -> None:
    """`'Unknown'` is what the dataset writes when the height is not known."""
    identification = _identification(morphological_attributes={"height_m": "Unknown"})
    assert identification.morphological_attributes is not None
    assert identification.morphological_attributes.height_m == "Unknown"


# ── empty is absent ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "field", ["toxin_profile", "toxicity_by_species", "morphological_attributes"]
)
def test_an_empty_structure_is_treated_as_missing(field: str) -> None:
    """Storing `{}` would look answered while saying nothing.

    The approval endpoint warns when toxicity is unrecorded, and a stored `{}`
    is truthy — the entry would silence the warning without carrying any
    information.
    """
    assert getattr(_identification(**{field: {}}), field) is None


def test_a_structure_with_only_nulls_is_also_missing() -> None:
    empty = _identification(toxicity_by_species={"humans": None, "dogs": None})
    assert empty.toxicity_by_species is None


def test_a_structure_with_one_real_value_is_kept() -> None:
    identification = _identification(toxicity_by_species={"humans": "Toxic"})
    assert identification.toxicity_by_species is not None
    assert identification.toxicity_by_species.humans == "Toxic"


# ── what reaches the database ────────────────────────────────────────────────


def test_nested_values_dump_to_plain_json() -> None:
    """The columns are jsonb; a Pydantic model would not survive the driver."""
    import json

    identification = _identification(
        toxin_profile={"primary_class": "Lectins / Toxalbumins"},
        toxicity_by_species={"humans": "Toxic"},
        morphological_attributes={"growth_form": "vine"},
    )
    dumped = identification.model_dump(exclude_unset=True)

    assert isinstance(dumped["toxin_profile"], dict)
    assert json.loads(json.dumps(dumped)) == dumped


def test_omitted_fields_stay_omitted() -> None:
    """`exclude_unset` is what stops an approval blanking columns it never mentioned."""
    dumped = _identification(family_name="Fabaceae").model_dump(exclude_unset=True)
    assert set(dumped) == {"botanical_name", "family_name"}


# ── blank is unanswered ──────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["   ", "\t\n"])
def test_a_whitespace_botanical_name_is_refused(name: str) -> None:
    """`min_length=1` alone let these through and published a nameless plant."""
    with pytest.raises(ValidationError):
        PlantIdentification(botanical_name=name)


def test_the_botanical_name_is_trimmed() -> None:
    """Searching and sorting treat a leading space as part of the name."""
    assert _identification().botanical_name == "Abrus precatorius"
    assert PlantIdentification(botanical_name="  Abrus precatorius ").botanical_name == (
        "Abrus precatorius"
    )


@pytest.mark.parametrize("value", ["", "   "])
def test_a_blank_text_answer_is_not_stored_as_blank(value: str) -> None:
    """So `_create_plant` writes the dataset's 'Unknown' rather than `""`."""
    identification = _identification(family_name=value, health_effects=value)
    dumped = identification.model_dump(exclude_unset=True, exclude_none=True)
    assert "family_name" not in dumped
    assert "health_effects" not in dumped


def test_blank_list_entries_are_dropped_and_the_rest_trimmed() -> None:
    identification = _identification(common_names=["  Jequirity ", "", "   ", "Crab's eye"])
    assert identification.common_names == ["Jequirity", "Crab's eye"]


@pytest.mark.parametrize("value", [[], [""], ["  ", ""]])
def test_a_list_with_nothing_in_it_is_unanswered(value: list[str]) -> None:
    """`[]` is not the dataset's gap either; `['Unknown']` is."""
    assert _identification(common_names=value, toxic_parts=value).toxic_parts is None
    assert _identification(distribution=value).distribution is None


def test_a_structure_of_blanks_is_missing() -> None:
    """Blank nested answers collapse the same way `{}` does, so the warning fires."""
    identification = _identification(
        toxicity_by_species={"humans": " ", "notes": ""},
        toxin_profile={"primary_class": ""},
        morphological_attributes={"growth_form": "  ", "flowering_time": [""]},
    )
    assert identification.toxicity_by_species is None
    assert identification.toxin_profile is None
    assert identification.morphological_attributes is None


# ── species findings are a closed vocabulary ─────────────────────────────────


@pytest.mark.parametrize("typed", ["toxic", "TOXIC", " Toxic ", "tOxIc"])
def test_the_case_of_toxic_is_forgiven(typed: str) -> None:
    """It can only have meant one thing, and `toxic_to_humans` tests for 'Toxic'."""
    profile = ToxicityBySpecies(humans=typed)  # type: ignore[arg-type]
    assert profile.humans == "Toxic"


def test_unknown_is_accepted_and_normalised() -> None:
    assert ToxicityBySpecies(dogs="unknown").dogs == "Unknown"  # type: ignore[arg-type]


@pytest.mark.parametrize("typed", ["Yes", "true", "Mildly toxic", "Non-toxic", "1"])
def test_any_other_finding_is_refused(typed: str) -> None:
    """Accepted as-is, these read back as *not* toxic — the opposite of intended."""
    with pytest.raises(ValidationError, match="Toxic"):
        ToxicityBySpecies(humans=typed)  # type: ignore[arg-type]


def test_notes_stay_free_text() -> None:
    """Qualifications go here, which is why the species values can be closed."""
    profile = ToxicityBySpecies(humans="Toxic", notes="  mildly, seeds only ")
    assert profile.notes == "mildly, seeds only"
