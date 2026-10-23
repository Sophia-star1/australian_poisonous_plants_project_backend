# Plant / not-plant screening

Grades a submitted photo: refuse it, flag it, or let it through. EfficientNet-B0
with a single logit, `sigmoid(logit)` being the probability the frame contains
a plant.

## Layout

```
app/ml/
├── __init__.py       why this package exists
├── README.md         this file
├── classifier.py     load + preprocess + grade
└── weights/
    ├── model.onnx              what runs in production   16 MB
    ├── model.torchscript.pt    as delivered; git only    17 MB
    ├── export_meta.yaml        img_size, thresholds
    └── HANDOFF.md              notes from the model author
```

## Measured behaviour

Run against this project's own curated library — 104 photographs, every one of
them genuinely a plant — at the model's own 0.7 cutoff:

```
judged plant  97        rejected 7        false-rejection rate 6.7%
```

Lowering the threshold does not rescue it. The distribution is bimodal: 91 of
104 sit above 0.9, and the failures sit far below any usable cutoff.

```
0.0–0.3    4     0.3–0.5  2     0.5–0.7  1     0.7–0.9  6     0.9–1.0  91

threshold 0.5 → 5.8%     0.7 → 6.7%     0.9 → 12.5%
```

The failures are not random. They are specimen shots (a fruit beside a
centimetre rule on a labelled card, `p=0.0000`), bark and texture close-ups
with no foliage in frame (`p=0.21`), and plant parts that are not green (a
maroon inflorescence, `p=0.16`) — the sorts of photograph a poisonous-plant
record wants most. Hence three outcomes rather than two: refuse only a
confident negative, and let the middle band through with a mark on it.

Preprocessing choice shifts *which* images fail, not how many: centre crop
6.7%, whole-image squash 6.7%, five-crop maximum 4.8% at five times the cost.
One example moves a long way — a white-flowered `Acokanthera` scores 0.14
cropped and 0.91 squashed — so the centre crop does throw away signal, it just
does not throw away less of it on average.

## Why ONNX Runtime and not PyTorch

The handoff suggests `torch` + `torchvision`. Measured side by side:

|                             | installed | resident | per image |
| --------------------------- | --------- | -------- | --------- |
| `torch` + `torchvision`     | 726 MB    | 426 MB   | 114 ms    |
| `onnxruntime` + NumPy + PIL | 97 MB     | 110 MB   | 92 ms     |

The results are the same, and that was checked rather than assumed: the ONNX
graph exported from the delivered TorchScript agrees with it to 1.4e-06 over
random inputs, and the NumPy/Pillow preprocessing is byte-identical to
torchvision's across all 104 library images with no verdict moving.

This matters because Cloud Run runs the service under a hard memory cap. At
426 MB for the model alone the container does not fit in the current 512Mi and
gets killed; ONNX Runtime fits with room for the rest of the application.

The one trap: torchvision truncates when computing the resized long side.
Writing `round()` there — which reads more naturally — shifts probabilities by
up to 0.06, comfortably enough to move a photo across a threshold.

## Configuration

| setting                     | default | meaning                              |
| --------------------------- | ------- | ------------------------------------ |
| `PLANT_CLASSIFIER_ENABLED`  | `true`  | off switch                           |
| `PLANT_REJECT_BELOW`        | `0.50`  | under this the upload is refused     |
| `PLANT_ACCEPT_AT`           | `0.70`  | at or over this it counts as a plant |

`PLANT_REJECT_BELOW` was `0.10`, which rejected 1 of the 104 library images,
but ordinary non-plants landed above it and were accepted: screenshots scored
0.24–0.49 and a blank frame about 0.32. At `0.50` those are refused, at the
cost of 6 of the 104 library images (5.8%) — the specimen shots, bark
close-ups and non-green parts described above. Because every photo in a
submission must pass, an honest submission's chance of being refused grows
with its photo count. Lower the variable to trade back; it needs no code
change.

