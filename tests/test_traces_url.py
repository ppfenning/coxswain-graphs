from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness.traces_url import (
    TracesRoot,
    _plugin_for,
    _s3_options,
    have_pyarrow,
    redact_url,
    resolve_traces_root,
)


def _fs():
    return pytest.importorskip("pyarrow.fs")


def test_have_pyarrow_is_a_bool():
    assert isinstance(have_pyarrow(), bool)


def test_redact_url_drops_a_query_string_and_fragment():
    assert redact_url("s3://b/p?token=abc#f") == "s3://b/p"


def test_redact_url_drops_userinfo():
    assert redact_url("s3://u:secret@b/p") == "s3://b/p"


def test_redact_url_drops_a_query_and_userinfo_without_a_scheme():
    assert redact_url("/var/traces?token=abc#f") == "/var/traces"
    assert redact_url("user:secret@host/path") == "host/path"


def test_redact_url_does_not_take_a_scheme_from_inside_the_query():
    assert redact_url("/var/traces?cb=http://x") == "/var/traces"


@pytest.mark.parametrize("secret", ["ab/cd", "pa?ss", "pa#ss", "a/b?c#d"])
def test_a_secret_holding_a_delimiter_is_refused_and_never_shown(secret):
    url = f"s3://AKIA:{secret}@bucket/p"
    assert secret not in redact_url(url)
    assert redact_url(url) == "s3://bucket/p"
    with pytest.raises(ValueError) as err:
        resolve_traces_root(url, Path("/r"), {})
    assert secret not in str(err.value)
    assert "AKIA" not in str(err.value)


def test_userinfo_is_refused_without_the_secret_in_the_error():
    with pytest.raises(ValueError) as err:
        resolve_traces_root("s3://user:hunter2@bucket/p?x=1", Path("/r"), {})
    assert "hunter2" not in str(err.value)
    assert "s3://bucket/p" in str(err.value)


def test_an_unknown_scheme_is_refused():
    with pytest.raises(ValueError, match="ftp"):
        resolve_traces_root("ftp://host/x", Path("/r"), {})


def _registered(monkeypatch, **filesystems):
    """Patch the entry-point read so each scheme loads a plugin that records its calls and returns its answer."""
    calls = []

    def entry(scheme, answer):
        def filesystem(url, env, block):
            calls.append((scheme, url, env, block))
            return answer

        return SimpleNamespace(name=scheme, load=lambda: SimpleNamespace(filesystem=filesystem))

    entries = {scheme: entry(scheme, answer) for scheme, answer in filesystems.items()}
    monkeypatch.setattr("harness.traces_url._storage_plugins", lambda: entries)
    return calls


def test_a_plugin_under_the_scheme_gets_url_env_and_block_and_supplies_the_root(monkeypatch):
    fake_fs = object()
    calls = _registered(monkeypatch, memfs=(fake_fs, "bucket/p"))
    env, block = {"K": "v"}, {"endpoint": "e"}
    root = resolve_traces_root("memfs://bucket/p", Path("/r"), env, block)
    assert calls == [("memfs", "memfs://bucket/p", env, block)]
    assert root == TracesRoot(fake_fs, "bucket/p")


def test_a_scheme_with_no_plugin_is_refused_naming_the_scheme_and_the_group(monkeypatch):
    _registered(monkeypatch)
    with pytest.raises(ValueError) as err:
        resolve_traces_root("fake://b/p", Path("/r"), {})
    assert str(err.value) == (
        "unknown traces URL scheme 'fake': no plugin registered under the coxswain.storage entry-point group"
    )


def test_the_lookup_returns_the_registered_loadable_and_refuses_the_rest():
    loadable = object()
    assert _plugin_for("fake", {"fake": loadable}) is loadable
    with pytest.raises(ValueError, match="'other'"):
        _plugin_for("other", {"fake": loadable})


def test_a_plugin_under_s3_is_not_called_and_the_built_in_branch_is_taken(monkeypatch):
    fs = _fs()
    calls = _registered(monkeypatch, s3=(object(), "plugin/path"))
    built = object()
    monkeypatch.setattr(fs, "S3FileSystem", lambda **kwargs: built)
    root = resolve_traces_root("s3://bucket/p", Path("/runs"), {})
    assert calls == []
    assert root == TracesRoot(built, "bucket/p")


