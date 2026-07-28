# ToupTek 4040 Alpaca Camera V4 设计

日期：2026-07-28

目标设备：ToupTek `sMAX16AM-U3-CL-GPS`

目标传感器尺寸：4096 × 4096

目标协议：ASCOM Alpaca Device API v1、ICameraV4

厂商 SDK：`toupcamsdk.20250623`

## 1. 目标与范围

本项目在 `G:\ToupTek_4040` 中创建一套面向 ToupTek 4096 × 4096 单色制冷相机的控制软件。功能范围与 `G:\cosmos66` 的交付保持一致，包括：

- ASCOM Alpaca Camera V4 服务；
- Alpaca discovery、management API 和 setup 页面；
- 同端口 Web 控制台；
- 正式曝光与实时 Preview；
- RAW 8-bit、RAW 12-bit、增益、ROI、1×1 至 8×8 对称 binning；
- 温度、制冷开关和制冷功率；
- ImageBytes、流式 JSON ImageArray、FITS 下载；
- 原始像素检查、统计、直方图和 FWHM；
- 完整状态及操作日志；
- 明确启用的软件模拟器；
- 本机诊断 GUI；
- Windows 64-bit 可执行程序和中文使用说明。

实现必须以 `toupcamsdk.20250623/doc/en.html` 的线程、回调、触发、ROI、RAW、binning 和温控约束为准，并严格遵守 ASCOM ICameraV4 行为。不能把 COSMOS-66 的 PICam、18-bit、HDR、8120 × 8120 或硬件能力假设带入本项目。

所有测试源码、辅助文件、脚本、假设备和测试结果必须位于根目录 `test/` 中。

## 2. 规范依据

实现以以下资料为准：

1. ASCOM Master Interfaces：ICameraV4；
2. ASCOM Alpaca Device API v1；
3. ASCOM Alpaca API Reference Version 12；
4. ASCOM 官方 AlpycaDevice Platform 7 Camera 模板；
5. `G:\ToupTek_4040\toupcamsdk.20250623\doc\en.html`；
6. 随 SDK 提供的 `toupcam.py`、64-bit `toupcam.dll` 和官方示例。

当 COSMOS66 行为与上述规范冲突时，以 ASCOM 官方规范和本地 ToupTek SDK 文档为准。

## 3. 已确认的硬件能力

当前连接设备经 64-bit Python 和本地 SDK 读回：

| 项目 | 值 |
|---|---|
| 显示名称 / 型号 | `sMAX16AM-U3-CL-GPS` |
| 序列号 | `TP241025143218C92B01151AEC40CB7` |
| 传感器 | 单色 CMOS |
| 分辨率 | 4096 × 4096、2048 × 2048 |
| ASCOM 传感器尺寸 | 4096 × 4096 |
| 像元 | 9.0 µm × 9.0 µm |
| 原始位深 | RAW 8-bit、RAW 12-bit |
| 曝光范围 | 12 µs 至 3,600 s |
| SDK 曝光单位 | 1 µs |
| 数值增益范围 | 100 至 1650 |
| 温度设定范围 | -50.0°C 至 40.0°C |
| 制冷 | TEC 开关、目标温度、TEC 电压 |
| 风扇 | 1 至 4 档 |
| 触发 | 软件触发 |
| ROI | 硬件 ROI |
| binning | SDK 数字 1×1 至 8×8 |
| 固件 | `4.6.7.20240903` |
| 硬件版本 | `1.0` |
| FPGA | `3.17` |

2048 × 2048 是 SDK 的独立输出分辨率档。SDK 报告的分辨率比例仍为 1:1，因此它不作为 ASCOM 2×2 binning 使用。

## 4. 总体架构

项目采用以下分层：

