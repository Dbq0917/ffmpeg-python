"""Benchmark ffmpeg-python vs the raw ffmpeg command line.

Runs crop, scale and concat through both code paths, records a phase-level
timeline for every sample, compares output-file parameters, asserts that
failure/error behaviour is unchanged, and archives everything under
``benchmarks/perf_archive/<timestamp>/`` for at least three months.
"""

from __future__ import print_function, unicode_literals

import argparse
import cProfile
import csv
import datetime
import io
import json
import os
import pstats
import subprocess
import sys
import time
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchmarks.common import (  # noqa: E402
    ARCHIVE_DIR,
    build_graph,
    environment_info,
    ensure_media,
    probe_parameters,
    run_cli,
    run_library,
    summarize,
    write_json,
)
import ffmpeg  # noqa: E402

OPERATIONS = ['crop', 'scale', 'concat']


def _new_archive_dir():
    stamp = datetime.datetime.now().strftime('%Y%m%dT%H%M%S')
    target = os.path.join(ARCHIVE_DIR, stamp)
    os.makedirs(os.path.join(target, 'outputs'))
    return target, stamp


def _warm_up(source, parts):
    # Warm the filesystem/CPU caches so the first sample is not an outlier.
    warm = os.path.join(os.path.dirname(source), 'warm.mp4')
    build_graph('scale', source, parts, warm).run(quiet=True, overwrite_output=True)


def benchmark_operation(operation, source, parts, out_dir, iterations, resolve_cmd):
    lib_times, cli_times = [], []
    lib_timelines = []
    argv = None
    # Per-iteration outputs are disposable and numerous; keep them in a temp
    # directory so the archive only retains the representative final samples.
    scratch = tempfile.mkdtemp(prefix='ffmpeg_bench_{}_'.format(operation))
    for index in range(iterations):
        lib_out = os.path.join(scratch, '{}_lib_{}.mp4'.format(operation, index))
        cli_out = os.path.join(scratch, '{}_cli_{}.mp4'.format(operation, index))
        graph = build_graph(operation, source, parts, lib_out)
        elapsed, timeline = run_library(graph, resolve_cmd=resolve_cmd)
        lib_times.append(elapsed)
        lib_timelines.append(timeline)
        cli_graph = build_graph(operation, source, parts, cli_out)
        cli_elapsed, argv = run_cli(cli_graph, cli_out)
        cli_times.append(cli_elapsed)

    # Keep one representative output of each path for the parameter diff.
    keep_lib = os.path.join(out_dir, '{}_lib_final.mp4'.format(operation))
    keep_cli = os.path.join(out_dir, '{}_cli_final.mp4'.format(operation))
    build_graph(operation, source, parts, keep_lib).run(
        quiet=True, overwrite_output=True
    )
    cli_graph = build_graph(operation, source, parts, keep_cli)
    _, argv = run_cli(cli_graph, keep_cli)

    lib_params = probe_parameters(keep_lib)
    cli_params = probe_parameters(keep_cli)
    write_json(
        os.path.join(out_dir, '{}_params_lib.json'.format(operation)), lib_params
    )
    write_json(
        os.path.join(out_dir, '{}_params_cli.json'.format(operation)), cli_params
    )

    stage_summary = ffmpeg.StageTimer.aggregate(lib_timelines)
    return {
        'operation': operation,
        'argv': argv,
        'library': summarize(lib_times),
        'cli': summarize(cli_times),
        'library_vs_cli_ratio': round(
            summarize(lib_times)['median_ms'] / summarize(cli_times)['median_ms'],
            4,
        ),
        'stage_summary_ms': stage_summary,
        'output_params_equal': lib_params == cli_params,
        'lib_params': lib_params,
        'cli_params': cli_params,
        'samples': {
            'library_seconds': lib_times,
            'cli_seconds': cli_times,
            'timelines': lib_timelines,
        },
    }


