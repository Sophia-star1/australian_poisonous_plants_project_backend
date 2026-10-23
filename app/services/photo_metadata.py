"""Remove personal metadata from contributed photos, without re-encoding them.

A phone writes the GPS position, the time, the device and often the owner's
name into every photo. Contributor photos are served from public URLs, and
approved ones join the public plant library, so whoever downloads one could
read where it was taken — often the contributor's own garden. Avatars lose
their metadata by being re-encoded (`avatar_service`); submission photos are
kept as the record of what the reviewer saw, so here only the metadata goes:
the compressed image data is copied byte for byte and the pixels do not
change.

What is removed: EXIF (GPS, time, camera, serial numbers, the embedded
thumbnail), XMP, IPTC/Photoshop blocks, comments and vendor blocks, PNG text
chunks, and anything after the end of a JPEG (multi-picture extras, vendor
trailers). What is kept: everything needed to show the image the same way —
JPEG's JFIF and Adobe blocks, ICC colour profiles, and the EXIF orientation,
rewritten as a minimal EXIF block holding nothing else, so a portrait photo
does not turn sideways.

Bytes that are not JPEG, PNG or WebP are returned unchanged: screening has
already refused anything it cannot decode, and nothing else is accepted.
"""

from __future__ import annotations

import struct
import zlib

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

#: EXIF tag holding the rotation a viewer must apply.
_ORIENTATION_TAG = 0x0112

#: PNG chunks that carry text, time or EXIF. Everything else is kept.
_PNG_METADATA = {b"eXIf", b"tEXt", b"zTXt", b"iTXt", b"tIME"}

#: WebP chunks that carry metadata, and their flag bits in the VP8X header.
_WEBP_METADATA = {b"EXIF", b"XMP "}
_WEBP_METADATA_FLAGS = 0x08 | 0x04


class MalformedImageError(ValueError):
    """The bytes claim to be a JPEG, PNG or WebP but their structure is broken."""


def strip_metadata(data: bytes) -> bytes:
    """`data` without its metadata; the same object if there was none to remove.

    "None to remove" includes a photo stripped before: its orientation-only
    EXIF is rebuilt identically, and callers rely on getting `data` back
    unchanged to know there is nothing to store again.
    """
    if data.startswith(b"\xff\xd8"):
        stripped = _strip_jpeg(data)
    elif data.startswith(PNG_SIGNATURE):
        stripped = _strip_png(data)
    elif len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        stripped = _strip_webp(data)
    else:
        return data
    return data if stripped == data else stripped


# ── orientation ──────────────────────────────────────────────────────────────


def orientation_from_tiff(tiff: bytes) -> int | None:
    """The orientation (1–8) in a TIFF/EXIF structure, or None if absent or odd."""
    if len(tiff) < 8 or tiff[:2] not in (b"II", b"MM"):
        return None
    endian = "<" if tiff[:2] == b"II" else ">"
    (ifd,) = struct.unpack_from(endian + "I", tiff, 4)
    if ifd + 2 > len(tiff):
        return None
    (count,) = struct.unpack_from(endian + "H", tiff, ifd)
    for index in range(count):
        entry = ifd + 2 + 12 * index
        if entry + 12 > len(tiff):
            return None
        tag, kind, number = struct.unpack_from(endian + "HHI", tiff, entry)
        if tag == _ORIENTATION_TAG and kind == 3 and number >= 1:
            (value,) = struct.unpack_from(endian + "H", tiff, entry + 8)
            return value if 1 <= value <= 8 else None
    return None


def _orientation_only_tiff(orientation: int) -> bytes:
    """A TIFF structure with one IFD holding only the orientation tag."""
    return (
        b"MM\x00\x2a"
        + struct.pack(">I", 8)
        + struct.pack(">H", 1)
        + struct.pack(">HHIHH", _ORIENTATION_TAG, 3, 1, orientation, 0)
        + struct.pack(">I", 0)
    )


# ── JPEG ─────────────────────────────────────────────────────────────────────


def _jpeg_keeps(marker: int, payload: bytes) -> bool:
    """Whether a segment is needed to display the image."""
    if marker == 0xE2:  # APP2: keep the ICC profile, drop multi-picture/FlashPix
        return payload.startswith(b"ICC_PROFILE\x00")
    if 0xE0 <= marker <= 0xEF:  # APP0 (JFIF) and APP14 (Adobe colour) only
        return marker in (0xE0, 0xEE)
    return marker != 0xFE  # COM: free text


def _scan_end(data: bytes, pos: int) -> int:
    """Where the entropy-coded data starting at `pos` ends: the next real marker."""
    while True:
        found = data.find(b"\xff", pos)
        if found == -1 or found + 1 >= len(data):
            return len(data)
        following = data[found + 1]
        # 0xFF00 is an escaped data byte and 0xFFD0-0xFFD7 restart markers,
        # both part of the scan; 0xFFFF is fill before a marker.
        if following == 0x00 or 0xD0 <= following <= 0xD7 or following == 0xFF:
            pos = found + 1
            continue
        return found


