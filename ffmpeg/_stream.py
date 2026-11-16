from __future__ import unicode_literals

from builtins import object
from ._run import Error, compile
from .dag import topo_sort
from .nodes import InputNode, get_stream_spec_nodes, output_operator

import logging
import subprocess
import threading
import time
import weakref

logger = logging.getLogger(__name__)


_PIPE_INPUT_NAMES = ('pipe:', 'pipe:0', '-')
_DEFAULT_STDERR_TAIL_SIZE = 64 * 1024
_DEFAULT_CHUNK_SIZE = 64 * 1024


class StreamMetrics(object):
    """Process-wide counters for persistent decoding streams.

    ``initializations`` counts ffmpeg processes that were started.
    ``decoder_creations`` counts input decoders that belong to those processes
    (one per input node).  A healthy chunked read has one initialization and a
    constant number of decoder creations, no matter how many chunks are read.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        with self._lock:
            self.initializations = 0
            self.decoder_creations = 0
            self.read_calls = 0
            self.bytes_read = 0
            self.errors = 0
            self.cancellations = 0
            self.active_readers = 0

    def record_initialization(self, decoder_count=1):
        with self._lock:
            self.initializations += 1
            self.decoder_creations += decoder_count
            self.active_readers += 1

    def record_process_start(self, decoder_count=1):
        """Record a process started outside a managed StreamReader."""
        with self._lock:
            self.initializations += 1
            self.decoder_creations += decoder_count

    def record_close(self):
        with self._lock:
            if self.active_readers > 0:
                self.active_readers -= 1

    def record_read(self, size):
        with self._lock:
            self.read_calls += 1
            self.bytes_read += size

    def record_error(self):
        with self._lock:
            self.errors += 1

    def record_cancellation(self):
        with self._lock:
            self.cancellations += 1

    def snapshot(self):
        """Return a copy of the counters, suitable for logging or monitoring."""
        with self._lock:
            snapshot = {
                'initializations': self.initializations,
                'decoder_creations': self.decoder_creations,
                'read_calls': self.read_calls,
                'bytes_read': self.bytes_read,
                'errors': self.errors,
                'cancellations': self.cancellations,
                'active_readers': self.active_readers,
            }
            snapshot['init_count'] = snapshot['initializations']
            snapshot['open_count'] = snapshot['initializations']
            snapshot['process_creations'] = snapshot['initializations']
            snapshot['decoder_count'] = snapshot['decoder_creations']
            return snapshot

    as_dict = snapshot


stream_metrics = StreamMetrics()


def record_process_start(stream_spec):
    """Record an externally managed process start for monitoring."""
    decoder_count = StreamReader._count_input_decoders(stream_spec)
    stream_metrics.record_process_start(decoder_count)
    logger.info('ffmpeg process initialized via run_async decoders=%s', decoder_count)


class StreamReadError(Error):
    """Raised when a persistent stream fails while it is being read.

    ``position``/``offset`` is the number of bytes that were successfully
    returned before the failure, so callers can resume or report the exact
    location that failed.  ``chunk_start`` is the position at the beginning of
    the ``read()`` call that detected the failure.
    """

    cancelled = False

    def __init__(
        self,
        cmd,
        stdout,
        stderr,
        position=0,
        chunk_start=None,
        requested=None,
        returncode=None,
    ):
        super(StreamReadError, self).__init__(cmd, stdout, stderr)
        self.cmd = cmd
        self.position = position
        self.offset = position
        self.failed_at = position
        self.bytes_read = position
        self.chunk_start = chunk_start
        self.requested = requested
        self.returncode = returncode
        self.args = ('{} at byte {}'.format(self.args[0], position),)


class StreamCancelledError(StreamReadError):
    """Raised by a blocked ``read()`` when the stream is cancelled."""

    cancelled = True

    def __init__(self, *args, **kwargs):
        super(StreamCancelledError, self).__init__(*args, **kwargs)
        self.args = ('ffmpeg stream read cancelled at byte {}'.format(self.position),)


def _is_process(obj):
    return hasattr(obj, 'stdout') and hasattr(obj, 'poll') and hasattr(obj, 'terminate')


class StreamReader(object):
    """A file-like reader backed by one long-lived ffmpeg process.

    The process is initialized lazily on construction (or explicitly with
    :func:`open_stream`) and is reused by every :meth:`read`.  Decoding state
    therefore survives across chunk boundaries, unlike an implementation that
    launches ffmpeg once per chunk.

    The reader is intentionally bounded: it does not retain decoded data.  A
    ``read(size)`` allocates at most ``size`` bytes, and only a fixed-size tail
    of stderr is retained for diagnostics.

    ``StreamReader`` implements the small file-like surface used by existing
    pipe readers: ``read()``, ``readinto()``, ``tell()``, ``close()`` and the
    context-manager protocol.  ``read()`` returns ``bytes``, matching
    ``subprocess.Popen.stdout.read()``.
    """

    def __init__(
        self,
        stream_spec,
        cmd='ffmpeg',
        pipe_stdin=None,
        pipe_stderr=True,
        quiet=False,
        overwrite_output=False,
        cwd=None,
        stderr_tail_size=_DEFAULT_STDERR_TAIL_SIZE,
        bufsize=0,
        chunk_size=_DEFAULT_CHUNK_SIZE,
    ):
        self._cmd = cmd
        self._stream_spec = stream_spec
        self._chunk_size = chunk_size
        self._stderr_tail_size = max(0, int(stderr_tail_size))
        self._stderr_tail = b''
        self._stderr_lock = threading.Lock()
        self._read_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._stderr_thread = None
        self._position = 0
        self._read_calls = 0
        self._eof = False
        self._closed = False
        self._cancelled = False
        self._initialized = False
        self._init_count = 0
        self._decoder_count = 0
        self._process = None
        self._stdin = None
        self._stdout = None
        self._stderr = None

        if _is_process(stream_spec):
            # Compatibility path: wrap a Popen that a caller already created
            # with run_async(pipe_stdout=True).  No new decoder or process is
            # created here.
            self._process = stream_spec
            self._stdin = getattr(stream_spec, 'stdin', None)
            self._stdout = getattr(stream_spec, 'stdout', None)
            self._stderr = getattr(stream_spec, 'stderr', None)
            if self._stdout is None:
                raise ValueError(
                    'The supplied process has no stdout pipe; '
                    'use run_async(pipe_stdout=True)'
                )
            self._start_stderr_drain()
            return

        args = compile(stream_spec, cmd, overwrite_output=overwrite_output)
        if pipe_stdin is None:
            pipe_stdin = self._input_uses_stdin(stream_spec)
        if quiet:
            pipe_stderr = False

        self._init_count = 1
        self._decoder_count = self._count_input_decoders(stream_spec)
        self._process = subprocess.Popen(
            args,
            stdin=subprocess.PIPE if pipe_stdin else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if pipe_stderr else None,
            cwd=cwd,
            bufsize=bufsize,
        )
        self._stdin = self._process.stdin
        self._stdout = self._process.stdout
        self._stderr = self._process.stderr
        self._initialized = True
        stream_metrics.record_initialization(self._decoder_count)
        logger.info(
            'ffmpeg stream initialized pid=%s decoders=%s',
            getattr(self._process, 'pid', None),
            self._decoder_count,
        )
        self._start_stderr_drain()

    @staticmethod
    def _count_input_decoders(stream_spec):
        try:
            nodes = get_stream_spec_nodes(stream_spec)
            sorted_nodes, _ = topo_sort(nodes)
            return sum(1 for node in sorted_nodes if isinstance(node, InputNode))
        except Exception:
            return 1

    @staticmethod
    def _input_uses_stdin(stream_spec):
        try:
            nodes = get_stream_spec_nodes(stream_spec)
            sorted_nodes, _ = topo_sort(nodes)
            for node in sorted_nodes:
                if isinstance(node, InputNode):
                    filename = node.kwargs.get('filename')
                    if filename in _PIPE_INPUT_NAMES:
                        return True
        except Exception:
            pass
        return False

    def _start_stderr_drain(self):
        if self._stderr is None:
            return

        def drain():
            try:
                while True:
                    chunk = self._stderr.read(4096)
                    if not chunk:
                        break
                    if self._stderr_tail_size == 0:
                        continue
                    with self._stderr_lock:
                        tail = self._stderr_tail + chunk
                        if len(tail) > self._stderr_tail_size:
                            tail = tail[-self._stderr_tail_size :]
                        self._stderr_tail = tail
            except Exception:
                # stderr is diagnostic only; never let it break decoding.
                pass

        self._stderr_thread = threading.Thread(target=drain)
        self._stderr_thread.daemon = True
        self._stderr_thread.start()

    @property
    def process(self):
        return self._process

    @property
    def stdout(self):
        return self._stdout

    @property
    def stdin(self):
        return self._stdin

    @property
    def stderr(self):
        return self._stderr

    @property
    def stderr_tail(self):
        with self._stderr_lock:
            return self._stderr_tail

    @property
    def position(self):
        return self._position

    @property
    def offset(self):
        return self._position

    @property
    def initializations(self):
        return self._init_count

    @property
    def init_count(self):
        return self._init_count

    @property
    def decoder_creations(self):
        return self._decoder_count

    @property
    def decoder_count(self):
        return self._decoder_count

    @property
    def decoder_creation_count(self):
        return self._decoder_count

    @property
    def open_count(self):
        return self._init_count

    @property
    def bytes_read(self):
        return self._position

    @property
    def read_calls(self):
        return self._read_calls

    @property
    def eof(self):
        return self._eof

    @property
    def closed(self):
        return self._closed

    @property
    def cancelled(self):
        return self._cancelled

    def readable(self):
        return not self._closed

    def writable(self):
        return self._stdin is not None

    def seekable(self):
        return False

    def tell(self):
        return self._position

    def fileno(self):
        return self._stdout.fileno()

    def write(self, data):
        if self._stdin is None:
            raise IOError('stream is not writable')
        written = 0
        while written < len(data):
            count = self._stdin.write(data[written:])
            if count is None:
                count = len(data) - written
            if count <= 0:
                raise IOError('failed to write to ffmpeg stdin')
            written += count
        if hasattr(self._stdin, 'flush'):
            self._stdin.flush()
        return len(data)

    def read(self, size=-1):
        if size == 0:
            return b''

        with self._read_lock:
            if self._cancelled:
                raise self._cancelled_error(size, self._position)
            if self._closed:
                raise ValueError('I/O operation on closed stream')
            if self._eof:
                return b''

            self._read_calls += 1
            start = self._position
            try:
                data = self._read_data(size)
            except (IOError, OSError, ValueError) as exc:
                if self._cancelled:
                    raise self._cancelled_error(size, start)
                self._fail(start, size, exc)
                raise

            if data:
                self._position += len(data)
                stream_metrics.record_read(len(data))
                return data

            self._eof = True
            returncode = self._poll_until(5.0)
            if self._cancelled:
                raise self._cancelled_error(size, start)
            if returncode is None:
                self._shutdown()
                returncode = self._poll_until(1.0)
            if returncode not in (0, None):
                self._raise_failure(start, size, returncode)
            stream_metrics.record_read(0)
            return b''

    def _read_data(self, size):
        if size is None or size < 0:
            parts = []
            while True:
                chunk = self._stdout.read(_DEFAULT_CHUNK_SIZE)
                if not chunk:
                    break
                parts.append(chunk)
            return b''.join(parts)

        parts = []
        remaining = size
        while remaining > 0:
            chunk = self._stdout.read(remaining)
            if not chunk:
                break
            parts.append(chunk)
            remaining -= len(chunk)
        return b''.join(parts)

    def readinto(self, buffer):
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def readline(self, size=-1):
        parts = []
        total = 0
        while size is None or size < 0 or total < size:
            byte = self.read(1)
            if not byte:
                break
            parts.append(byte)
            total += 1
            if byte == b'\n':
                break
        return b''.join(parts)

    def chunks(self, size=None):
        """Yield decoded bytes in bounded chunks until EOF."""
        if size is None:
            size = self._chunk_size
        while True:
            data = self.read(size)
            if not data:
                return
            yield data

    def read_chunk(self, size=-1):
        """Alias for :meth:`read`, for callers that name block reads explicitly."""
        return self.read(size)

    iter_chunks = chunks

    def stats(self):
        return stream_metrics.snapshot()

    metrics = stats

    def __iter__(self):
        return self

    def __next__(self):
        data = self.read(self._chunk_size)
        if not data:
            raise StopIteration
        return data

    next = __next__

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    def cancel(self):
        """Stop a reader, including a read blocked in another thread."""
        with self._close_lock:
            if not self._cancelled:
                self._cancelled = True
                stream_metrics.record_cancellation()
            self._shutdown()

    def close(self):
        with self._close_lock:
            self._shutdown()

    def _shutdown(self):
        if self._closed:
            return

        process = self._process
        try:
            if self._stdin is not None:
                self._stdin.close()
        except Exception:
            pass

        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except Exception:
                pass
            if self._poll_until(1.0) is None:
                try:
                    process.kill()
                except Exception:
                    pass
                self._poll_until(1.0)

        for stream in (self._stdout, self._stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass

        thread = self._stderr_thread
        if thread is not None and thread.is_alive():
            thread.join(1.0)

        self._closed = True
        if self._initialized:
            stream_metrics.record_close()
            self._initialized = False

    def _poll_until(self, timeout):
        process = self._process
        if process is None:
            return 0
        deadline = time.time() + timeout
        while True:
            returncode = process.poll()
            if returncode is not None:
                return returncode
            if time.time() >= deadline:
                return None
            time.sleep(0.005)

    def _fail(self, start, requested, cause):
        stream_metrics.record_error()
        returncode = self._poll_until(0.1)
        if returncode is None:
            self._shutdown()
            returncode = self._poll_until(1.0)
        error = StreamReadError(
            self._cmd,
            None,
            self.stderr_tail,
            position=self._position,
            chunk_start=start,
            requested=requested,
            returncode=returncode,
        )
        error.__cause__ = cause
        logger.error(
            'ffmpeg stream read failed at byte %s (chunk_start=%s): %s',
            self._position,
            start,
            cause,
        )
        raise error

    def _raise_failure(self, start, requested, returncode):
        stream_metrics.record_error()
        error = StreamReadError(
            self._cmd,
            None,
            self.stderr_tail,
            position=self._position,
            chunk_start=start,
            requested=requested,
            returncode=returncode,
        )
        logger.error(
            'ffmpeg stream exited with code %s at byte %s', returncode, self._position
        )
        raise error

    def _cancelled_error(self, requested, start):
        stream_metrics.record_error()
        return StreamCancelledError(
            self._cmd,
            None,
            self.stderr_tail,
            position=self._position,
            chunk_start=start,
            requested=requested,
            returncode=self._process.poll() if self._process is not None else None,
        )

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def __repr__(self):
        return (
            '<StreamReader position={} initializations={} decoders={} closed={}>'
        ).format(
            self._position,
            self._init_count,
            self._decoder_count,
            self._closed,
        )


def open_stream(
    stream_spec,
    cmd='ffmpeg',
    pipe_stdin=None,
    pipe_stderr=True,
    quiet=False,
    overwrite_output=False,
    cwd=None,
    stderr_tail_size=_DEFAULT_STDERR_TAIL_SIZE,
    bufsize=0,
    chunk_size=_DEFAULT_CHUNK_SIZE,
    **kwargs
):
    """Open a decoded output stream once and read it in bounded chunks.

    This is the persistent counterpart to ``run_async(pipe_stdout=True)``.  It
    starts ffmpeg exactly once; every subsequent ``read(size)`` consumes the
    same process without seeking or re-initializing decoders::

        stream = ffmpeg.input('long.wav').output(
            'pipe:', format='s16le', acodec='pcm_s16le'
        )
        with ffmpeg.open_stream(stream) as reader:
            while True:
                chunk = reader.read(64 * 1024)
                if not chunk:
                    break
                consume(chunk)

    A ``Popen`` created by ``run_async(pipe_stdout=True)`` can also be wrapped
    directly, in which case no additional process or decoder is created.
    """
    # Accept a few run_async-compatible keyword names so existing call sites
    # can migrate without changing their option spelling.
    kwargs.pop('pipe_stdout', True)
    kwargs.pop('capture_stdout', True)
    capture_stderr = kwargs.pop('capture_stderr', None)
    if capture_stderr is not None:
        pipe_stderr = capture_stderr
    if kwargs:
        unexpected = next(iter(kwargs))
        raise TypeError('Unexpected keyword argument {!r}'.format(unexpected))
    return StreamReader(
        stream_spec,
        cmd=cmd,
        pipe_stdin=pipe_stdin,
        pipe_stderr=pipe_stderr,
        quiet=quiet,
        overwrite_output=overwrite_output,
        cwd=cwd,
        stderr_tail_size=stderr_tail_size,
        bufsize=bufsize,
        chunk_size=chunk_size,
    )


def stream(stream_spec, **kwargs):
    """Alias for :func:`open_stream`."""
    return open_stream(stream_spec, **kwargs)


def read_stream(stream_spec, size=None, **kwargs):
    """Return a reader, or read one chunk when ``size`` is supplied."""
    if size is None:
        size = kwargs.pop('chunk_size', None)
    if size is None:
        return open_stream(stream_spec, **kwargs)
    return _get_stream_reader(stream_spec, kwargs).read(size)


def iter_stream(stream_spec, size=_DEFAULT_CHUNK_SIZE, **kwargs):
    """Yield bounded chunks from one persistent stream until EOF."""
    if 'chunk_size' in kwargs:
        size = kwargs.pop('chunk_size')
    with open_stream(stream_spec, **kwargs) as reader:
        for chunk in reader.chunks(size):
            yield chunk


stream_chunks = iter_stream


_stream_readers = weakref.WeakKeyDictionary()
_stream_readers_lock = threading.Lock()


def _get_stream_reader(stream_spec, kwargs):
    with _stream_readers_lock:
        reader = _stream_readers.get(stream_spec)
        if reader is None or reader.closed:
            reader = open_stream(stream_spec, **kwargs)
            _stream_readers[stream_spec] = reader
        return reader


@output_operator(name='open')
def _open_output_stream(stream_spec, **kwargs):
    """Open this output stream as a persistent :class:`StreamReader`."""
    return _get_stream_reader(stream_spec, kwargs)


@output_operator(name='read')
def read(stream_spec, size=-1, **kwargs):
    """Read decoded bytes from a persistent output stream.

    The first call opens the stream; later calls reuse the same ffmpeg process
    and continue from the current position.  This method intentionally returns
    ``bytes``, just like ``Popen.stdout.read(size)``.
    """
    return _get_stream_reader(stream_spec, kwargs).read(size)


@output_operator(name='close')
def close(stream_spec):
    """Close and reap the persistent reader associated with an output stream."""
    with _stream_readers_lock:
        reader = _stream_readers.pop(stream_spec, None)
    if reader is not None:
        reader.close()


@output_operator(name='cancel')
def cancel(stream_spec):
    """Cancel a blocked read and release the associated ffmpeg process."""
    with _stream_readers_lock:
        reader = _stream_readers.pop(stream_spec, None)
    if reader is not None:
        reader.cancel()


def open(stream_spec, **kwargs):
    """Alias for :func:`open_stream`."""
    return open_stream(stream_spec, **kwargs)


__all__ = [
    'StreamReader',
    'StreamReadError',
    'StreamCancelledError',
    'StreamMetrics',
    'stream_metrics',
    'open_stream',
    'open',
    'stream',
    'read_stream',
    'iter_stream',
    'stream_chunks',
    'read',
    'close',
    'cancel',
]
