from __future__ import unicode_literals

import subprocess

import pytest

import ffmpeg
from ffmpeg import _perf, _run, _probe

try:
    import mock  # python 2
except ImportError:
    from unittest import mock


TEST_INPUT_FILE1 = __file__.rsplit('/', 1)[0] + '/sample_data/in1.mp4'


@pytest.fixture(autouse=True)
def _reset_executable_cache():
    _perf.clear_executable_cache()
    yield
    _perf.clear_executable_cache()


def test_resolve_executable_caches_path_lookup(mocker):
    which = mocker.patch.object(_perf.shutil, 'which', return_value='/usr/bin/ffmpeg')
    assert _perf.resolve_executable('ffmpeg') == '/usr/bin/ffmpeg'
    assert _perf.resolve_executable('ffmpeg') == '/usr/bin/ffmpeg'
    assert which.call_count == 1


def test_resolve_executable_leaves_paths_untouched(mocker):
    which = mocker.patch.object(_perf.shutil, 'which')
    assert _perf.resolve_executable('/custom/bin/ffmpeg') == '/custom/bin/ffmpeg'
    assert which.call_count == 0


def test_resolve_executable_falls_back_to_name_when_missing(mocker):
    mocker.patch.object(_perf.shutil, 'which', return_value=None)
    assert _perf.resolve_executable('nope') == 'nope'


def test_resolve_executable_non_string_passthrough():
    sentinel = object()
    assert _perf.resolve_executable(sentinel) is sentinel


def test_clear_executable_cache_forces_relookup(mocker):
    which = mocker.patch.object(_perf.shutil, 'which', return_value='/usr/bin/ffprobe')
    _perf.resolve_executable('ffprobe')
    _perf.clear_executable_cache()
    _perf.resolve_executable('ffprobe')
    assert which.call_count == 2


def test_stage_timer_inert_by_default():
    timer = _perf.StageTimer()
    assert timer.enabled is False
    with timer.stage('anything'):
        pass
    # No timeline attached => nothing recorded, no exception.


def test_stage_timer_records_stages_in_order():
    timeline = []
    timer = _perf.StageTimer(timeline)
    with timer.stage('a'):
        pass
    with timer.stage('b'):
        pass
    assert [event['stage'] for event in timeline] == ['a', 'b']
    assert all(event['elapsed_ms'] >= 0 for event in timeline)


def test_stage_timer_does_not_swallow_exceptions():
    timeline = []
    timer = _perf.StageTimer(timeline)
    with pytest.raises(RuntimeError):
        with timer.stage('boom'):
            raise RuntimeError('x')
    assert timeline[0]['stage'] == 'boom'


def test_stage_timer_record_and_aggregate():
    timelines = [
        [{'stage': 'a', 'elapsed_ms': 10.0}, {'stage': 'b', 'elapsed_ms': 5.0}],
        [{'stage': 'a', 'elapsed_ms': 20.0}, {'stage': 'b', 'elapsed_ms': 15.0}],
    ]
    summary = _perf.StageTimer.aggregate(timelines)
    assert summary['a'] == {'count': 2, 'total_ms': 30.0, 'avg_ms': 15.0}
    assert summary['b']['count'] == 2


def test_run_async_records_timeline_and_popen_kwargs(mocker):
    process = mock.Mock()
    popen = mocker.patch.object(subprocess, 'Popen', return_value=process)
    stream = ffmpeg.input('in.mp4').output('out.mp4')
    timeline = []
    result = ffmpeg.run_async(stream, quiet=True, timeline=timeline)
    assert result is process
    stages = [event['stage'] for event in timeline]
    assert 'compile' in stages and 'popen_spawn' in stages
    (args,), kwargs = popen.call_args
    # Default behaviour preserved: bare executable name and exact Popen kwargs.
    assert args[0] == 'ffmpeg'
    assert kwargs == dict(
        stdin=None,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        cwd=None,
    )


def test_run_async_resolve_cmd_uses_absolute_path(mocker):
    mocker.patch.object(_perf.shutil, 'which', return_value='/usr/bin/ffmpeg')
    popen = mocker.patch.object(subprocess, 'Popen', return_value=mock.Mock())
    stream = ffmpeg.input('in.mp4').output('out.mp4')
    ffmpeg.run_async(stream, timeline=[], resolve_cmd=True)
    (args,), _ = popen.call_args
    assert args[0] == '/usr/bin/ffmpeg'
    # ffmpeg arguments after the executable remain byte-for-byte identical.
    assert args[1:] == ffmpeg.compile(stream)[1:]


def test_run_timeline_records_communicate_stage(mocker):
    process = mock.Mock()
    process.communicate.return_value = (b'out', b'err')
    process.poll.return_value = 0
    mocker.patch.object(_run, 'run_async', return_value=process)
    timeline = []
    out, err = ffmpeg.run(ffmpeg.input('in.mp4').output('out.mp4'), timeline=timeline)
    assert (out, err) == (b'out', b'err')
    assert 'communicate' in [event['stage'] for event in timeline]


def test_run_error_path_unchanged_with_timeline(mocker):
    process = mock.Mock()
    process.communicate.return_value = (None, b'boom')
    process.poll.return_value = 1
    mocker.patch.object(_run, 'run_async', return_value=process)
    with pytest.raises(ffmpeg.Error) as excinfo:
        ffmpeg.run(ffmpeg.input('in.mp4').output('out.mp4'), timeline=[])
    assert str(excinfo.value) == 'ffmpeg error (see stderr output for detail)'
    assert excinfo.value.stderr == b'boom'


def test_probe_timeline_and_resolve_cmd(mocker):
    which = mocker.patch.object(_perf.shutil, 'which', return_value='/usr/bin/ffprobe')
    process = mock.Mock()
    process.communicate.return_value = (b'{"format": {}, "streams": []}', b'')
    process.returncode = 0
    popen = mocker.patch.object(subprocess, 'Popen', return_value=process)
    timeline = []
    result = ffmpeg.probe('in.mp4', timeline=timeline, resolve_cmd=True)
    assert result == {'format': {}, 'streams': []}
    (args,), kwargs = popen.call_args
    assert args[0] == '/usr/bin/ffprobe'
    assert kwargs == dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert which.called
    assert 'probe_communicate' in [event['stage'] for event in timeline]


def test_probe_default_executable_name_unchanged(mocker):
    popen = mocker.patch.object(
        subprocess,
        'Popen',
        return_value=mock.Mock(
            communicate=mock.Mock(return_value=(b'{"format": {}, "streams": []}', b'')),
            returncode=0,
        ),
    )
    ffmpeg.probe('in.mp4')
    (args,), _ = popen.call_args
    assert args[0] == 'ffprobe'
