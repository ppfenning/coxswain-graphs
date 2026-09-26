"""Resolve a traces URL to a pyarrow filesystem and root; `redact_url` is the only form of a URL to show.

The provider profile may carry an optional `object_store` block for an S3-compatible server:

    object_store:
      endpoint: http://garage.lan:3900      # no scheme defaults to https
      region: garage                        # optional
      access_key_env: GARAGE_ACCESS_KEY_ID  # the NAME of the env var holding the key, never the key
      secret_key_env: GARAGE_SECRET_ACCESS_KEY
      path_style: true                      # optional, default true

Credentials come only from the environment. A literal `access_key`, `secret_key` or `secret` is refused.
Without the block, the AWS_REGION, AWS_DEFAULT_REGION and AWS_ENDPOINT_URL variables apply.

Plain paths, `file://` and `s3://` are built in and cannot be overridden. Any other scheme is
served by a plugin: an entry point in the `coxswain.storage` group whose name is the URL scheme.
It loads to an object with `filesystem(url, env, block) -> (pyarrow FileSystem, path)`. `block` is
the provider profile's `object_store` mapping or None. Credentials are read from `env`, never from `block`.
"""

from __future__ import annotations

import importlib.metadata
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
STORAGE_ENTRY_POINT_GROUP = "coxswain.storage"


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


_LITERAL_KEYS = ("access_key", "secret_key", "secret")


def _env_value(env: Mapping[str, str], name: str) -> str:
    value = env.get(name)
    if not value:
        raise ValueError(f"object_store names env var {name}, which is not set")
    return value


def _block_options(env: Mapping[str, str], block: Mapping[str, Any]) -> dict[str, Any]:
    literal = next((key for key in _LITERAL_KEYS if key in block), None)
    if literal is not None:
        raise ValueError(f"object_store must not hold a literal {literal}: name an env var in {literal}_env")
    endpoint = block.get("endpoint")
    parts = urlsplit(endpoint if endpoint and _SCHEME.match(endpoint) else f"https://{endpoint}")
    # No *_env names leaves the credentials to pyarrow's default chain.
    return {
        **({"scheme": parts.scheme, "endpoint_override": parts.netloc + parts.path.rstrip("/")} if endpoint else {}),
        **({"region": block["region"]} if block.get("region") else {}),
        **({"access_key": _env_value(env, block["access_key_env"])} if block.get("access_key_env") else {}),
        **({"secret_key": _env_value(env, block["secret_key_env"])} if block.get("secret_key_env") else {}),
        "force_virtual_addressing": not block.get("path_style", True),
    }


def _s3_options(env: Mapping[str, str], object_store: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if object_store:
        return _block_options(env, object_store)
    region = env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION")
    endpoint = env.get("AWS_ENDPOINT_URL")
    return {
        **({"region": region} if region else {}),
        **({"endpoint_override": endpoint} if endpoint else {}),
    }


def _storage_plugins() -> Mapping[str, Any]:
    return {entry.name: entry for entry in importlib.metadata.entry_points(group=STORAGE_ENTRY_POINT_GROUP)}


def _plugin_for(scheme: str, plugins: Mapping[str, Any]) -> Any:
    """The loadable registered under `scheme`, else the refusal that names the scheme and the group."""
    if scheme not in plugins:
        raise ValueError(
            f"unknown traces URL scheme {scheme!r}: "
            f"no plugin registered under the {STORAGE_ENTRY_POINT_GROUP} entry-point group"
        )
    return plugins[scheme]


def resolve_traces_root(
    url: str | None,
    runs_dir: Path,
    env: Mapping[str, str],
    object_store: Mapping[str, Any] | None = None,
) -> TracesRoot:
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
        return TracesRoot(S3FileSystem(**_s3_options(env, object_store)), path)
    fs, path = _plugin_for(parts.scheme, _storage_plugins()).load().filesystem(url, env, object_store)
    return TracesRoot(fs, path)


def _local(path: str) -> TracesRoot:
    from pyarrow.fs import LocalFileSystem

    return TracesRoot(LocalFileSystem(), path)
