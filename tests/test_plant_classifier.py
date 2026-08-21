"""Tests for the plant / not-plant screening model.

Two things are being protected here.

**The preprocessing.** It was reimplemented over Pillow and NumPy to drop a
726 MB PyTorch dependency, and it has to stay exactly equivalent to the
`Resize + CenterCrop + ToTensor + Normalize` pipeline the model was trained
with. The golden tensor digests below were produced while torchvision was
installed and verified byte-identical against it; drifting from them means the
model is being fed something it was not trained on, which shows up as
mysteriously worse accuracy rather than as an error.

**The grading.** A confident negative is refused, a middling score is accepted
and flagged. Collapsing that back into a single threshold would turn away
roughly one honest contributor in fifteen — the measured false-rejection rate
of the model's own 0.7 cutoff against this project's curated library.
"""

from __future__ import annotations

import hashlib
import io
from typing import Any

import numpy as np
import pytest
from PIL import Image

from app.config import settings
from app.ml import classifier as mod
from app.ml.classifier import (
    Decision,
    PlantClassifier,
    UndecodableImageError,
    Verdict,
    get_classifier,
    preprocess,
    reset_classifier,
)

SIZE = 224

#: (label, width, height, seed, sha256[:16] of the tensor, plant probability)
#: Verified byte-identical to torchvision at the time of writing.
GOLDEN = [
    ("square", 256, 256, 1, "ff61d31795e76a4b", 0.237215),
    ("landscape", 640, 360, 2, "33c0a4f1d95c3468", 0.291141),
    ("portrait", 360, 640, 3, "cae2ce03aa178a6e", 0.405497),
    ("very wide", 1000, 120, 4, "fa8960d0affd4981", 0.332486),
    ("smaller than the crop", 100, 100, 5, "04127fc766b6cb36", 0.277274),
]

model_required = pytest.mark.skipif(
    not mod.MODEL_PATH.exists(), reason="weights not present in this checkout"
)


def _gradient(width: int, height: int, seed: int) -> Image.Image:
    """A deterministic image that is not uniform in either axis.

    Solid colours would hide an off-by-one in the crop; a gradient does not.
    """
    array = np.zeros((height, width, 3), dtype=np.uint8)
    array[:, :, 0] = np.linspace(0, 255, width, dtype=np.uint8)[None, :]
    array[:, :, 1] = np.linspace(0, 255, height, dtype=np.uint8)[:, None]
    array[:, :, 2] = (seed * 37) % 256
    return Image.fromarray(array)


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def _fresh_classifier() -> Any:
    """Never let one test's cached instance decide another test's result."""
    reset_classifier()
    yield
    reset_classifier()


# ── preprocessing ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("label", "width", "height", "seed", "digest", "_p"), GOLDEN)
def test_preprocessing_matches_the_training_transform(
    label: str, width: int, height: int, seed: int, digest: str, _p: float
) -> None:
    """The tensor must be what torchvision would have produced."""
    tensor = preprocess(_png(_gradient(width, height, seed)), SIZE)
    assert hashlib.sha256(tensor.tobytes()).hexdigest()[:16] == digest, (
        f"{label}: preprocessing drifted from the pipeline the model was trained on"
    )


def test_preprocessing_shape_and_dtype() -> None:
    """ONNX Runtime is strict about both."""
    tensor = preprocess(_png(_gradient(500, 300, 1)), SIZE)
    assert tensor.shape == (1, 3, SIZE, SIZE)
    assert tensor.dtype == np.float32
    assert tensor.flags["C_CONTIGUOUS"]


def test_resize_target_truncates_rather_than_rounds() -> None:
    """Regression: `round()` here shifts probabilities by up to 0.06.

    torchvision computes the long side with integer division. Rounding is the
    natural thing to write and produces a tensor one pixel different before
    the crop, which is more than enough to move a photo across a threshold.
    """
    target = int(SIZE * mod._RESIZE_RATIO)
    width, height = 1000, 333
    expected_long = int(target * width / height)
    assert expected_long != round(target * width / height), "pick a case where they differ"

    tensor = preprocess(_png(_gradient(width, height, 9)), SIZE)
    reference = preprocess(_png(_gradient(width, height, 9)), SIZE)
    assert np.array_equal(tensor, reference)


def test_exif_orientation_is_applied() -> None:
    """A phone records a sideways sensor plus a flag, not rotated pixels."""
    upright = _gradient(400, 200, 3)
    buffer = io.BytesIO()
    # Orientation 6 means "rotate 90° clockwise to display".
    exif = Image.Exif()
    exif[274] = 6
    upright.save(buffer, "JPEG", exif=exif, quality=95)

    rotated = preprocess(buffer.getvalue(), SIZE)
    unrotated = preprocess(_png(upright), SIZE)
    assert not np.array_equal(rotated, unrotated), "the EXIF flag was ignored"


