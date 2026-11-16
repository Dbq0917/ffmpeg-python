# 视频转码库性能优化任务清单

## 已确认的事实（实测，见 benchmarks/ 留档数据）
- 同一条 ffmpeg argv，库路径与原生 subprocess 路径的稳态耗时一致（~118ms/短视频）。
- 参数图构建 + compile 合计 < 0.05ms，不是瓶颈（拼装缓存只快 10% 与此吻合）。
- 主要耗时 = 每次调用都新起 ffmpeg 进程（~20ms 纯 spawn）+ ffmpeg 内 demux/解码器初始化/编码/收尾，
  批量小文件时进程启动类开销占大头。
- ffmpeg.probe 每次是独立 ffprobe 进程（~21ms），属探测成本，不能跳过，需独立计量。

## 约束
- 公开 API 签名与返回/异常行为不变；ffmpeg argv 与输出文件参数不变。
- 不减少校验、不跳过探测；Error 消息与 stderr 内容不变。
- 现有 Popen kwargs 被测试精确断言，进程创建的可观测行为必须保持。

## 计划
1. 新增可选的可执行文件路径解析缓存（shutil.which），消除重复 PATH 查找；默认行为不变。
2. 新增分阶段计时探针（timeline），按阶段输出：graph_build/compile/popen_spawn/ffmpeg_run/communicate/probe。
3. 新增 benchmarks/：裁剪/缩放/拼接 三操作 × 库 vs CLI 基准，含进程启动开销、输出参数比对、失败场景断言。
4. 用 cProfile + flameprof 产出火焰图（SVG），性能数据 JSON/CSV 留档，保留期 >= 3 个月。
5. 补充单元测试覆盖新工具函数；全量回归保证既有行为不变。

## 结论（实测，见 benchmarks/perf_archive/）
- **库并不比命令行慢**：同一条 argv，库/CLI 中位数比值 crop 1.13x、scale 1.01x、concat 1.03x，
  差异在机器噪声范围内；火焰图显示 >99% 时间在 `posix.waitpid` 等待 ffmpeg 子进程。
- **参数拼装不是瓶颈**：compile 平均约 0.2ms，占总耗时 < 0.2%，与“缓存拼装只快 10%”吻合。
- **进程启动 + 解码器初始化是大头**：纯进程 spawn 约 28ms，probe 约 31ms；其余是 ffmpeg 内部
  demux/解码/滤镜/编码/封装，库与 CLI 完全相同，无法在不改输出的前提下从 Python 侧消除。
- PATH 查找仅约 9 微秒，`resolve_cmd` 缓存属低风险清理项，收益在噪声级，默认关闭以保持行为不变。
- 输出参数库与 CLI 完全一致；失败仍抛 `ffmpeg.Error`，消息 `ffmpeg error (see stderr output for detail)` 不变。
- 若要进一步压缩批量成本，方向是减少进程数（单进程多输出 / 常驻 ffmpeg 服务 / concat demuxer 批处理），
  这会改变调用模型，需单独立项，不在本次“接口不变”约束内。
