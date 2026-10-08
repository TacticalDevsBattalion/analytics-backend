"""Correlation IDs contain no source payload, credentials or user identifiers."""
from contextvars import ContextVar

request_id: ContextVar[str] = ContextVar('analytics_request_id', default='-')
