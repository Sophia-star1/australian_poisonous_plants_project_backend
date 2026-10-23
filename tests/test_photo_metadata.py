"""Contributed photos lose their personal metadata, and nothing else.

Each case builds a real image file carrying a GPS position, then checks that
the stripped file still decodes to exactly the same pixels, keeps what is
needed to display it (orientation, colour profile), and holds none of the
rest.
"""

from __future__ import annotations

import io
import struct
from typing import Any

import numpy as np
import pytest
from PIL import Image, ImageOps, PngImagePlugin
from starlette.datastructures import Headers, UploadFile

from app.config import settings
from app.services import photo_metadata
from app.services.photo_metadata import MalformedImageError, strip_metadata
from app.services.storage_service import StorageService, StoredFile

GPS_IFD = 0x8825
ORIENTATION = 0x0112
MAKE = 0x010F
XMP = b"http://ns.adobe.com/xap/1.0/\x00<x:xmpmeta>Owner: Ada, near Canberra</x:xmpmeta>"
ICC = b"\x00" * 128 + b"fake icc profile body"


def _exif(orientation: int = 6) -> Image.Exif:
    exif = Image.Exif()
    exif[ORIENTATION] = orientation
    exif[MAKE] = "PhoneMaker"
    exif.get_ifd(GPS_IFD).update({1: "S", 2: (35.0, 16.0, 50.0), 3: "E", 4: (149.0, 7.0, 49.0)})
    return exif


def _picture(mode: str = "RGB") -> Image.Image:
    rng = np.random.default_rng(7)
    return Image.fromarray(rng.integers(0, 255, (48, 64, 3), dtype=np.uint8)).convert(mode)


def _pixels(data: bytes) -> np.ndarray[Any, Any]:
    with Image.open(io.BytesIO(data)) as image:
        return np.asarray(image.convert("RGB"))


def _jpeg(**save: Any) -> bytes:
    out = io.BytesIO()
    _picture().save(out, "JPEG", exif=_exif(), icc_profile=ICC, comment=b"Ada's garden", **save)
    data = out.getvalue()
    # XMP as a second APP1 right after SOI, as cameras write it.
    xmp = b"\xff\xe1" + struct.pack(">H", len(XMP) + 2) + XMP
    return data[:2] + xmp + data[2:] + b"TRAILER:serial=1234"


@pytest.mark.parametrize("progressive", [False, True], ids=["baseline", "progressive"])
def test_a_jpeg_keeps_its_pixels_orientation_and_colour_but_nothing_personal(
    progressive: bool,
) -> None:
    original = _jpeg(progressive=progressive)
    secrets = (b"PhoneMaker", b"Canberra", b"Ada's garden", b"TRAILER")
    assert all(secret in original for secret in secrets)
    with Image.open(io.BytesIO(original)) as before:
        assert before.getexif().get_ifd(GPS_IFD)

    stripped = strip_metadata(original)

    assert np.array_equal(_pixels(stripped), _pixels(original))
    with Image.open(io.BytesIO(stripped)) as image:
        exif = image.getexif()
        assert dict(exif) == {ORIENTATION: 6}
        assert not exif.get_ifd(GPS_IFD)
        assert image.info.get("icc_profile") == ICC
        assert "comment" not in image.info
        # Shown upright exactly as the original would be.
        with Image.open(io.BytesIO(original)) as before:
            assert np.array_equal(
                np.asarray(ImageOps.exif_transpose(image)),
                np.asarray(ImageOps.exif_transpose(before)),
            )
    for secret in secrets:
        assert secret not in stripped
    assert stripped.endswith(b"\xff\xd9")


def test_an_upright_jpeg_carries_no_exif_at_all() -> None:
    out = io.BytesIO()
    _picture().save(out, "JPEG", exif=_exif(orientation=1))
    stripped = strip_metadata(out.getvalue())
    with Image.open(io.BytesIO(stripped)) as image:
        assert dict(image.getexif()) == {}


def test_a_png_loses_its_exif_and_text_but_keeps_orientation() -> None:
    info = PngImagePlugin.PngInfo()
    info.add_text("Author", "Ada")
    info.add_itxt("Location", "near Canberra")
    out = io.BytesIO()
    _picture().save(out, "PNG", exif=_exif(orientation=3), pnginfo=info)
    original = out.getvalue()
    assert all(secret in original for secret in (b"PhoneMaker", b"Canberra", b"Ada"))

    stripped = strip_metadata(original)

    assert np.array_equal(_pixels(stripped), _pixels(original))
    with Image.open(io.BytesIO(stripped)) as image:
        image.load()
        assert dict(image.getexif()) == {ORIENTATION: 3}
        assert "Author" not in image.info and "Location" not in image.info
    for secret in (b"PhoneMaker", b"Canberra", b"Ada"):
        assert secret not in stripped


