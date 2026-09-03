# Copyright 2022 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

import os.path
import textwrap
from collections import defaultdict
from dataclasses import dataclass

from pants.backend.go.lint.golangci_lint.skip_field import SkipGolangciLintField
from pants.backend.go.lint.golangci_lint.subsystem import GolangciLint
from pants.backend.go.subsystems.golang import GolangSubsystem
from pants.backend.go.target_types import GoPackageSourcesField
from pants.backend.go.util_rules.build_opts import (
    GoBuildOptionsFromTargetRequest,
    go_extract_build_options_from_target,
)
from pants.backend.go.util_rules.go_bootstrap import GoBootstrap
from pants.backend.go.util_rules.go_mod import (
    GoModInfoRequest,
    OwningGoModRequest,
    determine_go_mod_info,
    find_owning_go_mod,
)
from pants.backend.go.util_rules.goroot import GoRoot
from pants.core.goals.lint import LintResult, LintTargetsRequest
from pants.core.goals.resolves import ExportableTool
from pants.core.util_rules.config_files import find_config_file
from pants.core.util_rules.env_vars import environment_vars_subset
from pants.core.util_rules.external_tool import download_external_tool
from pants.core.util_rules.partitions import Partition, PartitionerType, Partitions
from pants.core.util_rules.source_files import SourceFilesRequest, determine_source_files
from pants.core.util_rules.system_binaries import (
    BashBinary,
    BinaryShimsRequest,
    create_binary_shims,
)
from pants.engine.addresses import Address
from pants.engine.env_vars import EnvironmentVarsRequest
from pants.engine.fs import CreateDigest, Digest, FileContent, MergeDigests
from pants.engine.internals.graph import transitive_targets as transitive_targets_get
from pants.engine.internals.selectors import concurrently
from pants.engine.intrinsics import create_digest, execute_process, merge_digests
from pants.engine.platform import Platform
from pants.engine.process import Process
from pants.engine.rules import collect_rules, implicitly, rule
from pants.engine.target import FieldSet, SourcesField, Target, TransitiveTargetsRequest
from pants.engine.unions import UnionRule
from pants.util.logging import LogLevel


@dataclass(frozen=True)
class GolangciLintPartitionMetadata:
    """Metadata for a golangci-lint partition, identifying the go.mod context."""

    go_mod_address: Address

    @property
    def description(self) -> str:
        return f"module {self.go_mod_address}"


@dataclass(frozen=True)
class GolangciLintFieldSet(FieldSet):
    required_fields = (GoPackageSourcesField,)

    sources: GoPackageSourcesField

    @classmethod
    def opt_out(cls, tgt: Target) -> bool:
        return tgt.get(SkipGolangciLintField).value


class GolangciLintRequest(LintTargetsRequest):
    field_set_type = GolangciLintFieldSet
    tool_subsystem = GolangciLint  # type: ignore[assignment]
    partitioner_type = PartitionerType.CUSTOM


@rule(desc="Partition golangci-lint by go.mod", level=LogLevel.DEBUG)
async def partition_golangci_lint(
    request: GolangciLintRequest.PartitionRequest[GolangciLintFieldSet],
    golangci_lint: GolangciLint,
) -> Partitions[GolangciLintFieldSet, GolangciLintPartitionMetadata]:
    if golangci_lint.skip:
        return Partitions()

    # Find the owning go.mod for each field set
    owning_go_mods = await concurrently(
        find_owning_go_mod(OwningGoModRequest(fs.address), **implicitly())
        for fs in request.field_sets
    )

    # Group field sets by their owning go.mod
    by_go_mod: dict[Address, list[GolangciLintFieldSet]] = defaultdict(list)
    for field_set, owning in zip(request.field_sets, owning_go_mods):
        by_go_mod[owning.address].append(field_set)

    return Partitions(
        Partition(tuple(field_sets), GolangciLintPartitionMetadata(go_mod_addr))
        for go_mod_addr, field_sets in by_go_mod.items()
    )


