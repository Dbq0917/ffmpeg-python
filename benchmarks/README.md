# 转码性能基准与留档

本目录用于定位 `ffmpeg-python` 库相对裸 `ffmpeg` 命令行的性能开销，并把每次基准数据
留档，便于三个月以上的复盘对比。

## 设计原则
- **不改输出**：库与 CLI 使用完全相同的 ffmpeg 参数，输出文件用 ffprobe 做参数比对。
- **不跳探测、不减校验**：`ffmpeg.probe` 等探测单独计量，不缓存探测结果。
- **进程启动计入**：每个样本都重新起进程，分别记录 spawn / ffmpeg 运行 / 通信等阶段。
- **三种操作都测**：裁剪(crop/trim)、缩放(scale)、拼接(concat)，避免单操作下结论。

## 用法
```bash
# 生成测试素材（默认 tests/sample_data/in1.mp4，缺失时用 lavfi 生成）
python3 benchmarks/benchmark.py --rebuild-media

# 完整基准：库 vs CLI × 三操作，写 JSON/CSV/Markdown，并生成火焰图
python3 benchmarks/benchmark.py --iterations 40

# 仅失败场景，断言错误信息不变
python3 benchmarks/benchmark.py --failure-only
```

## 产物（见 `perf_archive/<时间戳>/`）
- `results.json`：每条样本的分阶段 timeline 与汇总（机器、ffmpeg 版本、git sha）。
- `results.csv`：扁平表，方便复盘与画图。
- `report.md`：人读报告，含库 vs CLI 耗时对比与差距倍数。
- `flamegraph.svg`：cProfile + flameprof 生成的火焰图（等价可视化）。
- `outputs/`：库与 CLI 的实际输出及 ffprobe 参数快照（用于参数比对）。
- `failures.json`：失败场景与错误信息断言结果。

## 留档保留策略
`perf_archive/` 下的数据设计为**至少保留 3 个月**。该目录与 `media/` 已在
`.gitignore` 中忽略（体积大、可由脚本再生成），实际部署时应挂到持久化磁盘/对象存储，
并由定时任务执行保留脚本。每份数据目录以时间戳命名，并用 `git sha` 标注被测代码版本，
复盘时用「时间戳 + git sha」即可对齐前后两版数据。

仓库内置一个保留策略脚本：
```bash
python3 benchmarks/retention.py --keep-days 90 --apply   # 默认 dry-run
```
CI/定时任务可定期执行；过期（默认 >90 天）的归档会被清理，未识别命名的目录不会被误删。

## 阶段定义（timeline）
- `compile`：从节点图生成 ffmpeg 参数（参数拼装），实测 < 0.3ms，非瓶颈。
- `resolve_executable`：可执行文件 PATH 解析（仅 `resolve_cmd=True` 时出现）。
- `popen_spawn`：`subprocess.Popen` 创建子进程（fork/exec），即进程启动开销。
- `run_async`：`run_async` 整体（含 compile + resolve + spawn）。
- `communicate`：等待 ffmpeg 运行结束并回收，包含 demux/解码器初始化/滤镜/编码/封装，
  是绝对大头；火焰图中表现为 `posix.waitpid`。
- probe 相关：`probe_compile` / `popen_spawn` / `probe_communicate` / `probe_parse_json`，
  探测始终保留，只单独计量。
