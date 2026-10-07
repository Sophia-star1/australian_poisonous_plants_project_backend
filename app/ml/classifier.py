"""Decide whether a submitted photo shows a plant.

An EfficientNet-B0 with one logit; `sigmoid(logit)` is the probability the
image contains a plant. Run through ONNX Runtime rather than PyTorch: the
exported graph is numerically identical (max deviation 1.4e-06 over random
inputs) while the dependency is 97 MB instead of 726 MB and resident memory
110 MB instead of 426 MB. The service runs on Cloud Run with a hard memory
cap, so that difference decides whether it runs at all.

## Three outcomes, not two

Measured against the project's own curated library — 104 photographs, every
one of them genuinely a plant — the model puts 7 of them below its 0.7
threshold. Rejecting on that threshold would turn away roughly one in fifteen
honest contributors with no way to appeal. The failures are not random:

* specimen shots — a fruit beside a centimetre rule, on a labelled card;
* bark and texture close-ups, no foliage in frame;
* plant parts that are not green, such as a maroon inflorescence.

Those are exactly the photographs a poisonous-plant record wants. So the
verdict is graded: only a confident negative is refused outright, and the
band between counts as "probably not, worth a look" — recorded on the
submission for the reviewer, not thrown back at the contributor.

    p < REJECT_BELOW      REJECT   refuse the upload
    p < ACCEPT_AT         FLAG     accept, mark ai_is_plant false
    otherwise             ACCEPT   accept, mark ai_is_plant true

## Preprocessing

Reimplemented over Pillow and NumPy instead of torchvision, and checked to be
exact: across all 104 library images the tensors are bit-identical to
`Resize(257) + CenterCrop(224) + ToTensor + Normalize` and no verdict moves.
The one subtlety is that torchvision truncates when it computes the resized
side, so `int()` is required where `round()` looks natural — rounding shifts
probabilities by as much as 0.06, enough to flip a borderline photo.
"""

from __future__ import annotations

import io
import threading
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from PIL import Image, ImageOps, UnidentifiedImageError

from app.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

WEIGHTS_DIR = Path(__file__).parent / "weights"
MODEL_PATH = WEIGHTS_DIR / "model.onnx"
META_PATH = WEIGHTS_DIR / "export_meta.yaml"

#: ImageNet statistics the model was trained against.
_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)

#: Ratio between the resize target and the crop, from the training transform.
_RESIZE_RATIO = 1.15

#: The most pixels `preprocess` will decode at full resolution.
#:
#: Decoding is where the memory goes, and it scales with pixels, not with
#: file size: measured at ~12 bytes per pixel at peak (the decoded RGB plus
#: the copies `exif_transpose` and `convert` make). An ordinary 48-megapixel
#: phone photo — 7.4 MB, inside the upload limit — peaks at 586 MB, so two at
#: once overrun the 1 GiB Cloud Run instance; a 17 KB PNG declaring 144
#: megapixels peaked at 865 MB on its own. At 13 MP the four screening
#: slots (`PLANT_SCREENING_CONCURRENCY`) stay near 620 MB together, on top of
#: the ~170 MB the process holds anyway.
#:
#: 13 rather than 12 so a standard 4032x3024 (12.2 MP) phone photo still takes
#: the full-resolution path, whose tensors are bit-identical to the training
#: transform.
MAX_DECODE_PIXELS = 13_000_000


class Decision(StrEnum):
    """What the pipeline should do with an upload."""

    ACCEPT = "accept"
    FLAG = "flag"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class Verdict:
    """One photo's result."""

    decision: Decision
    confidence: float

    @property
    def is_plant(self) -> bool:
        """What goes into `submissions.ai_is_plant`."""
        return self.decision is Decision.ACCEPT

    def as_dict(self) -> dict[str, Any]:
        """Shape stored in `submissions.ai_top_matches`."""
        return {"decision": str(self.decision), "plant_probability": self.confidence}


class ClassifierUnavailableError(RuntimeError):
    """The model could not be loaded."""


class UndecodableImageError(ValueError):
    """The bytes are not an image any decoder here understands."""


class ImageTooLargeError(ValueError):
    """The image declares more pixels than can be decoded safely."""