@rule(desc="Lint with golangci-lint", level=LogLevel.DEBUG)
async def run_golangci_lint(
    request: GolangciLintRequest.Batch[GolangciLintFieldSet, GolangciLintPartitionMetadata],
    golangci_lint: GolangciLint,
    goroot: GoRoot,
    bash: BashBinary,
    platform: Platform,
    golang_subsystem: GolangSubsystem,
    golang_env_aware: GolangSubsystem.EnvironmentAware,
    go_bootstrap: GoBootstrap,
) -> LintResult:
    # Get the single go.mod address for this partition
    go_mod_address = request.partition_metadata.go_mod_address
    go_mod_dir = os.path.normpath(go_mod_address.spec_path) if go_mod_address.spec_path else ""

    transitive_targets = await transitive_targets_get(
        TransitiveTargetsRequest(field_set.address for field_set in request.elements),
        **implicitly(),
    )
    all_source_files_request = determine_source_files(
        SourceFilesRequest(
            tgt[SourcesField] for tgt in transitive_targets.closure if tgt.has_field(SourcesField)
        )
    )
    target_source_files_request = determine_source_files(
        SourceFilesRequest(field_set.sources for field_set in request.elements)
    )
    downloaded_golangci_lint_request = download_external_tool(golangci_lint.get_request(platform))
    config_files_request = find_config_file(golangci_lint.config_request())
    go_mod_info_request = determine_go_mod_info(GoModInfoRequest(go_mod_address))
    go_build_opts_request = go_extract_build_options_from_target(
        GoBuildOptionsFromTargetRequest(go_mod_address), **implicitly()
    )
    # golangci-lint runs its own `go list`-style module loading, so it needs the same
    # environment the Go SDK processes get in `setup_go_sdk_process`: the
    # `[golang].subprocess_env_vars` (e.g. `HOME` for git credential/URL-rewrite config,
    # `GOPRIVATE`, `GONOSUMDB`). Without these, any module that is only reachable through
    # an authenticated VCS fetch fails to typecheck and the whole partition fails.
    env_vars_request = environment_vars_subset(
        EnvironmentVarsRequest(golang_env_aware.env_vars_to_pass_to_subprocesses),
        **implicitly(),
    )

    (
        target_source_files,
        all_source_files,
        downloaded_golangci_lint,
        config_files,
        go_mod_info,
        go_build_opts,
        env_vars,
    ) = await concurrently(
        target_source_files_request,
        all_source_files_request,
        downloaded_golangci_lint_request,
        config_files_request,
        go_mod_info_request,
        go_build_opts_request,
        env_vars_request,
    )

    cgo_enabled = go_build_opts.cgo_enabled

    # If cgo is enabled, golangci-lint needs to be able to locate the
    # associated tools in its environment. This is injected in $PATH in the
    # wrapper script.
    tool_search_path = ":".join(
        ["${GOROOT}/bin", *(golang_env_aware.cgo_tool_search_paths if cgo_enabled else ())]
    )

    env: dict[str, str] = dict(env_vars)
    immutable_input_digests: dict[str, Digest] = {}
    # `[golang].extra_tools` (e.g. `git`, which `go` shells out to for VCS-backed modules)
    # are exposed through binary shims exactly as for the Go SDK processes.
    if golang_env_aware.extra_tools:
        extra_tools = await create_binary_shims(
            BinaryShimsRequest.for_binaries(
                *golang_env_aware.extra_tools,
                rationale="allow additional tools for golangci-lint",
                search_path=go_bootstrap.go_search_paths,
            ),
            bash,
        )
        env["PATH"] = (
            f"{extra_tools.path_component}:{env['PATH']}"
            if env.get("PATH")
            else extra_tools.path_component
        )
        immutable_input_digests.update(extra_tools.immutable_input_digests)

    # Compute package directories relative to the go.mod directory
    package_dirs = sorted(
        {
            os.path.relpath(os.path.dirname(f), go_mod_dir) if go_mod_dir else os.path.dirname(f)
            for f in target_source_files.snapshot.files
        }
    )

    # Compute path prefix to access sandbox root from working_directory
    # e.g., if working_directory is "foo/bar", prefix is "../../"
    sandbox_root_prefix = ""
    if go_mod_dir:
        depth = len(go_mod_dir.split(os.sep))
        sandbox_root_prefix = "../" * depth

    # The module cache, the Go build cache and golangci-lint's own analysis cache are
    # append-only named caches shared across partitions and runs. A fresh sandbox
    # GOPATH would make every partition re-download the module closure of the packages
    # it lints (the Go SDK processes never pay this: they compile from digests). All
    # three are designed for concurrent, content-addressed use, and golangci-lint runs
    # with `--allow-parallel-runners`.
    append_only_caches = {
        "golangci_lint_gomodcache": ".cache/golangci_lint/gomodcache",
        "golangci_lint_gocache": ".cache/golangci_lint/gocache",
        "golangci_lint_cache": ".cache/golangci_lint/lintcache",
    }
    # Absolute paths (via `{chroot}` interpolation in env values): the process runs with
    # `working_directory` set to the module directory, and `go` requires absolute cache paths.
    env["GOMODCACHE"] = "{chroot}/.cache/golangci_lint/gomodcache"
    env["GOCACHE"] = "{chroot}/.cache/golangci_lint/gocache"
    env["GOLANGCI_LINT_CACHE"] = "{chroot}/.cache/golangci_lint/lintcache"

    # golangci-lint requires an absolute path to a cache. The tool search path goes
    # first so the pinned GOROOT's `go` wins; the environment's PATH (binary shims for
    # `[golang].extra_tools`, then whatever `[golang].subprocess_env_vars` forwarded)
    # follows so `go` can find e.g. `git`.
    golangci_lint_run_script = FileContent(
        "__run_golangci_lint.sh",
        textwrap.dedent(
            f"""\
            export GOROOT={goroot.path}
            sandbox_root="$(/bin/pwd)"
            export PATH="{tool_search_path}${{PATH:+:$PATH}}"
            export GOPATH="${{sandbox_root}}/gopath"
            export CGO_ENABLED={1 if cgo_enabled else 0}
            export GOTOOLCHAIN=local
            /bin/mkdir -p "$GOPATH" "$GOMODCACHE" "$GOCACHE" "$GOLANGCI_LINT_CACHE"
            exec "$@"
            """
        ).encode("utf-8"),
    )

    golangci_lint_run_script_digest = await create_digest(CreateDigest([golangci_lint_run_script]))
    input_digest = await merge_digests(
        MergeDigests(
            [
                golangci_lint_run_script_digest,
                downloaded_golangci_lint.digest,
                config_files.snapshot.digest,
                target_source_files.snapshot.digest,
                all_source_files.snapshot.digest,
                go_mod_info.digest,
            ]
        )
    )

    # Adjust paths to be relative to working_directory
    script_path = f"{sandbox_root_prefix}{golangci_lint_run_script.path}"
    exe_path = f"{sandbox_root_prefix}{downloaded_golangci_lint.exe}"

    argv: list[str] = [
        bash.path,
        script_path,
        exe_path,
        "run",
        # keep golangci-lint from complaining
        # about concurrent runs
        "--allow-parallel-runners",
    ]
    if golangci_lint.config:
        config_path = f"{sandbox_root_prefix}{golangci_lint.config}"
        argv.append(f"--config={config_path}")
    elif config_files.snapshot.files:
        config_path = f"{sandbox_root_prefix}{config_files.snapshot.files[0]}"
        argv.append(f"--config={config_path}")
    else:
        argv.append("--no-config")
    argv.extend(golangci_lint.args)
    # Add package paths relative to the module root
    argv.extend(f"./{p}" if p != "." else "./..." for p in package_dirs)

    process_result = await execute_process(
        Process(
            argv=argv,
            input_digest=input_digest,
            immutable_input_digests=immutable_input_digests,
            append_only_caches=append_only_caches,
            env=env,
            description=f"Run `golangci-lint` on {request.partition_metadata.description}.",
            level=LogLevel.DEBUG,
            working_directory=go_mod_dir or None,
        ),
        **implicitly(),
    )
    return LintResult.create(request, process_result)


def rules():
    return (
        *collect_rules(),
        *GolangciLintRequest.rules(),
        UnionRule(ExportableTool, GolangciLint),
    )
