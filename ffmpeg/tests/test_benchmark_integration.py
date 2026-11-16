"""Integration guarantees for the performance work.

These tests do not assert absolute timings (machine-dependent and flaky) but
they do assert the invariants the optimization must preserve:

* the library and the raw CLI produce byte-comparable output parameters for
  every supported operation (crop, scale, concat);
* failure scenarios still raise ``ffmpeg.Error`` with the unchanged message
  while the equivalent CLI also fails;
* phase-level timelines are emitted when requested and are inert otherwise.
"""

from __future__ import unicode_literals

import os
import subprocess
import sys

import pytest

BENCH_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BENCH_DIR)

import ffmpeg  # noqa: E402
from benchmarks import common  # noqa: E402
from benchmarks import benchmark as bench  # noqa: E402


@pytest.fixture(scope='module')
def media():
    return common.ensure_media()


@pytest.fixture()
def scratch(tmp_path):
    return str(tmp_path)


@pytest.mark.parametrize('operation', bench.OPERATIONS)
def test_library_and_cli_outputs_have_identical_params(media, scratch, operation):
    source, parts = media
    lib_out = os.path.join(scratch, '{}_lib.mp4'.format(operation))
    cli_out = os.path.join(scratch, '{}_cli.mp4'.format(operation))

    common.build_graph(operation, source, parts, lib_out).run(
        quiet=True, overwrite_output=True
    )
    graph = common.build_graph(operation, source, parts, cli_out)
    common.run_cli(graph, cli_out)

    assert common.probe_parameters(lib_out) == common.probe_parameters(cli_out)


@pytest.mark.parametrize('operation', bench.OPERATIONS)
def test_library_run_employs_expected_argv(media, scratch, operation):
    source, parts = media
    out = os.path.join(scratch, 'argv.mp4')
    graph = common.build_graph(operation, source, parts, out)
    argv = ffmpeg.compile(graph)
    # The executable name is unchanged by default; output is the final token.
    assert argv[0] == 'ffmpeg'
    assert argv[-1] == out


@pytest.mark.parametrize('operation', bench.OPERATIONS)
def test_timeline_records_all_stages_when_enabled(media, scratch, operation):
    source, parts = media
    out = os.path.join(scratch, 'timeline.mp4')
    graph = common.build_graph(operation, source, parts, out)
    _, timeline = common.run_library(graph)
    stages = {event['stage'] for event in timeline}
    assert {'compile', 'popen_spawn', 'run_async', 'communicate'} <= stages


@pytest.mark.parametrize('case', bench.FAILURE_CASES, ids=lambda c: c['name'])
def test_failure_behaviour_matches_cli(media, scratch, case):
    source, parts = media
    out = os.path.join(scratch, 'fail.mp4')
    graph = case['build'](source, parts, out)
    with pytest.raises(ffmpeg.Error) as excinfo:
        graph.run(quiet=True, overwrite_output=True)
    assert str(excinfo.value) == 'ffmpeg error (see stderr output for detail)'

    argv = ffmpeg.compile(graph)
    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    proc.communicate()
    assert proc.returncode != 0


def test_probe_is_still_performed_and_timed(media):
    source, _ = media
    timeline = []
    data = ffmpeg.probe(source, timeline=timeline)
    assert set(data.keys()) == {'format', 'streams'}
    assert 'probe_communicate' in [event['stage'] for event in timeline]
