"""Shared helpers for the ffmpeg-python performance benchmark suite.

Everything here is benchmark scaffolding only; it does not modify the library
under test.  The same ffmpeg argument vector is fed to both the library path
and a raw subprocess path so any measured difference is attributable to the
wrapper/process management, never to a different transcode.
"""

from __future__ import print_function, unicode_literals

import json
import os
import shutil
import subprocess
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import ffmpeg  # noqa: E402

BENCH_DIR = os.path.join(REPO_ROOT, 'benchmarks')
MEDIA_DIR = os.path.join(BENCH_DIR, 'media')
ARCHIVE_DIR = os.path.join(BENCH_DIR, 'perf_archive')

# A small, fast preset keeps transcode time modest so process-launch overhead
# stays the dominant, measurable cost (mirrors a batch of short clips).
COMMON_OUTPUT_KWARGS = dict(preset='ultrafast', y=None)


def ensure_media(rebuild=False):
    """Return (source, parts) paths, generating deterministic media if needed."""
    if not os.path.isdir(MEDIA_DIR):
        os.makedirs(MEDIA_DIR)
    source = os.path.join(MEDIA_DIR, 'source.mp4')
    if rebuild or not os.path.exists(source):
        # 8s 640x480 clip with both video and audio, generated deterministically.
        subprocess.check_call(
            [
                'ffmpeg',
                '-y',
                '-f',
                'lavfi',
                '-i',
                'testsrc=size=640x480:rate=30:duration=8',
                '-f',
                'lavfi',
                '-i',
                'sine=frequency=440:duration=8',
                '-c:v',
                'libx264',
                '-preset',
                'ultrafast',
                '-pix_fmt',
                'yuv420p',
                '-c:a',
                'aac',
                '-shortest',
                source,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    parts = []
    for idx in range(2):
        part = os.path.join(MEDIA_DIR, 'part{}.mp4'.format(idx))
        if rebuild or not os.path.exists(part):
            subprocess.check_call(
                [
                    'ffmpeg',
                    '-y',
                    '-f',
                    'lavfi',
                    '-i',
                    'testsrc=size=640x480:rate=30:duration=3',
                    '-f',
                    'lavfi',
                    '-i',
                    'sine=frequency=300:duration=3',
                    '-c:v',
                    'libx264',
                    '-preset',
                    'ultrafast',
                    '-pix_fmt',
                    'yuv420p',
                    '-c:a',
                    'aac',
                    '-shortest',
                    part,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        parts.append(part)
    return source, parts


def build_graph(operation, source, parts, output_path):
    """Build the ffmpeg-python node graph for one of the three operations."""
    if operation == 'crop':
        # Center crop to 320x240 (x/y derived from source size), no resize.
        return (
            ffmpeg.input(source)
            .crop(160, 120, 320, 240)
            .output(output_path, **COMMON_OUTPUT_KWARGS)
        )
    if operation == 'scale':
        return (
            ffmpeg.input(source)
            .filter('scale', 960, 720)
            .output(output_path, **COMMON_OUTPUT_KWARGS)
        )
    if operation == 'concat':
        # Two same-shaped inputs concatenated via the filter graph (video+audio).
        v0 = ffmpeg.input(parts[0])
        v1 = ffmpeg.input(parts[1])
        joined = ffmpeg.concat(v0, v1, v=1, a=1).node
        return ffmpeg.output(joined[0], joined[1], output_path, **COMMON_OUTPUT_KWARGS)
    raise ValueError('unknown operation: {}'.format(operation))


def run_library(graph, resolve_cmd=False):
    """Run via the public library API and return (elapsed_seconds, timeline)."""
    timeline = []
    start = time.perf_counter()
    graph.run(
        quiet=True, overwrite_output=True, timeline=timeline, resolve_cmd=resolve_cmd
    )
    elapsed = time.perf_counter() - start
    return elapsed, timeline


def run_cli(graph, output_path):
    """Run the *same* argv via a raw subprocess (the command-line baseline)."""
    argv = ffmpeg.compile(graph)
    argv = list(argv)
    argv[-1] = output_path
    start = time.perf_counter()
    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proc.wait()
    elapsed = time.perf_counter() - start
    if proc.returncode != 0:
        raise RuntimeError('CLI baseline failed: {}'.format(argv))
    return elapsed, argv


def median(values):
    values = sorted(values)
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2.0


def percentile(values, pct):
    values = sorted(values)
    idx = min(len(values) - 1, int(round((pct / 100.0) * (len(values) - 1))))
    return values[idx]


def summarize(values):
    return {
        'count': len(values),
        'min_ms': round(min(values) * 1000.0, 3),
        'median_ms': round(median(values) * 1000.0, 3),
        'p90_ms': round(percentile(values, 90) * 1000.0, 3),
        'mean_ms': round((sum(values) / len(values)) * 1000.0, 3),
    }


def probe_parameters(path):
    """Return a compact, comparable fingerprint of an output's parameters."""
    data = ffmpeg.probe(path)
    fingerprint = {'format': {}, 'streams': []}
    fmt = data.get('format', {})
    fingerprint['format'] = {
        'format_name': fmt.get('format_name'),
        'duration': fmt.get('duration'),
        'bit_rate': fmt.get('bit_rate'),
        'nb_streams': fmt.get('nb_streams'),
    }
    for stream in data.get('streams', []):
        compact = {
            'codec_type': stream.get('codec_type'),
            'codec_name': stream.get('codec_name'),
            'width': stream.get('width'),
            'height': stream.get('height'),
            'pix_fmt': stream.get('pix_fmt'),
            'sample_rate': stream.get('sample_rate'),
            'channels': stream.get('channels'),
            'nb_frames': stream.get('nb_frames'),
            'r_frame_rate': stream.get('r_frame_rate'),
        }
        fingerprint['streams'].append(compact)
    return fingerprint


def environment_info():
    ffmpeg_version = (
        subprocess.check_output(['ffmpeg', '-version'])
        .decode('utf-8', 'replace')
        .splitlines()[0]
    )
    try:
        sha = (
            subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO_ROOT)
            .decode('utf-8', 'replace')
            .strip()
        )
    except Exception:
        sha = 'unknown'
    return {
        'python': sys.version.split()[0],
        'ffmpeg': ffmpeg_version,
        'ffmpeg_path': shutil.which('ffmpeg'),
        'ffprobe_path': shutil.which('ffprobe'),
        'git_sha': sha,
        'platform': sys.platform,
    }


def write_json(path, payload):
    with open(path, 'w') as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