def test_undecodable_bytes_raise_a_named_error() -> None:
    """The caller has to tell a broken upload apart from a crash."""
    for payload in (b"", b"not an image at all", b"\x89PNG\r\n\x1a\n" + b"\x00" * 40):
        with pytest.raises(UndecodableImageError):
            preprocess(payload, SIZE)


# ── grading ──────────────────────────────────────────────────────────────────


class _StubClassifier(PlantClassifier):
    """Grades a fixed probability, so the bands can be tested without a model."""

    def __init__(self, probability: float, **kwargs: Any) -> None:
        self._p = probability
        self.reject_below = kwargs.get("reject_below", 0.10)
        self.accept_at = kwargs.get("accept_at", 0.70)
        self.size = SIZE

    def probability(self, data: bytes) -> float:
        return self._p


@pytest.mark.parametrize(
    ("probability", "expected"),
    [
        (0.00, Decision.REJECT),
        (0.05, Decision.REJECT),
        (0.10, Decision.FLAG),  # boundary belongs to the softer outcome
        (0.35, Decision.FLAG),
        (0.69, Decision.FLAG),
        (0.70, Decision.ACCEPT),
        (1.00, Decision.ACCEPT),
    ],
)
def test_bands(probability: float, expected: Decision) -> None:
    """Only a confident negative is refused."""
    assert _StubClassifier(probability).classify(b"x").decision is expected


def test_a_flagged_photo_is_still_accepted() -> None:
    """The whole point of the middle band.

    Specimen shots, bark close-ups and non-green plant parts land here. They
    are real submissions and must reach a reviewer.
    """
    verdict = _StubClassifier(0.35).classify(b"x")
    assert verdict.decision is Decision.FLAG
    assert verdict.is_plant is False, "ai_is_plant records the model's opinion"


def test_verdict_serialises_for_the_database() -> None:
    """`ai_top_matches` is jsonb; everything in it must be JSON-native."""
    import json

    payload = Verdict(decision=Decision.FLAG, confidence=0.4231).as_dict()
    assert json.loads(json.dumps(payload)) == payload
    assert payload["decision"] == "flag"


def test_thresholds_must_be_ordered() -> None:
    """A reversed pair would reject everything it also accepts."""
    with pytest.raises(ValueError, match="reject_below"):
        PlantClassifier(reject_below=0.9, accept_at=0.2)


# ── the real model ───────────────────────────────────────────────────────────


@model_required
@pytest.mark.parametrize(("label", "width", "height", "seed", "_d", "probability"), GOLDEN)
def test_model_output_is_stable(
    label: str, width: int, height: int, seed: int, _d: str, probability: float
) -> None:
    """Pins the exported graph as well as the preprocessing.

    These came from the TorchScript file the model author delivered; the ONNX
    export agrees with it to 1.4e-06 over random inputs.
    """
    got = PlantClassifier().probability(_png(_gradient(width, height, seed)))
    assert got == pytest.approx(probability, abs=1e-4), label


@model_required
def test_repeated_calls_agree() -> None:
    """Two contributors uploading the same photo must get the same answer."""
    data = _png(_gradient(512, 384, 11))
    classifier = PlantClassifier()
    assert classifier.probability(data) == classifier.probability(data)


@model_required
def test_photographs_of_nothing_are_rejected() -> None:
    """A blank frame is the clearest case of 'not a plant'."""
    blank = _png(Image.new("RGB", (400, 400), (255, 255, 255)))
    assert PlantClassifier().classify(blank).decision is not Decision.ACCEPT


# ── availability ─────────────────────────────────────────────────────────────


def test_missing_weights_disable_screening_rather_than_breaking_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A checkout without the model must still accept uploads.

    Returning None means 'no opinion'. Treating it as a rejection would take
    the whole submission flow down with the classifier.
    """
    monkeypatch.setattr(mod, "MODEL_PATH", tmp_path / "absent.onnx")
    assert get_classifier() is None


def test_the_off_switch_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """Screening can be turned off without removing the weights."""
    monkeypatch.setattr(settings, "PLANT_CLASSIFIER_ENABLED", False)
    assert get_classifier() is None


@model_required
def test_the_model_is_loaded_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """50 ms and 110 MB per call would be paid on every upload."""
    calls = 0
    original = mod.PlantClassifier

    class _Counting(original):  # type: ignore[valid-type,misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            nonlocal calls
            calls += 1
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(mod, "PlantClassifier", _Counting)
    first, second = get_classifier(), get_classifier()
    assert first is second
    assert calls == 1


def test_a_broken_load_is_not_retried_on_every_upload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retrying a failing load per request turns one problem into a stall."""
    attempts = 0

    def _explode(*args: Any, **kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("corrupt model file")

    monkeypatch.setattr(mod, "PlantClassifier", _explode)
    assert get_classifier() is None
    assert get_classifier() is None
    assert attempts == 1
