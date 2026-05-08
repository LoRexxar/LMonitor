# SimC 后端二进制下载更新容错设计

## 背景

当前 SimC nightly 更新在后端 `SimcMonitor.ensure_simc_backend_up_to_date()` 中执行，下载使用 `requests.get(stream=True)`。在网络抖动/连接超时场景下容易失败，导致更新失败，进而影响后续 SimC 任务执行。

本设计仅增强“下载更新”链路的容错能力，不改变前端交互与触发方式（仍为后端自动触发，前端仅展示状态）。

## 目标

- 明显降低因网络抖动、超时导致的更新失败率
- 失败时给出更明确、可行动的错误信息（写入 `SimcBackendBinary.last_error` 与 `update_status`）
- 兼容现有目录结构与进度展示（`update_progress/update_status/is_updating`）
- 检查更新间隔缩短为 30 分钟（可配置）
- 支持 Windows/Linux 双平台（Windows 使用 win64 包，Linux 使用 linux 包）
- 下载与安装目录固定在进程当前工作目录下（`./bin/simc/...`）

## 非目标

- 不新增前端主动触发更新入口
- 不改变解压/定位 `simc.exe`/切换 `simc_path` 的既有逻辑
- 不引入新的强依赖（允许“可选依赖/可选外部工具”作为兜底）

## 现状与风险点

- 下载链接为 `http://downloads.simulationcraft.org/...`，在部分网络环境下更容易被中间设备干扰
- 仅单路径下载，失败后直接中断；虽然支持 `.part` 断点续传，但对 Range 不支持/服务端行为变化缺少强兜底
- 网络失败信息多为异常字符串，缺少统一分类与重试策略
- 当前实现固定 `platform=win64`，不支持 Linux 平台包与解压格式扩展

## 方案概述（主链路增强 + 系统下载兜底）

### 主链路（Python 下载器增强）

对现有 `_download_file()` 增强：

- 统一超时策略：连接超时 + 读取超时区分设置
- 重试策略：针对网络/超时类异常进行有限次重试（指数退避）
- 断点续传容错：
  - 服务器不支持 Range/返回 200 与预期不一致时，自动清理 `.part` 并改为全量下载
  - 明确处理 416（Requested Range Not Satisfiable）：清理 `.part` 后重试
- 链接优先级：优先使用配置的下载源；如需镜像可通过配置项扩展
- 下载完成后仍沿用现有压缩包校验 `_validate_archive()`，校验失败视为“不完整下载”，触发重试/兜底

### 兜底链路（系统下载器）

当主链路在可重试错误下仍失败时，尝试使用系统能力下载：

- Windows 优先：PowerShell BITS（`Start-BitsTransfer`），更适配系统网络栈、代理与断点续传
- 可选：若 BITS 不可用，尝试探测 `curl`（Windows 10/11 通常自带）或 `wget`
- 兜底下载到 `archive_path.part`，成功后原子替换为正式文件
- 下载完成后继续走同一套校验/解压/定位逻辑

## 配置与可观测性

- 复用现有 `SimcBackendBinary` 状态字段：
  - `update_status`：展示“重试中/切换兜底下载/兜底下载中”等状态文本
  - `update_progress`：主链路沿用现有 1-80% 映射；兜底下载阶段可用“未知/粗粒度”进度（BITS/curl 进度难统一）
  - `last_error`：写入最终失败原因，并尽量包含可行动建议（例如“多次超时，已尝试 BITS 仍失败”）
- 不新增 DB 字段；如需调参，优先以 `settings.SIMC_CONFIG` 增加可选项（例如 `download_retry`, `download_timeout`, `download_fallback`, `update_check_interval_seconds`）

## 失败场景处理

- 网络抖动/超时：主链路重试 → 兜底下载 → 最终失败写明重试次数与最后异常
- 下载站不可达/DNS：快速失败并提示网络环境问题
- 下载文件损坏：删除损坏文件与 `.part` 后重下（主链路/兜底均如此）

## 验证计划

- 人工验证：
  - 正常网络：确认能下载/解压/更新完成，前端进度条状态正常更新
  - 模拟超时：将超时时间调小或阻断网络，观察重试与兜底路径是否生效
- 回归验证：
  - 不影响 `SimcMonitor.process_simc_task()` 的任务执行路径
  - 旧版本已下载文件存在时仍可“跳过下载”并正常解压/定位
