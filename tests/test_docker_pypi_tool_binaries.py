"""Regression check: PyPI-provided tool binaries must reach the runtime images.

`ast-grep-cli` is a declared dependency in `pyproject.toml`, but it ships real
binaries rather than importable Python -- only its `ast_grep_cli-*.dist-info`
lands in site-packages. The builder's `uv pip install --system` therefore puts
`/usr/local/bin/ast-grep` in its bin dir, and the runtime stages of the
Dockerfile have to copy it out explicitly: they whitelist individual
`COPY --from=builder` entries instead of copying `/usr/local/bin` wholesale, so
a dependency's binary is invisible to them.

When that entry is missing the image still looks healthy. site-packages carries
the dist-info, so `pip list` reports the tool installed, while nothing is on
PATH. `headroom tools doctor` then reports `ast-grep` as `unsupported-platform`
with a hint blaming `pip install`, and `headroom proxy --intercept-tool-results`
refuses to start (#3649) -- which becomes a crash loop under
`restart: unless-stopped`. Same whitelist-COPY class as #976, for a PyPI
dependency rather than the Rust proxy binary.

The assertion is written against the registry instead of naming `ast-grep`, so
the next PyPI-distributed tool binary fails here rather than in a deployed
image.
"""

from __future__ import annotations

import re
from pathlib import Path

from headroom import binaries

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")

_FROM_RE = re.compile(r"^FROM\s+\S+\s+AS\s+(\S+)$", re.IGNORECASE)
_COPY_FROM_RE = re.compile(r"^COPY\s+--from=(\S+)\s+(.*)$", re.IGNORECASE)


def _builder_sources_by_stage() -> dict[str, set[str]]:
    """Map each stage that copies from `builder` to the paths it copies in.

    Stages are delimited by `FROM ... AS <name>`. The `builder` stage copies
    nothing from itself and so does not appear; every stage that does is an
    image stage, whose contents have to stand on their own.
    """
    stages: dict[str, set[str]] = {}
    current: str | None = None
    for raw in DOCKERFILE.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        stage = _FROM_RE.match(line)
        if stage is not None:
            current = stage.group(1)
            stages.setdefault(current, set())
            continue
        copied = _COPY_FROM_RE.match(line)
        if copied is not None and copied.group(1) == "builder" and current is not None:
            stages[current].update(copied.group(2).split())
    return {name: sources for name, sources in stages.items() if sources}


def _pypi_tool_binaries() -> dict[str, str]:
    """Map every PyPI-distributed tool to the binary name its wheel installs.

    These are the tools the image has to carry itself. The remaining registry
    entries are fetched at runtime into a cache dir by `headroom.binaries`, so
    they need no `/usr/local/bin` entry.
    """
    return {
        name: entry["binary"]
        for name, entry in binaries._registry()["tools"].items()
        if binaries._is_pypi_tool(name)
    }


def test_pypi_tool_binaries_are_copied_into_every_runtime_stage() -> None:
    """A PyPI tool binary must be copied out of the builder, not just installed.

    Fails on `main` ahead of the #3649 fix: the `ast-grep-cli` dist-info crosses
    into the runtime images with site-packages, but no `COPY` carries the
    binary, so `ast-grep` is absent from PATH.
    """
    expected = {f"/usr/local/bin/{binary}" for binary in _pypi_tool_binaries().values()}
    assert expected, (
        "expected at least one PyPI-distributed tool in headroom/tools.json; "
        "without one this guard would pass vacuously"
    )

    stages = _builder_sources_by_stage()
    assert stages, "expected the Dockerfile runtime stages to copy from the builder"

    missing = {
        name: sorted(expected - sources)
        for name, sources in sorted(stages.items())
        if expected - sources
    }
    assert not missing, (
        f"Dockerfile stages copying from builder without {sorted(expected)}: {missing}. "
        "site-packages still carries the dist-info, so `pip list` reports the tool "
        "installed while nothing is on PATH, and `headroom proxy "
        "--intercept-tool-results` refuses to start (#3649)."
    )


def test_sg_alias_is_not_copied_over_the_shadow_utils_binary() -> None:
    """The `ast-grep` wheel also installs `sg`, and the image has `/usr/bin/sg`.

    `/usr/local/bin` precedes `/usr/bin` on the default PATH, so copying the
    alias would shadow shadow-utils' `sg` (set-group) inside the image. Headroom
    resolves the tool by the name `ast-grep`, so the alias buys nothing.
    """
    sources: set[str] = set()
    for paths in _builder_sources_by_stage().values():
        sources |= paths
    assert "/usr/local/bin/sg" not in sources