- `touptek4040/toupcam_camera.py`：唯一直接调用 `toupcam.py` 和 `toupcam.dll` 的硬件适配层；
- `touptek4040/camera_contract.py`：真实相机和模拟器共用的内部接口；
- `touptek4040/camera_coordinator.py`：句柄所有权、状态机、配置串行化、曝光和 Preview 协调；
- `touptek4040/alpaca/`：从 ASCOM 官方 AlpycaDevice Platform 7 Camera 模板派生的 Camera V4 响应层、management、setup 和 discovery；
- `touptek4040/image_workspace.py`：不可变原图代次、PNG 视口、统计、FWHM 和下载引用；
- `touptek4040/fits_io.py`：流式 FITS；
- `touptek4040/event_broker.py`：有界状态和日志事件；
- `touptek4040/web_console.py` 与 `touptek4040/web/`：同端口 Web API、SSE 和静态界面；
- `touptek4040/sim_camera.py`：必须显式启用的软件模拟器；
- `touptek4040/gui.py`：本机诊断界面；
- `run_alpaca.py`、`run_gui.py`：程序入口。

Web、GUI 和 Alpaca 不得分别持有相机句柄。服务进程中的 `CameraCoordinator` 是相机句柄和状态的唯一所有者。

服务启动时只枚举并确认设备身份，不报告 ASCOM `Connected=True`。`Connect()` 或旧式 `Connected=True` 才打开相机。真实相机失败时必须返回明确错误，不能自动切换模拟器。

## 5. ASCOM 通用设备接口

| 成员 | 实现 |
|---|---|
| `Connect` | 异步打开选定相机；操作期间 `Connecting=True` |
| `Disconnect` | 异步取消采集、停止任务并关闭句柄 |
| `Connecting` | 准确报告连接或断开操作 |
| `Connected` GET/PUT | 保留旧客户端兼容；PUT 使用同步语义 |
| `DeviceState` | 返回 CameraState、ImageReady、PercentCompleted、温度和制冷状态 |
| `Name` | `ToupTek 4040 Alpaca Camera` |
| `Description` | 包含真实型号并限制在 64 个 ASCII 字符内 |
| `DriverInfo` | 驱动、SDK、固件、硬件和 FPGA 版本 |
| `DriverVersion` | 可解析的 `major.minor.patch` |
| `InterfaceVersion` | `4` |
| `SupportedActions` | 空数组 |
| `Action` | `ActionNotImplementedException`，错误号 `0x40C` |
| `CommandBlind` / `CommandBool` / `CommandString` | `NotImplementedException`，错误号 `0x400` |

Alpaca UID 使用序列号生成稳定 UUID，不使用随 USB 端口变化的设备路径。

## 6. 传感器、ROI 与 binning 对齐

| 成员 | 实现 |
|---|---|
| `CameraXSize` / `CameraYSize` | 4096 / 4096 |
| `PixelSizeX` / `PixelSizeY` | 9.0 / 9.0 µm |
| `SensorType` | `Monochrome` |
| `SensorName` | `sMAX16AM-U3-CL-GPS` |
| `BayerOffsetX` / `BayerOffsetY` | 单色相机必须返回 `PropertyNotImplementedException` |
| `MaxBinX` / `MaxBinY` | 8 / 8 |
| `CanAsymmetricBin` | `False` |
| `BinX` / `BinY` | 1 至 8；写任一轴时同步另一轴 |
| `StartX` / `StartY` | 合并后像素坐标 |
| `NumX` / `NumY` | 合并后输出尺寸 |

SDK ROI 坐标始终相对于原始分辨率，因此转换为：

```text
sdk_x = StartX × BinX
sdk_y = StartY × BinY
sdk_width = NumX × BinX
sdk_height = NumY × BinY
```

SDK 要求 ROI 偏移、宽和高为偶数，宽高不小于 16。属性写入阶段只保存数值；`StartExposure()` 必须按照 ASCOM 要求检查 `BinX`、`BinY`、`StartX`、`StartY`、`NumX` 和 `NumY` 的最终组合，并在非法时返回指出具体字段的 `InvalidValueException`。不在属性写入时提前拒绝仅因其他属性尚未同步而暂时非法的组合。

binning 使用 `TOUPCAM_OPTION_BINNING` 的饱和求和模式，保持当前 RAW 位深和 `MaxADU` 不变。2048 × 2048 独立分辨率档不暴露为 ASCOM binning。

## 7. 位深、增益和读出模式

