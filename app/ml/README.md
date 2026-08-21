# Plant / not-plant screening

Rejects an upload whose photo is not a plant, before the submission row is
written and before a reviewer ever sees it.

## Layout

```
app/ml/
├── __init__.py       why this package exists
├── README.md         this file
├── classifier.py     load + predict  (added once the model is in hand)
└── weights/          the model files
```

`weights/` sits under `app/` deliberately. The runtime stage of the Dockerfile
copies exactly three things:

```dockerfile
COPY --from=builder /build/app ./app
COPY --from=builder /build/alembic ./alembic
COPY --from=builder /build/alembic.ini ./alembic.ini
```

A `models/` directory at the repository root would be built into the image and
then dropped from it — the code would work locally and fail in Cloud Run with
a missing-file error on the first upload after deploy.

## Where it plugs in

`submissions` already carries two columns for this, unused so far:

| column           | type    | meaning                                  |
| ---------------- | ------- | ---------------------------------------- |
| `ai_is_plant`    | boolean | verdict, `NULL` when screening was skipped |
| `ai_top_matches` | jsonb   | scores, for a reviewer to second-guess with |

So the result has somewhere to live without a migration.

The call belongs in `SubmissionService.create`, next to the upload: photos are
already read into memory there, and a rejection has to happen **before** the
first byte reaches storage, otherwise a refused submission still leaves files
in the bucket. `StorageError` is already mapped to a 422 by the endpoint, so a
rejection can follow the same path.

## Constraints worth knowing before choosing a model

**Memory.** Cloud Run runs this service at `512Mi` with `1` CPU
(`cloudbuild.yaml`). Python plus FastAPI plus SQLAlchemy already occupy a
sizeable share of that. A PyTorch or TensorFlow runtime alone is typically
several hundred MB of resident memory before any weights load — expect to
raise the limit, and note that memory is billed per instance for the whole
request duration.

**Cold start.** `--min-instances 0`, so an idle service scales to zero. Model
loading happens once per instance, on the first request that instance serves;
whatever that costs is added to some unlucky contributor's upload. Loading at
module import (during startup) moves the cost into the container's boot rather
than into a request.

**Image size.** Weights baked into the image lengthen every deploy and every
cold start. Under roughly 50 MB this is fine. Above that, prefer downloading
from a Supabase bucket on startup and caching to `/tmp`.

**Blocking.** Inference is CPU-bound, so calling it directly inside an `async`
endpoint stalls the event loop for every other request on that instance. Run
it through `run_in_threadpool` (or a worker), not inline.

## Git

Binary formats are declared in `.gitattributes` so the repo-wide
`* text=auto eol=lf` rule cannot rewrite bytes inside a checkpoint. Anything
past about 50 MB should not go into git directly — `git-lfs` is not installed
here, so that means either installing it or keeping the file out of the
repository and fetching it at startup.
