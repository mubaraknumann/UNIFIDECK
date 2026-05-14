"""
Ubisoft binary catalog parser — decode UPC's compiled game database.

OP-55f | py_modules/unifideck/stores/ubisoft/parser_binary.py

In addition to the plaintext catalog handled by ``parser.py``, UPC keeps
a *compiled* representation of the catalog used internally for fast
lookups. This module exposes module-level functions to decode that
binary format: a header section followed by length-prefixed string
records and a checksum trailer.

The decoded records expose the same shape as the plaintext parser's
output so the two can be merged by the library facade.
"""

from __future__ import annotations
import math


def _convert_data(data: int) -> int:
    """Strip continuation bits from a multi-byte varint-style value.

    UPC's binary catalog encodes integers as little-endian byte
    sequences where each byte carries a continuation marker in
    its top bit. This helper subtracts the marker contribution.

    Args:
        data: Accumulated raw value before normalization.

    Returns:
        Decoded integer.
    """
    if data > 256 * 256:
        data -= 128 * 256 * math.ceil(data / (256 * 256))
        data -= 128 * math.ceil(data / 256)
    elif data > 256:
        data -= 128 * math.ceil(data / 256)
    return data


def parse_record_size(
    header: bytes,
    offset: int,
    second_eight: bool,
) -> tuple[int, int, int]:
    """Walk the size-prefix bytes of a binary record header.

    Two scanning modes are supported via ``second_eight``: the
    first reads until the first non-``0x08`` byte; the second
    tolerates a doubled ``0x08 0x08`` separator.

    Args:
        header: Raw record header bytes.
        offset: Cursor into ``header``.
        second_eight: Pick the secondary scanning mode.

    Returns:
        Tuple ``(record_size, new_offset, consumed_bytes)``.
    """
    multiplier = 1
    record_size = 0
    tmp_size = 0
    if second_eight:
        while header[offset] != 0x08 or (
            header[offset] == 0x08 and header[offset + 1] == 0x08
        ):
            record_size += header[offset] * multiplier
            multiplier *= 256
            offset += 1
            tmp_size += 1
    else:
        while header[offset] != 0x08 or record_size == 0:
            record_size += header[offset] * multiplier
            multiplier *= 256
            offset += 1
            tmp_size += 1
    record_size = _convert_data(record_size)
    offset += 1
    return record_size, offset, tmp_size


def parse_install_id(header: bytes, offset: int) -> tuple[int, int]:
    """Decode the install-ID varint preceding a ``0x10`` separator.

    Args:
        header: Raw record bytes.
        offset: Cursor into ``header``.

    Returns:
        Tuple ``(install_id, new_offset)``.
    """
    multiplier = 1
    install_id = 0
    while header[offset] != 0x10 or header[offset + 1] == 0x10:
        install_id += header[offset] * multiplier
        multiplier *= 256
        offset += 1
    install_id = _convert_data(install_id)
    offset += 1
    return install_id, offset


def parse_launch_id(header: bytes, offset: int) -> tuple[int, int]:
    """Decode the launch-ID varint preceding a ``0x1A`` separator.

    Args:
        header: Raw record bytes.
        offset: Cursor into ``header``.

    Returns:
        Tuple ``(launch_id, new_offset)``.
    """
    multiplier = 1
    launch_id = 0
    while header[offset] != 0x1A or (
        header[offset] == 0x1A and header[offset + 1] == 0x1A
    ):
        launch_id += header[offset] * multiplier
        multiplier *= 256
        offset += 1
    launch_id = _convert_data(launch_id)
    return launch_id, offset


def parse_ownership_record(chunk: bytes) -> tuple | None:
    """Decode one ownership record chunk from the binary catalog.

    Reads, in order: the size prefix, the first launch ID, and
    the second launch ID. Any decoding failure returns ``None``
    so the caller can skip a malformed record without aborting
    the whole parse.

    Args:
        chunk: Raw record bytes (must start at the size prefix).

    Returns:
        Tuple ``(rec_size, tmp_size, lid1, lid2)``, or ``None``
        on any decoding error.
    """
    try:
        pos = 1
        multiplier = 1
        rec_size = 0
        tmp_size = 0
        while chunk[pos] != 0x08 or rec_size == 0:
            rec_size += chunk[pos] * multiplier
            multiplier *= 256
            pos += 1
            tmp_size += 1
        rec_size = _convert_data(rec_size)
        pos += 1
        multiplier = 1
        lid1 = 0
        while chunk[pos] != 0x10 or chunk[pos + 1] == 0x10:
            lid1 += chunk[pos] * multiplier
            multiplier *= 256
            pos += 1
        lid1 = _convert_data(lid1)
        pos += 1
        multiplier = 1
        lid2 = 0
        while chunk[pos] != 0x22:
            lid2 += chunk[pos] * multiplier
            multiplier *= 256
            pos += 1
        lid2 = _convert_data(lid2)
        return rec_size, tmp_size, lid1, lid2
    except Exception:
        return None
