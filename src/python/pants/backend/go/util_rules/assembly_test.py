# Copyright 2026 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

from pants.backend.go.util_rules.assembly import _asm_args
from pants.backend.go.util_rules.goroot import GoRoot
from pants.util.frozendict import FrozenDict


def _goroot(version: str) -> GoRoot:
    return GoRoot(
        path="/goroot",
        version=version,
        _raw_metadata=FrozenDict({"GOVERSION": f"go{version}", "GOOS": "linux", "GOARCH": "amd64"}),
    )


def test_stdlib_package_gets_std_flag_on_go_1_27() -> None:
    args = _asm_args("sync/atomic", "__go/sync/atomic", _goroot("1.27"), (), is_stdlib=True)
    assert "-std" in args


def test_non_stdlib_package_never_gets_std_flag() -> None:
    args = _asm_args("example.com/foo", "foo", _goroot("1.27"), (), is_stdlib=False)
    assert "-std" not in args


def test_stdlib_package_does_not_get_std_flag_before_go_1_27() -> None:
    args = _asm_args("sync/atomic", "__go/sync/atomic", _goroot("1.26"), (), is_stdlib=True)
    assert "-std" not in args