`get_classifier()` returns `None` when the weights are absent or the feature is
off, and callers read that as "no opinion" — never as a rejection. A checkout
without the model still boots and still accepts uploads.

`weights/` sits under `app/` deliberately. The runtime stage of the Dockerfile
copies exactly two directories from the source tree:

```dockerfile
COPY --from=builder /build/app ./app
COPY --from=builder /build/sql ./sql
```

A `models/` directory at the repository root would be built into the image and
then dropped from it — the code would work locally and fail in Cloud Run with
a missing-file error on the first upload after deploy.

## Where it plugs in

`SubmissionService.screen()`, called from `create()` before any upload.

```
POST /submissions/
  ├─ content-type whitelist, photo count           existing checks
  ├─ screen(): read → classify → rewind            ← here
  │     any photo REJECT    → 422, nothing stored
  │     otherwise           → carry on
  ├─ upload each photo to submission-images
  └─ insert the row, with ai_is_plant / ai_top_matches
```

Screening runs first so a refusal costs no storage — uploading and then
deleting would pay bandwidth for exactly the traffic this exists to turn away.
Each photo is read, scored, and released, so peak memory is one photo rather
than all ten; `read_upload(rewind=True)` leaves the handle at the start for the
upload that follows. Without that rewind the upload reads an already-consumed
file and stores nothing, with no error anywhere, which is what
`test_submission_screening.py` pins first.

The verdict lands in columns that already existed:

| column           | type    | meaning                                       |
| ---------------- | ------- | --------------------------------------------- |
| `ai_is_plant`    | boolean | `NULL` when screening did not run — distinct from `false` |
| `ai_top_matches` | jsonb   | `{"screening": {"plant_probability": …, "photos": […]}}` |

**A submission is refused when any photo is a confident negative.** Every
photo in a submission must show the plant, and each one is judged exactly as
it would be on its own. Refusing only an all-negative set let a single plant
photo carry any number of non-plant ones through. The uncertain band is still
accepted and marked for the reviewer, for the reason above: it is where the
specimen shots and bark close-ups land.

## Still outstanding before this can be switched on

**Raise the memory limit.** `cloudbuild.yaml` deploys with `--memory 512Mi`.
Roughly 110 MB of that goes to the model, plus whatever decoding the upload
costs — a 4000×3000 phone photo is 36 MB of raw RGB before any resizing. 1Gi
is the sensible floor.

**Do not call this from the event loop.** Inference is CPU-bound and takes
about 90 ms. Called directly inside an `async` endpoint it stalls every other
request on that instance for that whole time. It has to go through
`run_in_threadpool`.

**First call pays for the load.** `--min-instances 0`, so an idle service
scales to zero and each new instance loads the model on the first upload it
handles: about 50 ms, once. Acceptable lazily; move it to startup if it
ever is not.

**There is no negative test set.** Every number above is measured against
photographs that really are plants, so the false-rejection rate is solid and
the false-acceptance rate is unknown. The model author notes it was trained
against a narrow negative set — food, dogs, faces — and gives middling scores
to other non-plant subjects rather than confidently refusing them. Before
trusting `PLANT_REJECT_BELOW` in production, a few hundred genuine non-plant
uploads should be scored to see where they actually land.

## Notes on the delivery

`serve.py` arrived empty (0 bytes) and the `Dockerfile` the handoff refers to
was not included, so "Option A", running the model as its own HTTP service,
is not usable as delivered. `model.onnx` here was exported locally from the
supplied TorchScript rather than taken from the author's own export; the two
should agree, but if the official one is fetched it is worth re-running
`tests/test_plant_classifier.py`, whose golden values pin the current graph.

Checkpoint formats are declared `binary` in `.gitattributes`. Without that the
repo-wide `* text=auto eol=lf` rule decides by heuristic, and a misjudged file
gets its bytes rewritten — it still commits cleanly and fails only when
something tries to load it.

`model.torchscript.pt` is in git but excluded from the build context via
`.gcloudignore`: it is worth keeping so the export can be reproduced, and worth
keeping out of a 17 MB-larger image that never reads it.
