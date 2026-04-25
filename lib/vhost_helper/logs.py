"""
Log path extraction utilities for the ``vhost logs`` command.

All functions in this module are pure functions — they accept a configuration
file's text content as a string and return extracted paths.  No filesystem or
subprocess access is performed here.
"""

import re
from typing import Optional, Tuple


def extract_nginx_log_paths(
    config_content: str,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Extract ``access_log`` and ``error_log`` paths from an Nginx configuration.

    Rules:
    - Only the first occurrence of each directive is used.
    - Inline comments (``# ...``) are stripped before parsing.
    - A directive set to ``off`` (e.g. ``access_log off;``) returns ``None``
      for that stream.

    Returns:
        A ``(access_log_path, error_log_path)`` tuple.  Either element may be
        ``None`` when the directive is absent or explicitly disabled.
    """
    access_log: Optional[str] = None
    error_log: Optional[str] = None

    for raw_line in config_content.splitlines():
        line = re.sub(r"\s*#.*$", "", raw_line).strip()
        if not line:
            continue

        if access_log is None:
            m = re.match(r"access_log\s+(\S+)", line, re.IGNORECASE)
            if m:
                path = m.group(1).rstrip(";")
                access_log = None if path.lower() == "off" else path

        if error_log is None:
            m = re.match(r"error_log\s+(\S+)", line, re.IGNORECASE)
            if m:
                path = m.group(1).rstrip(";")
                error_log = None if path.lower() == "off" else path

        if access_log is not None and error_log is not None:
            break

    return access_log, error_log


def extract_apache_log_paths(
    config_content: str,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Extract ``CustomLog`` and ``ErrorLog`` paths from an Apache configuration.

    Rules:
    - Directive matching is case-insensitive (``CustomLog``, ``customlog``,
      and ``CUSTOMLOG`` are all accepted).
    - For ``CustomLog``, only the first whitespace-delimited token after the
      directive keyword is used as the path; the optional log-format token is
      ignored.
    - Only the first occurrence of each directive is used.
    - Inline comments (``# ...``) are stripped before parsing.

    Returns:
        A ``(access_log_path, error_log_path)`` tuple.  Either element may be
        ``None`` when the directive is absent.
    """
    access_log: Optional[str] = None
    error_log: Optional[str] = None

    for raw_line in config_content.splitlines():
        line = re.sub(r"\s*#.*$", "", raw_line).strip()
        if not line:
            continue

        if access_log is None:
            m = re.match(r"CustomLog\s+(\S+)", line, re.IGNORECASE)
            if m:
                access_log = m.group(1)

        if error_log is None:
            m = re.match(r"ErrorLog\s+(\S+)", line, re.IGNORECASE)
            if m:
                error_log = m.group(1)

        if access_log is not None and error_log is not None:
            break

    return access_log, error_log
