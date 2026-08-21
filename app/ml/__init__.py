"""Image classification used to screen submissions before they reach a reviewer.

A contributor uploads a photo they believe shows a plant. Screening it here
means a reviewer never spends time on a photo of a dog, a wall, or a blurred
thumb, and the contributor finds out immediately rather than days later.

The pieces live apart on purpose:

* `classifier.py` — loading the model and running it. Pure functions over
  bytes; no FastAPI, no database, so it can be exercised from a script.
* `weights/`      — the model files themselves. Under `app/` because the
  runtime image only copies `app/`, `alembic/` and `alembic.ini`; a directory
  at the repository root would be left out of the deployed container.

Nothing here imports from `app.api` or `app.services`. The dependency runs one
way: a service calls into `app.ml`, never the reverse.
"""
