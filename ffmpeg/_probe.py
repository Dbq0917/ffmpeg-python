import json
import subprocess
from ._perf import StageTimer, resolve_executable
from ._run import Error
from ._utils import convert_kwargs_to_cmd_line_args


def probe(
    filename, cmd='ffprobe', timeout=None, timeline=None, resolve_cmd=False, **kwargs
):
    """Run ffprobe on the specified file and return a JSON representation of the output.

    Raises:
        :class:`ffmpeg.Error`: if ffprobe returns a non-zero exit code,
            an :class:`Error` is returned with a generic error message.
            The stderr output can be retrieved by accessing the
            ``stderr`` property of the exception.
    """
    timer = StageTimer(timeline)
    with timer.stage('probe_compile'):
        args = [cmd, '-show_format', '-show_streams', '-of', 'json']
        args += convert_kwargs_to_cmd_line_args(kwargs)
        args += [filename]
    if resolve_cmd:
        with timer.stage('resolve_executable'):
            args = [resolve_executable(args[0])] + args[1:]

    with timer.stage('popen_spawn'):
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    communicate_kwargs = {}
    if timeout is not None:
        communicate_kwargs['timeout'] = timeout
    with timer.stage('probe_communicate'):
        out, err = p.communicate(**communicate_kwargs)
    if p.returncode != 0:
        raise Error('ffprobe', out, err)
    with timer.stage('probe_parse_json'):
        result = json.loads(out.decode('utf-8'))
    return result


__all__ = ['probe']
