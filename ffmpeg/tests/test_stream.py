from __future__ import unicode_literals

import io
import subprocess
import threading
import time

import pytest

import ffmpeg

DURATION = 600
SAMPLE_RATE = 8000
BLOCK_SIZE = 4096


def _output_stream(path):
    return ffmpeg.input(path).output(
        'pipe:',
        format='s16le',
        acodec='pcm_s16le',
        ac=1,
        ar=SAMPLE_RATE,
    )


def _make_ten_minute_media(directory):
    wav_path = str(directory.join('ten-minutes.wav'))
    mp3_path = str(directory.join('ten-minutes.mp3'))
    subprocess.check_call(
        [
            'ffmpeg',
            '-v',
            'error',
            '-y',
            '-f',
            'lavfi',
            '-i',
            'sine=frequency=440:sample_rate={}:duration={}'.format(
                SAMPLE_RATE, DURATION
            ),
            '-ac',
            '1',
            '-c:a',
            'pcm_s16le',
            wav_path,
        ]
    )
    try:
        subprocess.check_call(
            [
                'ffmpeg',
                '-v',
                'error',
                '-y',
                '-i',
                wav_path,
                '-c:a',
                'libmp3lame',
                '-b:a',
                '32k',
                mp3_path,
            ]
        )
    except subprocess.CalledProcessError:
        subprocess.check_call(
            [
                'ffmpeg',
                '-v',
                'error',
                '-y',
                '-i',
                wav_path,
                '-c:a',
                'mp3',
                '-b:a',
                '32k',
                mp3_path,
            ]
        )
    return {'wav': wav_path, 'mp3': mp3_path}


@pytest.fixture(scope='module')
def long_media(tmpdir_factory):
    directory = tmpdir_factory.mktemp('ffmpeg-stream')
    paths = _make_ten_minute_media(directory)
    full_output = {}
    for name, path in paths.items():
        full_output[name], _ = ffmpeg.run(
            _output_stream(path), capture_stdout=True, capture_stderr=True
        )
    return {'paths': paths, 'full_output': full_output}


@pytest.mark.parametrize('name', ['wav', 'mp3'])
def test_chunked_read_matches_full_read(long_media, name):
    path = long_media['paths'][name]
    expected = long_media['full_output'][name]
    stream = _output_stream(path)

    ffmpeg.stream_metrics.reset()
    started = time.time()
    chunks = []
    with ffmpeg.open_stream(stream) as reader:
        while True:
            chunk = reader.read(BLOCK_SIZE)
            if not chunk:
                break
            chunks.append(chunk)
    elapsed = time.time() - started
    actual = b''.join(chunks)

    assert actual == expected
    assert len(actual) == DURATION * SAMPLE_RATE * 2
    assert reader.initializations == 1
    assert reader.decoder_creations == 1
    assert reader.position == len(expected)
    assert reader.read_calls >= (len(expected) + BLOCK_SIZE - 1) // BLOCK_SIZE
    assert reader.closed

    snapshot = ffmpeg.stream_metrics.snapshot()
    assert snapshot['initializations'] == 1
    assert snapshot['decoder_creations'] == 1
    assert snapshot['init_count'] == 1
    assert snapshot['decoder_count'] == 1
    assert snapshot['bytes_read'] == len(expected)
    assert snapshot['active_readers'] == 0

    # Re-opening ffmpeg for each block would be thousands of process starts.
    assert elapsed < 10.0


@pytest.mark.parametrize('name', ['wav', 'mp3'])
def test_output_stream_read_keeps_process_alive(long_media, name):
    path = long_media['paths'][name]
    expected = long_media['full_output'][name]
    stream = _output_stream(path)

    ffmpeg.stream_metrics.reset()
    chunks = []
    while True:
        chunk = stream.read(BLOCK_SIZE)
        if not chunk:
            break
        chunks.append(chunk)
    stream.close()

    assert b''.join(chunks) == expected
    snapshot = ffmpeg.stream_metrics.snapshot()
    assert snapshot['initializations'] == 1
    assert snapshot['decoder_creations'] == 1
    assert snapshot['active_readers'] == 0


class _FakeProcess(object):
    def __init__(self, output, stderr=b'', returncode=0):
        self.stdout = io.BytesIO(output)
        self.stderr = io.BytesIO(stderr)
        self.stdin = None
        self.pid = 1234
        self.returncode = returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


def test_error_reports_byte_position_and_stderr():
    process = _FakeProcess(b'0123456789', stderr=b'bad packet at frame 7', returncode=1)
    reader = ffmpeg.StreamReader(process)
    assert reader.initializations == 0
    assert reader.decoder_creations == 0

    assert reader.read(4) == b'0123'
    assert reader.read(6) == b'456789'
    time.sleep(0.05)

    with pytest.raises(ffmpeg.StreamReadError) as excinfo:
        reader.read(4)

    error = excinfo.value
    assert isinstance(error, ffmpeg.Error)
    assert error.position == 10
    assert error.offset == 10
    assert error.chunk_start == 10
    assert error.returncode == 1
    assert b'bad packet at frame 7' in error.stderr
    assert 'at byte 10' in str(error)
    reader.close()


def test_wrapping_existing_run_async_process_adds_no_decoder(long_media):
    path = long_media['paths']['wav']
    expected = long_media['full_output']['wav']
    stream = _output_stream(path)

    ffmpeg.stream_metrics.reset()
    process = ffmpeg.run_async(stream, pipe_stdout=True)
    after_start = ffmpeg.stream_metrics.snapshot()
    reader = ffmpeg.open_stream(process)
    chunks = []
    while True:
        chunk = reader.read(BLOCK_SIZE)
        if not chunk:
            break
        chunks.append(chunk)
    reader.close()

    assert b''.join(chunks) == expected
    assert process.poll() is not None
    after_read = ffmpeg.stream_metrics.snapshot()
    assert after_start['initializations'] == 1
    assert after_start['decoder_creations'] == 1
    assert after_read['initializations'] == after_start['initializations']
    assert after_read['decoder_creations'] == after_start['decoder_creations']


def test_cancel_releases_blocked_read_and_process():
    stream = (
        ffmpeg.input(
            'pipe:',
            format='rawvideo',
            pixel_format='rgb24',
            video_size=(32, 32),
            framerate=10,
        )
        .trim(start_frame=0)
        .output('pipe:1', format='rawvideo')
    )
    reader = ffmpeg.open_stream(stream)
    result = {}

    def blocked_read():
        try:
            result['data'] = reader.read(BLOCK_SIZE)
        except Exception as exc:
            result['error'] = exc

    thread = threading.Thread(target=blocked_read)
    thread.start()
    time.sleep(0.3)
    reader.cancel()
    thread.join(3.0)

    assert not thread.is_alive()
    assert isinstance(result.get('error'), ffmpeg.StreamCancelledError)
    assert result['error'].position == 0
    assert reader.closed
    assert reader.process.poll() is not None
    snapshot = ffmpeg.stream_metrics.snapshot()
    assert snapshot['cancellations'] >= 1
    assert snapshot['active_readers'] == 0