def _strip_jpeg(data: bytes) -> bytes:
    kept: list[bytes] = []
    orientation: int | None = None
    changed = False
    pos = 2
    size = len(data)
    while pos < size:
        if data[pos] != 0xFF:
            raise MalformedImageError("Expected a JPEG marker.")
        while pos < size and data[pos] == 0xFF:
            pos += 1
        if pos >= size:
            break
        marker = data[pos]
        pos += 1
        if marker == 0xD9:  # end of image: anything after it is dropped
            kept.append(b"\xff\xd9")
            changed = changed or pos < size
            break
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:  # no length, no payload
            kept.append(bytes((0xFF, marker)))
            continue
        if pos + 2 > size:
            raise MalformedImageError("Truncated JPEG segment.")
        (length,) = struct.unpack_from(">H", data, pos)
        if length < 2 or pos + length > size:
            raise MalformedImageError("Bad JPEG segment length.")
        payload = data[pos + 2 : pos + length]
        segment = bytes((0xFF, marker)) + data[pos : pos + length]
        pos += length
        if not _jpeg_keeps(marker, payload):
            if marker == 0xE1 and payload.startswith(b"Exif\x00\x00") and orientation is None:
                orientation = orientation_from_tiff(payload[6:])
            changed = True
            continue
        kept.append(segment)
        if marker == 0xDA:  # start of scan: copy the compressed data verbatim
            end = _scan_end(data, pos)
            kept.append(data[pos:end])
            pos = end
    if not changed:
        return data

    head = [b"\xff\xd8"]
    if kept and kept[0][1] == 0xE0:  # JFIF must stay first
        head.append(kept.pop(0))
    if orientation is not None and orientation != 1:
        exif = b"Exif\x00\x00" + _orientation_only_tiff(orientation)
        head.append(b"\xff\xe1" + struct.pack(">H", len(exif) + 2) + exif)
    return b"".join(head + kept)


# ── PNG ──────────────────────────────────────────────────────────────────────


def _png_chunks(data: bytes) -> list[tuple[bytes, bytes, bytes]]:
    """(type, payload, whole chunk) for each chunk, stopping after IEND."""
    chunks = []
    pos = len(PNG_SIGNATURE)
    while pos + 8 <= len(data):
        (length,) = struct.unpack_from(">I", data, pos)
        kind = data[pos + 4 : pos + 8]
        end = pos + 12 + length
        if end > len(data):
            raise MalformedImageError("Truncated PNG chunk.")
        chunks.append((kind, data[pos + 8 : pos + 8 + length], data[pos:end]))
        pos = end
        if kind == b"IEND":
            break
    return chunks


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)


def _strip_png(data: bytes) -> bytes:
    chunks = _png_chunks(data)
    if not any(kind in _PNG_METADATA for kind, _, _ in chunks):
        return data
    orientation = next(
        (orientation_from_tiff(payload) for kind, payload, _ in chunks if kind == b"eXIf"), None
    )
    out = [PNG_SIGNATURE]
    for kind, _, whole in chunks:
        if kind in _PNG_METADATA:
            continue
        if kind == b"IDAT" and orientation is not None and orientation != 1:
            out.append(_png_chunk(b"eXIf", _orientation_only_tiff(orientation)))
            orientation = None
        out.append(whole)
    return b"".join(out)


# ── WebP ─────────────────────────────────────────────────────────────────────


def _strip_webp(data: bytes) -> bytes:
    (riff_size,) = struct.unpack_from("<I", data, 4)
    end = min(len(data), 8 + riff_size)
    chunks: list[bytes] = []
    changed = False
    pos = 12
    while pos + 8 <= end:
        kind = data[pos : pos + 4]
        (length,) = struct.unpack_from("<I", data, pos + 4)
        if pos + 8 + length > end:
            raise MalformedImageError("Truncated WebP chunk.")
        chunk = data[pos : pos + 8 + length] + (b"\x00" if length % 2 else b"")
        pos += 8 + length + length % 2
        if kind in _WEBP_METADATA:
            changed = True
            continue
        if kind == b"VP8X" and length >= 1 and chunk[8] & _WEBP_METADATA_FLAGS:
            chunk = chunk[:8] + bytes((chunk[8] & ~_WEBP_METADATA_FLAGS & 0xFF,)) + chunk[9:]
        chunks.append(chunk)
    if not changed:
        return data
    body = b"WEBP" + b"".join(chunks)
    return b"RIFF" + struct.pack("<I", len(body)) + body