| 成员 | 实现 |
|---|---|
| `ReadoutModes` | `["RAW 12-bit", "RAW 8-bit"]` |
| `ReadoutMode` | 默认 0；只允许在 Idle 状态切换 |
| `CanFastReadout` | `False` |
| `FastReadout` GET/PUT | `PropertyNotImplementedException` |
| `MaxADU` | RAW12 为 4095；RAW8 为 255 |
| `Gain` | SDK 数值增益 100 至 1650 |
| `GainMin` / `GainMax` | 100 / 1650 |
| `Gains` | 数值增益模式下返回 `PropertyNotImplementedException` |
| `Offset` / `OffsetMin` / `OffsetMax` / `Offsets` | `PropertyNotImplementedException` |
| `ElectronsPerADU` | `PropertyNotImplementedException` |
| `FullWellCapacity` | `PropertyNotImplementedException` |
| `SubExposureDuration` GET/PUT | `PropertyNotImplementedException` |

设备没有 `TOUPCAM_FLAG_BLACKLEVEL`，实际读取 `TOUPCAM_OPTION_BLACKLEVEL` 返回 `E_NOTIMPL`，因此不能伪造 ASCOM Offset 支持。

电子增益和满阱容量缺乏可靠标定，因此不能返回猜测值。`MaxADU` 必须在 `ReadoutMode` 变化后立即反映新模式。

## 8. 曝光、状态与图像接口

| 成员 | 实现 |
|---|---|
| `ExposureMin` | 0.000012 s |
| `ExposureMax` | 3600 s |
| `ExposureResolution` | 0.000001 s |
| `StartExposure(Duration, Light)` | 配置后执行单次软件触发，异步返回 |
| `HasShutter` | `False` |
| `Light` | 无机械快门时按规范忽略 |
| `CanAbortExposure` | `True` |
| `AbortExposure` | 取消触发、丢弃帧并回到 Idle |
| `CanStopExposure` | `False` |
| `StopExposure` | Idle 时无操作；曝光中返回 `MethodNotImplementedException` |
| `CameraState` | Idle、Waiting、Exposing、Reading、Download、Error |
| `PercentCompleted` | 曝光期间返回 0 至 100；不适用状态返回 `InvalidOperationException` |
| `ImageReady` | 仅完整有效帧发布后为 True |
| `LastExposureDuration` | 返回实际应用的曝光秒数 |
| `LastExposureStartTime` | 实际曝光开始的 UTC FITS/ISO-8601 时间 |
| `CanPulseGuide` | `False` |
| `IsPulseGuiding` | `False` |
| `PulseGuide` | `MethodNotImplementedException` |

`StartExposure()` 将 Duration 转换为最近的整数微秒。曝光开始时 `ImageReady=False`。异步曝光失败不能生成图像；设备进入 Error 状态，并由后续状态查询返回可诊断的 `DriverException`。

`AbortExposure()` 使用 SDK 的触发取消机制。每次曝光具有递增 session ID；Abort、Disconnect 或下一次曝光会使旧 session 失效，迟到帧不得发布。

`StopExposure()` 不能伪装成 Abort。因为 SDK 不能保证提前停止仍返回有效科学图像，`CanStopExposure=False`。

## 9. ImageArray、ImageBytes 与 FITS

| 接口 | 实现 |
|---|---|
| `ImageArray` JSON | `Value[x][y]`，流式生成，不创建完整转置副本 |
| `ImageArray` ImageBytes | 接受 `Accept: application/imagebytes` 并返回标准二进制格式 |
| RAW12 图像元素 | `UInt16`，范围 0 至 4095 |
| RAW8 图像元素 | `Byte`，范围 0 至 255 |
| `ImageArrayVariant` | Alpaca 必须返回 `PropertyNotImplementedException` |
| 未有图像时读取 | `InvalidOperationException` |

标准 `ImageArray` 端点不能沿用 COSMOS66 的 `max_json_pixels` 拒绝策略。即使客户端不支持 ImageBytes，也必须能以流式 JSON 返回 4096 × 4096 全幅。

硬件测试必须确定 SDK 缓冲区的行方向和原点，并固定转换规则。ImageBytes、JSON、FITS、Web 像素坐标和统计必须从同一个不可变原始图像代次派生，且像素值完全一致。

