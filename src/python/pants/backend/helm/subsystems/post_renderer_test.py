# Copyright 2022 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import subprocess
from pathlib import PurePath
from textwrap import dedent

import pytest

from pants.backend.helm.subsystems import post_renderer
from pants.backend.helm.subsystems.post_renderer import HelmPostRenderer, SetupHelmPostRenderer
from pants.backend.helm.utils.yaml import MutableYamlIndex, YamlPath
from pants.core.goals.run import rules as run_rules
from pants.engine.fs import DigestContents, Snapshot
from pants.engine.process import Process, ProcessResult
from pants.engine.rules import QueryRule
from pants.testutil.rule_runner import PYTHON_BOOTSTRAP_ENV, RuleRunner


@pytest.fixture
def rule_runner() -> RuleRunner:
    rule_runner = RuleRunner(
        rules=[
            *post_renderer.rules(),
            *run_rules(),
            QueryRule(HelmPostRenderer, (SetupHelmPostRenderer,)),
            QueryRule(ProcessResult, (Process,)),
        ]
    )
    rule_runner.set_options(
        [],
        env_inherit=PYTHON_BOOTSTRAP_ENV,
    )
    return rule_runner


def test_post_renderer_is_runnable(rule_runner: RuleRunner) -> None:
    replacements = MutableYamlIndex[str]()
    replacements.insert(
        file_path=PurePath("file.yaml"),
        yaml_path=YamlPath.parse("/root/element"),
        item="replaced_value",
    )

    expected_cfg_file = dedent(
        """\
      ---
      file.yaml:
      - paths:
          /root/element: replaced_value
      """
    )

    post_renderer_setup = rule_runner.request(
        HelmPostRenderer,
        [
            SetupHelmPostRenderer(
                replacements.frozen(), description_of_origin="test_post_renderer_is_runnable"
            )
        ],
    )
    assert post_renderer_setup.exe == "post_renderer_wrapper.sh"

    input_snapshot = rule_runner.request(Snapshot, [post_renderer_setup.digest])
    assert "post_renderer.cfg.yaml" in input_snapshot.files
    assert "post_renderer_wrapper.sh" in input_snapshot.files

    input_contents = rule_runner.request(DigestContents, [post_renderer_setup.digest])
    for file in input_contents:
        if file.path == "post_renderer.cfg.yaml":
            assert file.content.decode() == expected_cfg_file
        elif file.path == "post_renderer_wrapper.sh":
            script_lines = file.content.decode().splitlines()
            assert (
                "./helm_post_renderer.pex_pex_shim.sh ./post_renderer.cfg.yaml ./__helm_stdout.yaml"
                in script_lines
            )


@pytest.mark.parametrize(
    "value, expected",
    [
        ("plain", "plain"),
        ("has space", "has space"),
        ("{chroot}/bin/tool.pex", "/sandbox dir/bin/tool.pex"),
        ("a:{chroot}/x:{chroot}/y", "a:/sandbox dir/x:/sandbox dir/y"),
        ("it's $HOME `x`", "it's $HOME `x`"),
    ],
)
def test_shell_word_expands_chroot_and_quotes_the_rest(value: str, expected: str) -> None:
    # Helm runs the wrapper directly, so `{chroot}` must become the wrapper's own working
    # directory rather than reach the command literally (as it did for `pex_binary` renderers).
    word = post_renderer._shell_word(value)
    result = subprocess.run(
        ["bash", "-c", f'{post_renderer._CHROOT_SHELL_VAR}="/sandbox dir"; printf %s {word}'],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout == expected
