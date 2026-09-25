"""Resolve a traces URL to a pyarrow filesystem and root; `redact_url` is the only form of a URL to show."""

from __future__ import annotations

import importlib.util
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

if TYPE_CHECKING:
    from pyarrow.fs import FileSystem

_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


@dataclass(frozen=True)
class TracesRoot:
    fs: FileSystem
    path: str


def have_pyarrow() -> bool:
    return importlib.util.find_spec("pyarrow") is not None


def redact_url(url: str) -> str:
    """Keep the scheme and everything after the last '@', minus any query or fragment.

    The cut is at the last '@' of the whole string, not of the authority, because a
    secret may hold an unencoded '/', '?' or '#'. It can over-redact; it never leaks.
    """
    match = _SCHEME.match(url)
    scheme = match.group(0) if match else ""
    rest = url[len(scheme) :].rpartition("@")[2]
    return scheme + rest.split("?", 1)[0].split("#", 1)[0]


def _s3_options(env: Mapping[str, str]) -> dict[str, Any]:
    region = env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION")
    endpoint = env.get("AWS_ENDPOINT_URL")
    return {
        **({"region": region} if region else {}),
        **({"endpoint_override": endpoint} if endpoint else {}),
    }


def resolve_traces_root(url: str | None, runs_dir: Path, env: Mapping[str, str]) -> TracesRoot:
    if not url:
        return _local(str(runs_dir / "traces"))
    if not _SCHEME.match(url):
        return _local(url)
    if "@" in url:
        raise ValueError(f"traces url must not carry credentials: {redact_url(url)}")
    parts = urlsplit(url)
    if parts.query or parts.fragment:
        raise ValueError(f"traces url must not carry a query or fragment: {redact_url(url)}")
    if parts.scheme == "file":
        if parts.netloc not in ("", "localhost"):
            raise ValueError(f"file url must name a local absolute path: {redact_url(url)}")
        return _local(unquote(parts.path))
    if parts.scheme == "s3":
        if not parts.netloc:
            raise ValueError(f"traces url has no bucket: {redact_url(url)}")
        from pyarrow.fs import S3FileSystem

        prefix = parts.path.strip("/")
        path = f"{parts.netloc}/{prefix}" if prefix else parts.netloc
        return TracesRoot(S3FileSystem(**_s3_options(env)), path)
    raise ValueError(f"unsupported traces url scheme {parts.scheme!r}: {redact_url(url)}")


def _local(path: str) -> TracesRoot:
    from pyarrow.fs import LocalFileSystem

    return TracesRoot(LocalFileSystem(), path)
