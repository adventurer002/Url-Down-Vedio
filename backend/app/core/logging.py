"""Redact URL credentials before API, Celery, HTTP-client or access-log formatting."""
import logging
import re
import traceback
from urllib.parse import unquote

REDACTED = "***REDACTED***"
SENSITIVE_QUERY_KEYS = frozenset({"token", "access_token", "key", "api_key", "auth", "authorization",
                                  "sig", "signature", "cookie", "sessdata", "password", "secret"})
_QUERY = re.compile(r"([?&;])([^?&;=#\s\"'<>]+)=([^&#\s\"'<>]*)")
# Encoded URLs can occur inside an outer URL's query. Match one to three encoding layers.
_ENCODED = [re.compile(rf"(%{('25' * level)}(?:3f|26|3b))(.+?)(%{('25' * level)}3d)(.*?)(?=%{('25' * level)}(?:26|23|3b)|[\s\"'<>]|$)", re.I)
            for level in range(3)]


def sensitive(key) -> bool:
    if not isinstance(key, str):
        return False
    for _ in range(3):
        key = unquote(key)
    return key.casefold() in SENSITIVE_QUERY_KEYS


def redact_urls(value: str) -> str:
    value = _QUERY.sub(lambda m: m[1] + m[2] + "=" + (REDACTED if sensitive(m[2]) else m[3]), value)
    for pattern in _ENCODED:
        cursor = 0
        while match := pattern.search(value, cursor):
            if sensitive(match[2]):
                value = value[:match.start(4)] + REDACTED + value[match.end(4):]
                cursor = match.start(4) + len(REDACTED)
            else:
                # Continue inside non-sensitive values: they may contain another encoded URL.
                cursor = match.end(1)
    # Server configuration URLs may contain basic auth; never log that userinfo.
    return re.sub(r"(\b[a-z][a-z0-9+.-]*://)[^/\s@?#]+@", lambda m: m[1] + REDACTED + "@", value, flags=re.I)


def redact_value(value):
    if isinstance(value, str):
        return redact_urls(value)
    if isinstance(value, bytes):
        return redact_urls(value.decode("utf-8", errors="replace")).encode("utf-8")
    if isinstance(value, dict):
        return {key: REDACTED if sensitive(key) else redact_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    return value


class SensitiveURLFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.args = redact_value(record.args)
        # Do not redact placeholders in the format string before interpolation.
        if not record.args:
            record.msg = redact_value(record.msg)
        # Handle URLs assembled with positional placeholders; preserve Uvicorn's access args,
        # which AccessFormatter needs to construct request_line/status_code.
        rendered = record.getMessage()
        safe = redact_urls(rendered)
        if rendered != safe and record.name != "uvicorn.access":
            record.msg, record.args = safe, ()
        for key, value in list(record.__dict__.items()):
            if key not in {"msg", "args", "exc_info"}:
                record.__dict__[key] = REDACTED if sensitive(key) else redact_value(value)
        if record.exc_info:
            record.exc_text = redact_urls("".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        return True


_filter = SensitiveURLFilter()


def install_log_redaction():
    """Chain the existing factory; handler filters additionally cover late `extra` fields."""
    previous = logging.getLogRecordFactory()
    if not getattr(previous, "_videohub_redaction", False):
        def factory(*args, **kwargs):
            record = previous(*args, **kwargs)
            _filter.filter(record)
            return record
        factory._videohub_redaction = True
        logging.setLogRecordFactory(factory)
    for logger in [logging.getLogger(), *(item for item in logging.Logger.manager.loggerDict.values() if isinstance(item, logging.Logger))]:
        for handler in logger.handlers:
            if not any(isinstance(item, SensitiveURLFilter) for item in handler.filters):
                handler.addFilter(_filter)
