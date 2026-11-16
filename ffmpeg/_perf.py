"""Performance instrumentation and process-launch helpers.

This module is intentionally side-effect free with respect to the ffmpeg
command line: nothing here changes the arguments passed to ffmpeg or the
produced output files.  It provides two opt-in facilities used by
:mod:`ffmpeg._run` and :mod:`ffmpeg._probe`:

* :func:`resolve_executable` / :func:`clear_executable_cache` - cache the
  ``PATH`` lookup (``shutil.which``) of an executable so a long batch run
  does not repeat the same lookup for every single transcode.
* :class:`StageTimer` - a lightweight, always-safe stage timer that records a
  phase-by-phase timeline (graph build, compile, process spawn, ffmpeg run,
  communicate, probe).  Recording is disabled unless a timeline object is
  attached, so the hot path stays allocation free when profiling is off.
"""

from __future__ import unicode_literals

import shutil
import threading
import time

from ._utils import basestring

_executable_cache = {}
_executable_cache_lock = threading.Lock()


def resolve_executable(cmd):
    """Return an executable path usable directly by :class:`subprocess.Popen`.

    A bare command name (e.g. ``'ffmpeg'``) is resolved through ``PATH`` once
    and cached, avoiding a repeated ``PATH`` search on every invocation in a
    batch.  Absolute or relative paths containing a directory separator are
    returned unchanged because :func:`shutil.which` does not alter them and
    :class:`subprocess.Popen` can exec them directly.

    If the command cannot be found on ``PATH`` the original value is returned
    untouched; the subsequent :class:`subprocess.Popen` then raises the exact
    same :class:`FileNotFoundError` it always did, preserving error behaviour.
    """
    if not isinstance(cmd, basestring):
        return cmd
    if '/' in cmd:
        return cmd
    cached = _executable_cache.get(cmd)
    if cached is not None:
        return cached
    with _executable_cache_lock:
        resolved = _executable_cache.get(cmd)
        if resolved is None:
            resolved = shutil.which(cmd) or cmd
            _executable_cache[cmd] = resolved
    return resolved


def clear_executable_cache():
    """Drop all cached executable resolutions (mainly useful for tests)."""
    with _executable_cache_lock:
        _executable_cache.clear()


class StageTimer(object):
    """Accumulate per-stage wall-clock timings for one or more invocations.

    A timer is either *recording* (a timeline list was supplied) or inert.
    Inert timers keep :meth:`stage` as a cheap no-op so callers do not pay any
    measurement cost unless profiling was explicitly requested.

    Each recorded stage is a dict::

        {'stage': 'popen_spawn', 'elapsed_ms': 1.234}

    Stages are recorded in call order so the timeline doubles as a crude
    sequence/flame view.  :meth:`aggregate` sums timings across many
    invocations to produce an aggregate breakdown suitable for archival.
    """

    def __init__(self, timeline=None, clock=None):
        self._timeline = timeline
        self._clock = clock or time.perf_counter

    @property
    def enabled(self):
        return self._timeline is not None

    def stage(self, name):
        """Return a context manager that times ``name`` when recording."""
        timeline = self._timeline
        clock = self._clock

        class _Stage(object):
            def __enter__(self):
                if timeline is not None:
                    self._start = clock()
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                if timeline is not None:
                    timeline.append(
                        {
                            'stage': name,
                            'elapsed_ms': (clock() - self._start) * 1000.0,
                        }
                    )
                return False

        return _Stage()

    def record(self, name, elapsed_seconds):
        """Record an already-measured duration, in seconds."""
        if self._timeline is not None:
            self._timeline.append(
                {'stage': name, 'elapsed_ms': elapsed_seconds * 1000.0}
            )

    @staticmethod
    def aggregate(timelines):
        """Sum stage timings across timelines into a serializable summary."""
        totals = {}
        counts = {}
        order = []
        for timeline in timelines:
            for event in timeline:
                stage = event['stage']
                if stage not in totals:
                    totals[stage] = 0.0
                    counts[stage] = 0
                    order.append(stage)
                totals[stage] += event['elapsed_ms']
                counts[stage] += 1
        return {
            stage: {
                'count': counts[stage],
                'total_ms': round(totals[stage], 4),
                'avg_ms': round(totals[stage] / counts[stage], 4),
            }
            for stage in order
        }


__all__ = [
    'StageTimer',
    'clear_executable_cache',
    'resolve_executable',
]