def test_a_plugin_under_file_is_not_called_and_the_built_in_branch_is_taken(monkeypatch):
    fs = _fs()
    calls = _registered(monkeypatch, file=(object(), "plugin/path"))
    root = resolve_traces_root("file:///var/traces", Path("/runs"), {})
    assert calls == []
    assert isinstance(root.fs, fs.LocalFileSystem)
    assert root.path == "/var/traces"


def test_an_s3_url_with_no_bucket_is_refused():
    with pytest.raises(ValueError):
        resolve_traces_root("s3:///x", Path("/r"), {})


def test_a_query_string_is_refused_without_its_value_in_the_error():
    with pytest.raises(ValueError) as err:
        resolve_traces_root("s3://bucket/p?token=abc", Path("/r"), {})
    assert "abc" not in str(err.value)


def test_a_file_url_with_a_host_is_refused():
    with pytest.raises(ValueError):
        resolve_traces_root("file://relative/dir", Path("/r"), {})


@pytest.mark.parametrize("empty", [None, ""])
def test_default_is_a_local_root_under_runs_dir(empty):
    fs = _fs()
    root = resolve_traces_root(empty, Path("/runs"), {})
    assert isinstance(root.fs, fs.LocalFileSystem)
    assert root.path == "/runs/traces"


def test_a_bare_path_is_local():
    fs = _fs()
    root = resolve_traces_root("/var/traces", Path("/runs"), {})
    assert isinstance(root.fs, fs.LocalFileSystem)
    assert root.path == "/var/traces"


def test_a_file_url_is_local():
    fs = _fs()
    root = resolve_traces_root("file:///var/my%20traces", Path("/runs"), {})
    assert isinstance(root.fs, fs.LocalFileSystem)
    assert root.path == "/var/my traces"


def test_an_s3_url_is_an_s3_filesystem_rooted_at_bucket_and_prefix():
    fs = _fs()
    root = resolve_traces_root("s3://bucket/prefix/", Path("/runs"), {"AWS_REGION": "us-east-1"})
    assert isinstance(root.fs, fs.S3FileSystem)
    assert root.path == "bucket/prefix"


def test_an_s3_url_with_no_prefix_is_the_bucket_alone():
    _fs()
    root = resolve_traces_root("s3://bucket", Path("/runs"), {"AWS_DEFAULT_REGION": "eu-west-1"})
    assert root.path == "bucket"


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, {}),
        ({"AWS_REGION": "us-east-1", "AWS_DEFAULT_REGION": "eu-west-1"}, {"region": "us-east-1"}),
        (
            {"AWS_DEFAULT_REGION": "eu-west-1", "AWS_ENDPOINT_URL": "http://minio:9000"},
            {"region": "eu-west-1", "endpoint_override": "http://minio:9000"},
        ),
    ],
)
def test_region_and_endpoint_reach_the_s3_filesystem_from_env_only(monkeypatch, env, expected):
    fs = _fs()
    seen = []
    monkeypatch.setattr(fs, "S3FileSystem", lambda **kwargs: seen.append(kwargs))
    resolve_traces_root("s3://bucket/p", Path("/runs"), env)
    assert seen == [expected]


_BLOCK = {
    "endpoint": "http://garage.lan:3900",
    "region": "garage",
    "access_key_env": "G_KEY",
    "secret_key_env": "G_SECRET",
    "path_style": True,
}


def test_the_block_gives_the_exact_kwargs():
    assert _s3_options({"G_KEY": "k1", "G_SECRET": "s1"}, _BLOCK) == {
        "scheme": "http",
        "endpoint_override": "garage.lan:3900",
        "region": "garage",
        "access_key": "k1",
        "secret_key": "s1",
        "force_virtual_addressing": False,
    }


def test_a_missing_env_var_raises_naming_it():
    with pytest.raises(ValueError, match="G_SECRET"):
        _s3_options({"G_KEY": "k1"}, _BLOCK)


def test_a_literal_secret_key_is_refused_without_its_value():
    with pytest.raises(ValueError, match="secret_key") as err:
        _s3_options({}, {**_BLOCK, "secret_key": "hunter2"})
    assert "hunter2" not in str(err.value)


def test_no_block_keeps_the_aws_env_behaviour():
    env = {"AWS_REGION": "us-east-1", "AWS_ENDPOINT_URL": "http://minio:9000"}
    assert _s3_options(env) == {"region": "us-east-1", "endpoint_override": "http://minio:9000"}


def test_the_root_is_frozen():
    _fs()
    root = resolve_traces_root(None, Path("/runs"), {})
    assert isinstance(root, TracesRoot)
    with pytest.raises(FrozenInstanceError):
        root.path = "x"  # type: ignore[misc]
