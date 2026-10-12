"""No operator, no homelab, no employer, no vendor assumption, anywhere in this repository.

The claim of a shipped repo is that it outlives the machines and the workplace it was first used
at. That claim is worth exactly as much as the check that enforces it, so it is enforced here
rather than remembered.

Rules that need no names, so they are committed:
  home-path  an absolute home directory other than the neutral stand-in /home/user
  lan-host   a host under the .lan pseudo-domain
  address    an address in the CGNAT (tailnet) range or the 192.168 private range
  vendor     a tracker or platform a tool must never assume
The names rule needs the names themselves, so they are never committed: it reads a regex from
the COXSWAIN_CLEAN_DENY environment variable, and without it says so and skips.

A line carrying the marker `clean: allow` is exempt from every rule; keep it to the lines that
define the rules and the fixtures that test them.

Usage: python .github/check_clean.py [root]. Stdlib only, so any repo can copy this file as is.
Exits 1 on any hit, 2 on an unusable COXSWAIN_CLEAN_DENY.
"""
import ipaddress
import os
import pathlib
import re
import sys

ALLOW = "clean: allow"
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
NEUTRAL_HOMES = {("home", "user")}
PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(net) for net in ("100.64.0.0/10", "192.168.0.0/16")  # clean: allow
)

HOME_PATH = re.compile(r"/(home|Users)/([A-Za-z0-9._-]+)")
LAN_HOST = re.compile(r"\b[a-z0-9-]+\.lan\b", re.I)  # clean: allow
ADDRESS = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d{1,3}){3})(?!\w|\.\d)")
VENDOR = re.compile(r"\b(asana|jira|confluence|cloudwatch|outline|snowflake|databricks)\b", re.I)  # clean: allow


def home_path(line):
    return next(
        (m.group(0) for m in HOME_PATH.finditer(line) if (m.group(1), m.group(2)) not in NEUTRAL_HOMES),
        None,
    )


def lan_host(line):
    hit = LAN_HOST.search(line)
    return hit.group(0) if hit else None


def address(line):
    def private(text):
        try:
            return any(ipaddress.ip_address(text) in net for net in PRIVATE_NETWORKS)
        except ValueError:
            return False

    return next((m.group(1) for m in ADDRESS.finditer(line) if private(m.group(1))), None)


def vendor(line):
    hit = VENDOR.search(line)
    return hit.group(0) if hit else None


def names_rule(pattern):
    deny = re.compile(pattern)

    def names(line):
        hit = deny.search(line)
        return hit.group(0) if hit else None

    return names


def rules(environ):
    """The rules to run, and the one line to print when the names rule is skipped."""
    fixed = {"home-path": home_path, "lan-host": lan_host, "address": address, "vendor": vendor}
    pattern = environ.get("COXSWAIN_CLEAN_DENY", "")
    if not pattern:
        return fixed, "names rule skipped: COXSWAIN_CLEAN_DENY is not set"
    return {**fixed, "names": names_rule(pattern)}, None


def text_files(root):
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if set(relative.parts) & SKIP_DIRS or any(p.endswith(".egg-info") for p in relative.parts):
            continue
        if not path.is_file():
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue
        yield relative, data.decode("utf-8", errors="replace")


def hits(root, active):
    for relative, text in text_files(root):
        for number, line in enumerate(text.splitlines(), 1):
            if ALLOW in line:
                continue
            for name, rule in active.items():
                found = rule(line)
                if found:
                    yield f"{relative}:{number}: {name}: '{found}'"


def main(argv, environ):
    root = pathlib.Path(argv[1] if len(argv) > 1 else ".")
    try:
        active, skipped = rules(environ)
    except re.error as error:
        print(f"COXSWAIN_CLEAN_DENY is not a usable regex: {error}")
        return 2
    if skipped:
        print(skipped)
    problems = list(hits(root, active))
    print("\n".join(problems) if problems else "clean: no personal reference or vendor named")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv, os.environ))