def test_a_webp_loses_exif_and_xmp_and_its_header_says_so() -> None:
    out = io.BytesIO()
    _picture().save(out, "WEBP", lossless=True, exif=_exif(), xmp=XMP)
    original = out.getvalue()
    assert b"PhoneMaker" in original and b"Canberra" in original

    stripped = strip_metadata(original)

    assert np.array_equal(_pixels(stripped), _pixels(original))
    assert b"EXIF" not in stripped and b"XMP " not in stripped
    assert b"PhoneMaker" not in stripped and b"Canberra" not in stripped
    assert struct.unpack_from("<I", stripped, 4)[0] == len(stripped) - 8
    assert stripped[12:16] == b"VP8X" and stripped[20] & 0x0C == 0


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP"])
def test_a_photo_without_metadata_is_returned_untouched(fmt: str) -> None:
    out = io.BytesIO()
    _picture().save(out, fmt)
    data = out.getvalue()
    assert strip_metadata(data) is data


@pytest.mark.parametrize("data", [b"", b"bytes", b"GIF89a....", b"\x00\x00\x00\x18ftypheic"])
def test_bytes_that_are_not_a_supported_image_are_left_alone(data: bytes) -> None:
    assert strip_metadata(data) is data


@pytest.mark.parametrize(
    "data",
    [
        b"\xff\xd8\x00\x00",  # no marker where one must be
        b"\xff\xd8\xff\xe1\x00\x40Exif",  # segment longer than the file
        photo_metadata.PNG_SIGNATURE + struct.pack(">I", 100) + b"tEXt" + b"short",
        b"RIFF\x20\x00\x00\x00WEBPVP8X\x40\x00\x00\x00\x00",
    ],
    ids=["jpeg marker", "jpeg length", "png chunk", "webp chunk"],
)
def test_a_broken_structure_is_refused_rather_than_passed_through(data: bytes) -> None:
    with pytest.raises(MalformedImageError):
        strip_metadata(data)


def test_orientation_is_read_from_either_byte_order() -> None:
    for order in ("<", ">"):
        mark = b"II" if order == "<" else b"MM"
        tiff = (
            mark
            + struct.pack(order + "HI", 42, 8)
            + struct.pack(order + "H", 1)
            + struct.pack(order + "HHIHH", ORIENTATION, 3, 1, 8, 0)
        )
        assert photo_metadata.orientation_from_tiff(tiff) == 8
    assert photo_metadata.orientation_from_tiff(b"junk") is None


# ── on the upload path ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_contributed_photo_is_stored_without_its_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setattr(settings, "SUPABASE_SERVICE_ROLE_KEY", "service-key")
    stored: list[bytes] = []

    async def _upload_bytes(
        self: Any, data: bytes, key: str, bucket: str, ctype: str
    ) -> StoredFile:
        stored.append(data)
        return StoredFile(key=key, url=key)

    monkeypatch.setattr(StorageService, "upload_bytes", _upload_bytes)
    photo = UploadFile(
        file=io.BytesIO(_jpeg()), filename="a.jpg", headers=Headers({"content-type": "image/jpeg"})
    )

    await StorageService().upload_file(photo, "submissions/1/a.jpg")

    assert b"PhoneMaker" not in stored[0] and b"Canberra" not in stored[0]


@pytest.mark.asyncio
async def test_a_photo_too_broken_to_strip_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.storage_service import StorageError

    photo = UploadFile(
        file=io.BytesIO(b"\xff\xd8\x00\x00"),
        filename="a.jpg",
        headers=Headers({"content-type": "image/jpeg"}),
    )
    with pytest.raises(StorageError, match="not a readable image"):
        await StorageService().upload_file(photo, "submissions/1/a.jpg")


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP"])
def test_stripping_a_stripped_photo_changes_nothing(fmt: str) -> None:
    """Re-running the cleanup must not store every portrait photo again."""
    out = io.BytesIO()
    _picture().save(out, fmt, exif=_exif(orientation=6))
    once = strip_metadata(out.getvalue())
    assert once != out.getvalue()
    assert strip_metadata(once) is once
