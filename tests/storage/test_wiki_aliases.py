# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
import pytest

from synthadoc.storage import wiki_aliases as wa


def test_empty_when_missing(tmp_path):
    aliases = wa.load(tmp_path / "nope.yaml")
    assert aliases.is_empty
    assert aliases.resolve("liz") is None
    assert aliases.canonical("liz") == "liz"  # unchanged passthrough


def test_parse_and_resolve():
    aliases = wa.parse(
        "aliases:\n"
        "  lyzr:\n"
        "    - liz\n"
        "    - Lizer\n"          # display-name casing is slugified
        "  gc-tst:\n"
        "    - gc-tst-d\n"
    )
    assert aliases.resolve("liz") == "lyzr"
    assert aliases.resolve("lizer") == "lyzr"
    assert aliases.resolve("gc-tst-d") == "gc-tst"
    assert aliases.resolve("unrelated") is None
    assert aliases.canonical("liz") == "lyzr"
    assert aliases.canonical("egp") == "egp"


def test_parse_flattens_chains():
    # liz -> lizer -> lyzr should resolve straight to lyzr.
    aliases = wa.parse(
        "aliases:\n"
        "  lyzr:\n"
        "    - lizer\n"
        "  lizer:\n"
        "    - liz\n"
    )
    assert aliases.resolve("liz") == "lyzr"
    assert aliases.resolve("lizer") == "lyzr"


def test_invalid_shapes():
    with pytest.raises(wa.WikiAliasError):
        wa.parse("aliases: not-a-map")
    with pytest.raises(wa.WikiAliasError):
        wa.parse("aliases:\n  lyzr: not-a-list\n")


def test_add_alias_creates_and_is_idempotent(tmp_path):
    path = tmp_path / "wiki_aliases.yaml"
    wa.add_alias(path, canonical_slug="lyzr", alias_slug="liz")
    wa.add_alias(path, canonical_slug="lyzr", alias_slug="liz")  # dup → no-op
    wa.add_alias(path, canonical_slug="lyzr", alias_slug="lizer")
    aliases = wa.load(path)
    assert aliases.resolve("liz") == "lyzr"
    assert aliases.resolve("lizer") == "lyzr"
    # only one 'liz' entry recorded
    assert path.read_text(encoding="utf-8").count("- liz\n") == 1


def test_add_alias_rejects_self():
    import tempfile, os
    with tempfile.TemporaryDirectory() as d:
        with pytest.raises(wa.WikiAliasError):
            wa.add_alias(os.path.join(d, "a.yaml"),
                         canonical_slug="lyzr", alias_slug="lyzr")
