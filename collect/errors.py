"""Errors raised by the acquisition layer.

Each one is a refusal, and each one says which rule caused it -- the same convention
the gates use, because these errors surface to an analyst in the CLI and have to be
quotable in an audit discussion.
"""
from __future__ import annotations


class CollectionError(Exception):
    """Base class."""


class PageNotFound(CollectionError):
    """The transport was asked for a URL it has no page for.

    The fixture transport raises this rather than returning an empty page, because a
    silent empty page would look like "the service had nothing there" and would be
    recorded as a genuine observation.
    """


class UnknownHost(CollectionError):
    """The fixture transport was asked for a host it does not model.

    Raised loudly so that a scope error -- a seed that drifted off the case's target
    list -- shows up as a crash rather than as a 404 collected into the record.
    """


class LiveCollectionRefused(CollectionError):
    """Live network collection was attempted without the explicit authorisation flag.

    This is the plan's `authorised_mode` gate (B2). It is off by default, and turning
    it on is a decision a person makes, not a default a module ships with.
    """


class ScopeViolation(CollectionError):
    """A run asked for a target outside the scope its case declared.

    Distinct from :class:`UnknownHost`, which the fixture transport raises when it is
    *asked*: this one is raised by the collector before the request exists, so the URL
    never leaves the process. A live transport has no fixture's host list to hide behind,
    so the collector carries the scope itself and refuses on its own authority.
    """


class TransportUnavailable(CollectionError):
    """Live transport was selected but its optional dependencies are not installed."""


class CheckpointError(CollectionError):
    """A stored checkpoint does not match the source it claims to resume."""


class SchedulingError(CollectionError):
    """A collection run violated its own politeness or budget limits."""
