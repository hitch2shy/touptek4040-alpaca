# ToupTek 4040 Alpaca Camera

面向 ToupTek `sMAX16AM-U3-CL-GPS`（4096 × 4096）的 ASCOM Alpaca 相机服务、网页控制台及本地诊断界面。

项目基于 ToupTek SDK `toupcamsdk.20250623`，实现 ASCOM Camera V4 接口。真实相机由独立受监督进程管理，网页和 Alpaca 客户端共享同一套采集状态与图像数据。

## 功能

- 曝光、Abort、ROI、Bin、RAW 12-bit / RAW 8-bit、增益与制冷控制。
- 网页预览、直方图、统计、原始像素 ADU 检查与 FITS 下载。
- 预览放大、平移、框选放大及限定在图像区域的键盘操作；视图操作不修改相机 ROI。
- Alpaca JSON ImageArray 和 ImageBytes。
- 网页防露加热控制；此功能独立于标准 Camera V4 接口。
- 显式模拟器模式，用于没有相机时的界面和接口测试。

## 当前验证状态

当前上传包含科学图像视图缩放功能。该功能在 2026-08-06 上传准备前的验证记录为：软件测试 1492 项通过，36 项硬件测试跳过；冻结包测试 21 项通过。

**本轮缩放功能的正式包真实相机验证尚未完成。** 先前相机功能的硬件记录保存在 `test/results/`，应按各自的日期和源码提交理解，不能视为本轮功能已通过硬件验收。

## 从源码运行

要求 64 位 Windows 10/11、Python 3.13 或更新版本。以下命令在仓库根目录的 PowerShell 中执行：

```powershell
py -3.13 -m venv .venv64
.\.venv64\Scripts\python.exe -m pip install -e .
```

先用模拟器启动网页服务：

```powershell
.\.venv64\Scripts\python.exe run_alpaca.py --source sim --host 127.0.0.1
```

浏览器打开 <http://127.0.0.1:11111/ui/>。

连接真实相机前：

1. 安装 ToupTek 官方 Windows 驱动。
2. 自行准备 SDK `toupcamsdk.20250623`，将整个 SDK 目录放在仓库根目录。加载器需要其中的 `python/toupcam.py` 和 `win/x64/toupcam.dll`。
3. 在 `touptek4040.json` 中，将 `camera.serial` 改为自己的相机序列号；提交的配置是原开发设备的配置。
4. 运行：

```powershell
.\.venv64\Scripts\python.exe run_alpaca.py
```

本地独立诊断界面入口为：

```powershell
.\.venv64\Scripts\python.exe run_gui.py
```

同一真实相机只能由一个进程持有；使用独立诊断界面前，应先关闭占用相机的服务或其他相机软件。

## 配置与使用说明

- [完整使用说明](使用说明.txt)
- [JSON 配置](touptek4040.json)
- [官方 SDK 输入校验记录](official-touptek-sdk.lock.json)
- [设计与实施文档](docs/superpowers/)

默认 HTTP 端口为 `11111`，Alpaca 发现端口为 UDP `32227`。仅在可信局域网中使用；服务没有公网身份认证和 TLS，不应直接暴露到互联网。

## 测试与构建

测试文件与测试产物统一放在 `test/`。基础源代码测试：

```powershell
.\.venv64\Scripts\python.exe -m pip install pytest
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/unit test/alpaca test/web test/smoke -q
```

部分测试需要 Node.js、外部 SDK、已构建的分发包或实际硬件，请根据对应测试说明准备。全套测试入口为 `test/scripts/run_all.ps1`；不要在相机被其他软件使用时执行硬件验证脚本。

Windows 分发包构建：

```powershell
.\.venv64\Scripts\python.exe -m pip install pyinstaller
powershell -ExecutionPolicy Bypass -File test/scripts/build_release.ps1 -SdkRoot .\toupcamsdk.20250623
```

构建脚本要求发布输入已提交，并核对 SDK 文件与锁定记录。输出位于 `dist/ToupTek4040Control/` 和 `dist/ToupTek4040Control.zip`。

## 仓库范围

仓库包含项目源码、测试、图标和文档，不包含 ToupTek SDK 安装目录、虚拟环境或已打包的可执行程序。ToupTek SDK、驱动及其他第三方组件适用各自的许可；公开此仓库不改变这些组件的许可。