Preview 帧使用独立槽位，不覆盖正式曝光图像，也不能成为标准 `ImageArray` 或 FITS 下载目标。

## 10. 制冷接口

| 成员 | 实现 |
|---|---|
| `CanSetCCDTemperature` | `True` |
| `CCDTemperature` | `get_Temperature() / 10` |
| `SetCCDTemperature` | -50.0°C 至 40.0°C，调用 `put_Temperature()` |
| `CoolerOn` GET/PUT | `TOUPCAM_OPTION_TEC` |
| `CanGetCoolerPower` | `True` |
| `CoolerPower` | `TEC_VOLTAGE / TEC_VOLTAGE_MAX × 100`，限制到 0 至 100 |
| `HeatSinkTemperature` | `PropertyNotImplementedException` |

温控属性只在已连接时访问硬件。SDK 不提供散热器温度，不能用传感器温度代替。

## 11. Alpaca 管理与协议

必须实现：

- `GET /management/apiversions`，返回 `[1]`；
- `GET /management/v1/description`；
- `GET /management/v1/configureddevices`，包含 Camera 0 和稳定 UID；
- `GET /setup`；
- `GET /setup/v1/camera/0/setup`；
- `/api/v1/camera/0/{command}` 的全部 ICameraV4 GET/PUT 成员；
- 默认 UDP 32227 的 discovery。

协议要求：

- URL 路径全部小写且区分大小写；
- GET 参数来自查询字符串，PUT 参数来自表单体；
- 参数名不区分大小写；
- 未识别的可选参数不能导致错误；
- 每个标准 JSON 响应包含 `ClientTransactionID`、`ServerTransactionID`、`ErrorNumber` 和 `ErrorMessage`；
- 合法设备调用即使返回 ASCOM 错误也使用 HTTP 200；
- 不可解析的路径、设备类型、设备号或参数使用 HTTP 400；
- 不可恢复的服务器内部错误使用 HTTP 500；
- JSON 和 ImageBytes 响应必须设置正确 `Content-Type`；
- discovery 响应必须使用键名 `AlpacaPort`；
- discovery socket 使用共享绑定，允许同机多个 Alpaca 设备；
- 支持 IPv4 广播；操作系统允许时支持 IPv6 链路本地组播 `ff12::a1:9aca`。

## 12. 错误映射

| 场景 | ASCOM 错误 |
|---|---|
| 标准成员未实现 | `0x400` |
| 参数越界或组合非法 | `0x401` |
| 值尚未产生 | `0x402`，或成员规范要求的 `0x40B` |
| 未连接访问硬件成员 | `0x407` |
| 当前状态不允许操作 | `0x40B` |
| 未支持的 Action | `0x40C` |
| 异步操作取消 | `0x40E` |
| SDK、USB、采集或内部故障 | `0x500` 至 `0xFFF` |

所有驱动错误必须包含操作、相机状态、SDK HRESULT 或事件、相关参数和可采取的下一步。日志使用 operation ID、client ID、client transaction ID 和 server transaction ID 关联请求与硬件动作。

## 13. 采集与并发模型

正式曝光流程：

1. 确认已连接且处于 Idle；
2. 校验 Duration、读出模式、binning 和 ROI；
3. 停止 Preview；
4. 在相机未运行时应用 RAW、位深、ROI、binning、增益和曝光时间；
5. 设置软件触发模式；
6. 启动后台采集任务并立即从 `StartExposure()` 返回；
7. 后台任务使用 `TriggerSyncV4` 等待单帧；
8. 成功后发布不可变原图并设置 `ImageReady=True`；
9. 失败、取消或断连时丢弃缓冲区并更新错误状态。

等待超时由实际曝光时间加固定传输安全余量计算，不能对长曝光使用固定短超时。

Preview 使用重复单帧软件触发。正式曝光和 Preview 互斥。

SDK 文档禁止在回调线程中调用 `put_Roi`、触发模式、位深、像素格式、binning 和旋转等配置。实现不得在 SDK 回调上下文执行任何配置。配置统一由协调器控制路径在采集停止后执行。