def _open_within_budget(data: bytes) -> Image.Image:
    """Open `data` so that decoding it cannot exceed `MAX_DECODE_PIXELS`.

    `Image.open` reads only the header, so the declared size is known before a
    single pixel is decoded. A JPEG over budget is decoded at a reduced scale
    instead — libjpeg can produce 1/2, 1/4 or 1/8 directly from the DCT
    coefficients, without ever holding the full image. The model sees 224 px
    in the end, so nothing it could use is lost; the tensor is no longer
    bit-identical to the training transform, which is acceptable for photos
    that would otherwise not be screenable at all. Other formats have no such
    path and are refused.

    Pillow's own guard is a backstop, not the limit: it only raises above
    ~179 MP and merely warns from ~89 MP.
    """
    try:
        opened = Image.open(io.BytesIO(data))
    except Image.DecompressionBombError as exc:
        raise ImageTooLargeError(str(exc)) from exc

    width, height = opened.size
    if width * height <= MAX_DECODE_PIXELS:
        return opened

    if opened.format == "JPEG":
        scale = 2
        while scale < 8 and -(-width // scale) * -(-height // scale) > MAX_DECODE_PIXELS:
            scale *= 2
        opened.draft("RGB", (-(-width // scale), -(-height // scale)))
        reduced_w, reduced_h = opened.size
        if reduced_w * reduced_h <= MAX_DECODE_PIXELS:
            return opened

    raise ImageTooLargeError(
        f"{width}x{height} is {width * height / 1e6:.0f} megapixels; the limit is "
        f"{MAX_DECODE_PIXELS / 1e6:.0f} for this format."
    )


def preprocess(data: bytes, size: int) -> np.ndarray:
    """Bytes to a normalised NCHW batch of one.

    EXIF orientation is applied first. Phone cameras record a sideways sensor
    plus a rotation flag rather than rotating the pixels, so without this a
    portrait photo reaches the model on its side — not what the model saw in
    training, and not what the contributor believes they sent.

    Raises:
        UndecodableImageError: the bytes are not an image.
        ImageTooLargeError: the pixel dimensions are beyond the decode budget.
    """
    try:
        opened = _open_within_budget(data)
        image: Image.Image = (ImageOps.exif_transpose(opened) or opened).convert("RGB")
    except ImageTooLargeError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise UndecodableImageError("The file could not be read as an image.") from exc

    width, height = image.size
    target = int(size * _RESIZE_RATIO)
    # int(), not round(): this mirrors torchvision's own integer arithmetic.
    # Rounding here moves probabilities by up to 0.06, which is more than the
    # gap between the decision bands.
    if width <= height:
        new_w, new_h = target, int(target * height / width)
    else:
        new_h, new_w = target, int(target * width / height)
    if (new_w, new_h) != (width, height):
        image = image.resize((new_w, new_h), Image.Resampling.BILINEAR)

    left = int(round((new_w - size) / 2.0))
    top = int(round((new_h - size) / 2.0))
    cropped = np.asarray(image.crop((left, top, left + size, top + size)), dtype=np.float32)

    normalised = (cropped / 255.0 - _MEAN) / _STD
    return np.ascontiguousarray(normalised.transpose(2, 0, 1)[None])


class PlantClassifier:
    """Loads the ONNX graph once and scores images against it."""

    def __init__(
        self,
        model_path: Path | None = None,
        meta_path: Path | None = None,
        *,
        reject_below: float | None = None,
        accept_at: float | None = None,
    ) -> None:
        import onnxruntime as ort  # imported here so a bare checkout can import this module

        # Resolved here rather than as a default argument: a default binds the
        # module constant once, at import, and then no amount of patching the
        # module reaches it — which silently defeats any attempt to point this
        # at a different file.
        model_path = MODEL_PATH if model_path is None else model_path
        meta_path = META_PATH if meta_path is None else meta_path

        if not model_path.exists():
            raise ClassifierUnavailableError(f"Model file missing: {model_path}")

        meta = yaml.safe_load(meta_path.read_text()) if meta_path.exists() else {}
        self.size = int(meta.get("img_size", 224))
        self.reject_below = (
            settings.PLANT_REJECT_BELOW if reject_below is None else reject_below
        )
        self.accept_at = settings.PLANT_ACCEPT_AT if accept_at is None else accept_at
        if not 0.0 <= self.reject_below <= self.accept_at <= 1.0:
            raise ValueError(
                f"Thresholds must satisfy 0 <= reject_below ({self.reject_below}) "
                f"<= accept_at ({self.accept_at}) <= 1."
            )

        options = ort.SessionOptions()
        # One thread: Cloud Run gives this service a single CPU, and letting
        # ONNX spawn a pool there costs context switching without adding work.
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        self._session = ort.InferenceSession(
            str(model_path), options, providers=["CPUExecutionProvider"]
        )
        self._input = self._session.get_inputs()[0].name
        # InferenceSession.run is thread-safe, but the surrounding bookkeeping
        # here is not worth reasoning about under a threadpool.
        self._lock = threading.Lock()
        logger.info(
            "plant_classifier_loaded",
            model=str(model_path.name),
            img_size=self.size,
            reject_below=self.reject_below,
            accept_at=self.accept_at,
        )

    def probability(self, data: bytes) -> float:
        """Probability that `data` shows a plant."""
        batch = preprocess(data, self.size)
        with self._lock:
            logit = self._session.run(None, {self._input: batch})[0]
        return float(1.0 / (1.0 + np.exp(-float(np.asarray(logit).reshape(-1)[0]))))

    def classify(self, data: bytes) -> Verdict:
        """Score one photo and grade it."""
        p = self.probability(data)
        if p < self.reject_below:
            decision = Decision.REJECT
        elif p < self.accept_at:
            decision = Decision.FLAG
        else:
            decision = Decision.ACCEPT
        return Verdict(decision=decision, confidence=round(p, 4))


_classifier: PlantClassifier | None = None
_load_lock = threading.Lock()
_load_failed = False


def get_classifier() -> PlantClassifier | None:
    """The process-wide classifier, or `None` when screening is off.

    Returns `None` rather than raising when the model is absent or disabled,
    so a checkout without the weights still boots and every test that does not
    care about screening keeps working — the same bargain `StorageService`
    makes. Callers treat `None` as "no opinion", never as "reject".

    Loading happens on first use behind a lock. It costs about 50 ms and 110 MB,
    which is cheap enough to pay lazily and wasteful to pay in a worker that
    never screens anything.
    """
    global _classifier, _load_failed
    if _classifier is not None or _load_failed:
        return _classifier
    if not settings.PLANT_CLASSIFIER_ENABLED:
        return None
    with _load_lock:
        if _classifier is None and not _load_failed:
            try:
                _classifier = PlantClassifier()
            except Exception as exc:  # noqa: BLE001 - degrade, never block uploads
                _load_failed = True
                logger.warning(
                    "plant_classifier_unavailable",
                    error=f"{type(exc).__name__}: {exc}",
                    detail="Uploads will not be screened.",
                )
    return _classifier


def reset_classifier() -> None:
    """Drop the cached instance. For tests that change configuration."""
    global _classifier, _load_failed
    _classifier = None
    _load_failed = False
