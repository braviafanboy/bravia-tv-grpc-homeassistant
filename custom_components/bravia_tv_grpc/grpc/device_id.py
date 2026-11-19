"""The device unique id as a Sony device advertises it, and how to compare two.

Sony devices announce themselves over mDNS as
``<device name>-<device_unique_id>._sonysmarthome._tcp.local.``, where the id
is 40 hex characters. The part before the service type is one DNS label, which
holds 63 bytes at most (the soundbars seen stop at 60 characters), so a long
device name leaves no room for the whole id: the name is cut short and the id
loses its end. The announcement carries the id nowhere else, so what is left
of it is all there is to go on.

That is still enough to tell devices apart. An id that was cut short is
compared with a whole one (the Sony account's, or one stored earlier) by its
beginning.
"""

from __future__ import annotations

import re

# The fewest id characters taken as an identity. Eight hex characters already
# tell apart far more devices than share a home network or a Sony account.
MIN_PARTIAL_LENGTH = 8

_WHOLE = re.compile(r"-([0-9a-f]{40})", re.IGNORECASE)
# What is left of an id at the very end of a name that was cut short.
_CUT_SHORT = re.compile(rf"-([0-9a-f]{{{MIN_PARTIAL_LENGTH},39}})$", re.IGNORECASE)


def parse_instance_name(name: str | None) -> tuple[str | None, str | None]:
    """``(device name, device unique id)`` from a ``_sonysmarthome._tcp``
    instance name, with or without the service type after it.

    The id is in lower case. It is None when the name carries none, and only
    the beginning of the id when the name was cut short; compare ids with
    :func:`same_device`, never with ``==``.
    """
    label = (name or "").split("._sonysmarthome")[0]
    match = _WHOLE.search(label) or _CUT_SHORT.search(label)
    if match is None:
        return (label or None), None
    return label[: match.start()].strip(" -") or None, match.group(1).lower()


def same_device(one: object, other: object) -> bool:
    """Whether two device unique ids name the same device.

    Either may be only the beginning of the id. Two of the same length must be
    equal; otherwise the shorter must be the beginning of the longer, and long
    enough to mean something.
    """
    if not isinstance(one, str) or not isinstance(other, str):
        return False
    one, other = one.lower(), other.lower()
    if not one or not other:
        return False
    if len(one) == len(other):
        return one == other
    short, long = sorted((one, other), key=len)
    return len(short) >= MIN_PARTIAL_LENGTH and long.startswith(short)