慢速 ImageBytes、JSON、FITS 或 Web 下载不阻塞相机状态查询。客户端断开下载连接只终止该次传输，不中止曝光。

## 14. Web 控制台和本机 GUI

Web 控制台延续 COSMOS66 的图像优先三栏工作台：

- 顶部显示真实型号、序列号、连接、RAW 位深、温度和 CameraState；
- 左侧提供连接、Preview、曝光、Abort、ROI、1 至 8 binning、增益和制冷；
- 中间为图像视口；
- 右侧为原始像素、统计、直方图、FWHM 和显示拉伸；
- 底部为可筛选、暂停和导出的日志。

品牌、文案、尺寸、位深、binning 和温控范围必须替换为 ToupTek 能力。显示拉伸不能修改科学数据。

Web 和 Alpaca 共用端口和同一个 `CameraCoordinator`。Web 只在受信任局域网使用；默认无 TLS 和账户认证，页面必须持续提示安全边界。

本机 GUI 只用于安装和硬件诊断，不能与 Alpaca 服务进程同时持有相机。

## 15. 测试布局与覆盖

所有测试内容位于：

```text
test/
├── fixtures/
├── unit/
├── alpaca/
├── web/
├── hardware/
├── packaging/
├── scripts/
└── results/
```

不得在项目根目录或产品源码包中放置测试脚本。

测试覆盖：

1. 相机契约、配置、状态机、事件和日志；
2. 假 SDK 的成功、错误、超时、断连和迟到帧；
3. Camera V4 全成员清单，确保每个 GET/PUT 端点均有明确行为；
4. 错误号、事务号、HTTP 状态、Content-Type 和参数大小写；
5. RAW8/RAW12、MaxADU、ImageBytes 类型和 JSON/FITS 像素一致性；
6. 1×1 至 8×8 binning、ROI 换算、偶数限制和非法组合；
7. CameraState、PercentCompleted、ImageReady 和异步失败；
8. Preview、曝光、Abort、Disconnect 和并发客户端；
9. 4096 × 4096 ImageBytes、流式 JSON、FITS 和 Web 图块的内存压力；
10. Web API、SSE、图像代次、统计、FWHM 和日志；
11. 模拟器完整往返；
12. 打包后的 EXE 模拟器和真机烟雾测试。

真实硬件测试覆盖枚举、稳定序列号、RAW8/12、最短曝光、全幅、ROI、全部 binning、增益、温控、Abort、连续曝光和重连。测试开始时保存设备设置，结束时恢复原始设置并关闭句柄。

最终验收必须运行 ASCOM 官方 ConformU 的 Alpaca Protocol 和 Camera V4 检查，并把结果保存到 `test/results/conformu/`。还需用 N.I.N.A. 验证 discovery、连接、曝光、Abort、ROI/binning、制冷和图像下载。

## 16. 打包与交付

当前 `.venv` 是 32-bit，不能加载项目的 64-bit `toupcam.dll`。开发、测试和打包使用新的 64-bit Python 环境。

交付目录包含：

- `ToupTek4040Alpaca.exe`；
- `ToupTek4040GUI.exe`；
- 64-bit `toupcam.dll`；
- 默认 INI 配置；
- Web 静态资源；
- 中文使用说明；
- SDK、固件、驱动和 ASCOM 版本说明。

程序默认使用真实相机。模拟器只能通过明确配置或命令行参数启用。发布前必须完成打包后模拟器测试、真机测试和 ConformU 验证。

## 17. 完成标准

项目只有在以下条件全部满足时才视为完成：

- Camera V4 API 清单全部具有规范一致的实现或规范允许的 NotImplemented 行为；
- 所有自动测试通过；
- 真实硬件矩阵通过；
- ConformU Alpaca Protocol 与 Camera V4 检查通过，或所有剩余警告均有书面、规范支持的解释；
- 4096 × 4096 RAW12 ImageBytes、JSON 和 FITS 像素一致；
- N.I.N.A. 能发现、连接并完成完整曝光流程；
- Windows 64-bit 交付包可在无开发环境的目标机启动；
- 真机失败不会自动回退模拟器；
- 所有测试内容均位于 `test/`。
