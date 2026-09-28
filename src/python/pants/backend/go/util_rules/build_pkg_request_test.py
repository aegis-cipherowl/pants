# Copyright 2026 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import pytest

from pants.backend.go.util_rules.build_opts import GoBuildOptions
from pants.backend.go.util_rules.build_pkg import BuildGoPackageRequest
from pants.engine.fs import EMPTY_DIGEST


def request(
    import_path: str = "example.org/root",
    dependencies: tuple[BuildGoPackageRequest, ...] = (),
) -> BuildGoPackageRequest:
    return BuildGoPackageRequest(
        import_path=import_path,
        pkg_name="pkg",
        digest=EMPTY_DIGEST,
        dir_path="pkg",
        build_opts=GoBuildOptions(),
        go_files=("pkg.go",),
        s_files=(),
        direct_dependencies=dependencies,
        minimum_go_version="1.18",
    )


def test_equal_requests_keep_hash_and_dictionary_key_contract() -> None:
    left = request(dependencies=(request("dep"),))
    right = request(dependencies=(request("dep"),))
    assert left is not right
    assert left.direct_dependencies[0] is not right.direct_dependencies[0]
    assert left == right
    assert right == left
    assert hash(left) == hash(right)
    assert {left: "compiled"}[right] == "compiled"


@pytest.mark.parametrize("dependencies", [("b", "a"), ("a",), ("a", "b", "c")])
def test_dependency_order_and_length_are_part_of_equality(dependencies: tuple[str, ...]) -> None:
    left = request(dependencies=(request("a"), request("b")))
    right = request(dependencies=tuple(request(name) for name in dependencies))
    right._hashcode = left._hashcode
    assert left != right
    assert right != left


@pytest.mark.parametrize(
    "field",
    [
        "import_path",
        "pkg_name",
        "digest",
        "dir_path",
        "build_opts",
        "import_map",
        "go_files",
        "s_files",
        "minimum_go_version",
        "for_tests",
        "embed_config",
        "with_coverage",
        "cgo_files",
        "cgo_flags",
        "c_files",
        "header_files",
        "cxx_files",
        "objc_files",
        "fortran_files",
        "prebuilt_object_files",
        "pkg_specific_compiler_flags",
        "pkg_specific_assembler_flags",
        "is_stdlib",
    ],
)
def test_equal_cached_hashes_do_not_hide_metadata_differences(field: str) -> None:
    left, right = request(), request()
    # Deliberately retain the cached hash while changing exactly one comparison field.
    setattr(right, field, object())
    assert hash(left) == hash(right)
    assert left != right


def test_identity_and_unrelated_type_comparisons() -> None:
    value = request()
    assert value == value
    assert value.__eq__(object()) is NotImplemented
