"""Typed errors. Each maps to a distinct operator action, not just a message."""
from __future__ import annotations


class AnalystError(Exception):
    """Base class for every error this system raises deliberately."""


class ConfigError(AnalystError):
    """Malformed or missing configuration — the operator must fix a YAML file."""


class DataUnavailableError(AnalystError):
    """A provider returned nothing usable. Retryable; not a bug."""


class InsufficientDataError(AnalystError):
    """Data arrived but is too short/gappy to analyse honestly."""


class ProviderError(AnalystError):
    """The upstream provider failed (network, rate limit, schema change)."""