def measure_spawn_overhead(iterations):
    """Isolate the fixed cost of launching an (almost no-op) ffmpeg process."""
    times = []
    for _ in range(iterations):
        start = time.perf_counter()
        proc = subprocess.Popen(
            ['ffmpeg', '-version'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.wait()
        times.append(time.perf_counter() - start)
    return summarize(times)


def measure_probe_overhead(source, iterations):
    times = []
    for _ in range(iterations):
        start = time.perf_counter()
        ffmpeg.probe(source)  # probing is kept; measured, never skipped/cached
        times.append(time.perf_counter() - start)
    return summarize(times)


FAILURE_CASES = [
    {
        'name': 'missing_input_file',
        'build': lambda src, parts, out: ffmpeg.input(
            os.path.join(os.path.dirname(src), 'does_not_exist.mp4')
        ).output(out, preset='ultrafast', y=None),
    },
    {
        'name': 'invalid_filter_arguments',
        'build': lambda src, parts, out: ffmpeg.input(src)
        .filter('scale', 'not_a_number', 100)
        .output(out, preset='ultrafast', y=None),
    },
]


def run_failure_scenarios(source, parts, out_dir):
    results = []
    for case in FAILURE_CASES:
        out = os.path.join(out_dir, 'fail_{}.mp4'.format(case['name']))
        graph = case['build'](source, parts, out)
        record = {'name': case['name']}
        try:
            graph.run(quiet=True, overwrite_output=True)
            record['raised'] = False
        except ffmpeg.Error as exc:
            record['raised'] = True
            record['error_message'] = str(exc)
            record['has_stderr'] = exc.stderr is not None and len(exc.stderr) > 0
        # Mirror through the raw CLI to confirm identical exit status.
        argv = ffmpeg.compile(graph)
        proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        _, stderr = proc.communicate()
        record['cli_returncode'] = proc.returncode
        record['cli_nonzero'] = proc.returncode != 0
        record['library_and_cli_both_failed'] = (
            record.get('raised') is True and record['cli_nonzero']
        )
        results.append(record)
    return results


def write_csv(path, operation_results):
    with open(path, 'w', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                'operation',
                'path',
                'count',
                'min_ms',
                'median_ms',
                'p90_ms',
                'mean_ms',
                'ratio_vs_cli_median',
            ]
        )
        for result in operation_results:
            ratio = result['library_vs_cli_ratio']
            for label, stats in (
                ('library', result['library']),
                ('cli', result['cli']),
            ):
                writer.writerow(
                    [
                        result['operation'],
                        label,
                        stats['count'],
                        stats['min_ms'],
                        stats['median_ms'],
                        stats['p90_ms'],
                        stats['mean_ms'],
                        ratio if label == 'library' else 1.0,
                    ]
                )


def write_markdown(path, stamp, env, operation_results, spawn, probe, failures):
    lines = [
        '# ffmpeg-python 性能基准报告',
        '',
        '- 运行时间戳: `{}`'.format(stamp),
        '- git sha: `{}`'.format(env['git_sha']),
        '- python: `{}`'.format(env['python']),
        '- ffmpeg: `{}`'.format(env['ffmpeg']),
        '',
        '## 库 vs 命令行（中位数，毫秒）',
        '',
        '| 操作 | 库 median | CLI median | 库/CLI | 输出参数一致 |',
        '| --- | --- | --- | --- | --- |',
    ]
    for result in operation_results:
        lines.append(
            '| {} | {:.2f} | {:.2f} | {}x | {} |'.format(
                result['operation'],
                result['library']['median_ms'],
                result['cli']['median_ms'],
                result['library_vs_cli_ratio'],
                result['output_params_equal'],
            )
        )
    lines += [
        '',
        '## 固定开销（批量场景占大头）',
        '',
        '- 纯进程启动 `ffmpeg -version`: median `{:.2f}ms`'.format(spawn['median_ms']),
        '- 单次 `ffmpeg.probe`（保留探测，仅计量）: median `{:.2f}ms`'.format(
            probe['median_ms']
        ),
        '',
        '## 分阶段耗时（库路径，毫秒汇总）',
        '',
        '| 操作 | 阶段 | 次数 | 平均 ms | 合计 ms |',
        '| --- | --- | --- | --- | --- |',
    ]
    for result in operation_results:
        for stage, stats in result['stage_summary_ms'].items():
            lines.append(
                '| {} | {} | {} | {:.3f} | {:.3f} |'.format(
                    result['operation'],
                    stage,
                    stats['count'],
                    stats['avg_ms'],
                    stats['total_ms'],
                )
            )
    lines += ['', '## 失败场景', '']
    for record in failures:
        lines.append(
            '- `{}`: raised={}, message="{}", 库与CLI均失败={}'.format(
                record['name'],
                record['raised'],
                record.get('error_message'),
                record['library_and_cli_both_failed'],
            )
        )
    with open(path, 'w') as handle:
        handle.write('\n'.join(lines) + '\n')


def render_flamegraph(target):
    """Profile a representative batch and emit a flame-graph SVG (flameprof)."""
    source, parts = ensure_media()
    out_dir = os.path.join(target, 'outputs')
    profiler = cProfile.Profile()
    profiler.enable()
    for _ in range(20):
        graph = build_graph('scale', source, parts, os.path.join(out_dir, 'flame.mp4'))
        graph.run(quiet=True, overwrite_output=True)
    profiler.disable()
    stats_path = os.path.join(target, 'profile.pstats')
    profiler.dump_stats(stats_path)
    buffer = io.StringIO()
    pstats.Stats(profiler, stream=buffer).sort_stats('cumulative').print_stats(30)
    with open(os.path.join(target, 'profile_top30.txt'), 'w') as handle:
        handle.write(buffer.getvalue())
    svg_path = os.path.join(target, 'flamegraph.svg')
    try:
        subprocess.check_call(
            [sys.executable, '-m', 'flameprof', stats_path],
            stdout=open(svg_path, 'w'),
        )
    except Exception as exc:  # pragma: no cover - optional visualization
        with open(svg_path, 'w') as handle:
            handle.write(
                'flameprof unavailable ({}); use profile.pstats with '
                'snakeviz/flamegraph.pl instead.\n'.format(exc)
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--iterations', type=int, default=40)
    parser.add_argument('--rebuild-media', action='store_true')
    parser.add_argument('--failure-only', action='store_true')
    parser.add_argument(
        '--resolve-cmd',
        action='store_true',
        help='Enable cached executable PATH resolution.',
    )
    args = parser.parse_args()

    source, parts = ensure_media(rebuild=args.rebuild_media)
    target, stamp = _new_archive_dir()
    out_dir = os.path.join(target, 'outputs')
    env = environment_info()

    failures = run_failure_scenarios(source, parts, out_dir)
    write_json(os.path.join(target, 'failures.json'), failures)
    if args.failure_only:
        print(json.dumps(failures, indent=2))
        return

    _warm_up(source, parts)
    operation_results = []
    for operation in OPERATIONS:
        print('Benchmarking {} ...'.format(operation))
        operation_results.append(
            benchmark_operation(
                operation, source, parts, out_dir, args.iterations, args.resolve_cmd
            )
        )

    spawn = measure_spawn_overhead(args.iterations)
    probe = measure_probe_overhead(source, args.iterations)

    payload = {
        'timestamp': stamp,
        'environment': env,
        'iterations': args.iterations,
        'resolve_cmd_enabled': args.resolve_cmd,
        'spawn_overhead': spawn,
        'probe_overhead': probe,
        'operations': operation_results,
        'failures': failures,
    }
    write_json(os.path.join(target, 'results.json'), payload)
    write_csv(os.path.join(target, 'results.csv'), operation_results)
    write_markdown(
        os.path.join(target, 'report.md'),
        stamp,
        env,
        operation_results,
        spawn,
        probe,
        failures,
    )
    render_flamegraph(target)
    print('Archived benchmark data to {}'.format(target))


if __name__ == '__main__':
    main()
