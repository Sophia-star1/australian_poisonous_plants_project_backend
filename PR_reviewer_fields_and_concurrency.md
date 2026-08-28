# Reviewer form: real field shapes, and a guard against deciding on stale content

`feat/reviewer-fields-and-concurrency` → `dev`

Two changes to the review path.

## 1. The toxicity fields have a shape now

`toxin_profile`, `health_effects` and `morphological_attributes` were
`dict[str, Any]`. A reviewer could record the same fact under any key, so two
entries could disagree about what a thing is called and no client could rely
on finding either.

The keys are taken from the ten entries already in `plants`, all of which are
fully populated:

| column | keys | present in |
| --- | --- | --- |
| `toxin_profile` | `toxins`, `chemical_class`, `onset` | 10/10 |
| `health_effects` | `symptoms[]`, `toxidrome`, `target_organs[]` | 10/10 |
| `morphological_attributes` | `growth_habit`, `life_history`, `flower` | 10/10 |
| | `leaf` 7, `fruit` 3, `sap` 3, `odor`/`stem`/`armament` 1 | |

The two toxicity structures are **closed** — they are what triage actually
reads, and an invented key there is a fact nobody will find again. Morphology
stays **open**: describing a plant is not a closed set, and a reviewer who
needs to record a rhizome should not wait for a schema change. `symptoms` and
`target_organs` are lists because symptom search matches items, not sentences.

**Requiredness is unchanged.** `botanical_name` remains the only mandatory
field. Blocking a review on a full profile pushes reviewers to invent values;
the approval response already warns about what is missing.

An empty nested object now collapses to `None` — a stored `{}` is truthy and
would silence the "nothing recorded" warning while carrying no information.

Not touched: `plants.photographs`. Declared on the model, read or written by no
code anywhere, empty in all ten rows — superseded by `plant_images`.

## 2. A reviewer cannot decide on content they never saw

A contributor may edit while pending. Nothing was corrupt about that — every
mutation is already a conditional UPDATE guarded on `status = 'pending'`, so
whoever commits first wins. The gap was that a reviewer could approve a
**photograph swapped out while they were reading**, and publish it.

`content_version` is a digest of what the reviewer looks at: name,
description, location, coordinates, and the id and primary flag of every
photo. Returned with the submission, required back in the review body, and a
mismatch is `409`.

Excluded on purpose: `status`, `reviewer_notes`, `reviewed_by`, `reviewed_at`.
Those belong to the reviewer, and folding them in would make a reviewer's own
write invalidate the version they are holding.

**Derived, not stored.** `submissions` has no version column and the database
is not managed by Alembic — no migration history, no `alembic_version` table —
so adding one means hand-editing production. A digest also cannot drift from
the content it describes.

**Optimistic, not locked.** A lock needs an owner and a timeout, and any
timeout is wrong: too short and it expires mid-review, too long and a closed
tab freezes the submission for everyone. Here neither side is blocked and
whoever acts second is told what happened — symmetrically, since a reviewer
deciding first makes the contributor's edit hit the existing status guard.

`expected_version` is **required**. A guard a client can forget to send is not
a guard, approving publishes photographs, and there is no existing consumer to
break — the web reviewer UI does not exist yet.

## Breaking change

`PATCH /submissions/{id}` now requires `expected_version`, and `plant`'s three
JSONB fields reject unknown keys (except morphology). No client calls this
endpoint today.

## Verification

205 tests pass, 37 of them new; ruff 7 and mypy 16, both unchanged from `dev`.
