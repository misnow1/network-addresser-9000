"""Shared field validators for inventory models."""

import ipaddress

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
