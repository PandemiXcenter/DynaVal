"""Actionable domain and boundary errors, safe to show in the application."""


class DynaValError(Exception):
    """Base class for expected application errors."""


class ImportFailure(DynaValError):
    """The dataset cannot be parsed without losing information."""


class SessionError(DynaValError):
    """A saved session or requested state transition is invalid."""


class MediaError(DynaValError):
    """A source image could not be loaded."""


class ExportError(DynaValError):
    """An export could not be safely published."""
