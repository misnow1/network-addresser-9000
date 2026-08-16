"""Shared field validators for inventory models."""

import ipaddress
import re

from django.core.exceptions import ValidationError


def validate_ipv4_cidr(value: str) -> None:
    """Validate that value is an IPv4 network in CIDR notation, e.g. '10.200.0.0/21'."""
    try:
        ipaddress.IPv4Network(value, strict=True)
    except ValueError as exc:
        raise ValidationError(
            "%(value)s is not a valid IPv4 CIDR (e.g. 10.200.0.0/21).",
            params={"value": value},
        ) from exc


_DNS_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


def validate_dns_label(value: str) -> None:
    """Validate that ``value`` is a single, bare DNS label — lowercase
    alphanumerics and internal hyphens only, no leading/trailing hyphen,
    max 63 characters (RFC 1035). Ships here in ADR 0022 so phase 18's
    hostname assembly can reuse it rather than writing a second one; the
    63-character cap is ADR 0023 decision 8.

    Deliberately does not lower/strip ``value`` itself — callers
    (``NetworkDeviceTypePort.clean()``/``save()``) normalize before
    validating and storing, so this only ever sees, and only ever
    accepts, an already-normalized value.
    """
    if len(value) > 63:
        raise ValidationError(
            "%(value)s is too long for a DNS label (max 63 characters).",
            params={"value": value},
        )
    if not _DNS_LABEL_RE.match(value):
        raise ValidationError(
            "%(value)s is not a valid DNS label — lowercase letters, digits and internal "
            "hyphens only, no leading or trailing hyphen.",
            params={"value": value},
        )
