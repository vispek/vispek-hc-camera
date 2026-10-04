# vispek-hc

**Vispek HC1500** 多光谱相机的 SDK 与命令行工具：一台 USB 相机加 17 个可切换的 LED
波段（255 nm 到 1050 nm）。一次采集是依次拍下的 17 帧，每帧只亮一盏灯。

[English](README.md)

> [!WARNING]
> **紫外危险。** 1、2 号灯（255 nm、275 nm）发出 **UV-C**，会伤害眼睛和皮肤；3、4 号灯
> （370 nm、400 nm）为 UV-A / 紫光。不要直视光源；请在暗箱内或遮挡后操作。除非显式传入
> `--allow-uv` / `allow_uv=True`，本软件不会点亮 1–4 号灯。紫外灯由第二根线（USB2）
> 供电。程序被强制结束时灯不会灭：请把设备的**每一根线**（包括相机线）都拔掉确认。接入硬件前先读
> [SAFETY.md](SAFETY.md)。

## 状态

0.1 版，pre-alpha。**目前只在一台真机上检查过**：串口协议、取帧、参数锁定、黑电平、
时序、LED 5-17 的完整扫描、用白纸做的白板参考，以及含紫外灯在内的 17 通道扫描（见
[docs/acceptance/](docs/acceptance/README.md)）。真正的反射率标准板尚未试过。下面的内容今天都能在随包提供的
仿真设备上运行（`--simulate`）。相机参数锁定支持 macOS（已真机验证）和 Linux（已实现、未验证），Windows 尚不支持，见
[docs/platforms.md](docs/platforms.md)。

## 安装

需要 Python 3.11 及以上；读相机需要 `PATH` 里有 `ffmpeg`。

```bash
python3 -m venv hc-env && source hc-env/bin/activate
pip install vispek-hc
vispek-hc setup                      # macOS：编译相机控制工具，只需一次
vispek-hc check --simulate           # 在仿真设备上跑完整个栈
```

只要命令行的话，也可以用 `uv tool install vispek-hc` 或 `pipx install vispek-hc`。在当前的
Linux 和 macOS 上，直接对系统 Python 执行 `pip install` 会被拒绝（PEP 668）。要改代码，
克隆本仓库后运行 `uv sync`。

## 不接硬件的第一张图

```bash
uv run vispek-hc demo -o demo
```

它在仿真设备上扫描一个虚构的场景，采白参考，计算相对反射，把 `rgb.png`、`cir.png`、
`ndvi.png` 和 `spectra.csv` 写进 `demo/`，并把结果和它仿真的场景比对（`PASS`）。同样的
事，分步来做：

```bash
uv run vispek-hc record -o sample --simulate              # 暗帧 + 5–17 号灯
uv run vispek-hc reference white -o white --simulate      # 白板，参数相同
uv run vispek-hc calibrate --white white -o calibration
uv run vispek-hc reflectance sample --calib calibration -o cube.npz
uv run vispek-hc view sample --calib calibration --mode ndvi -o ndvi.png
```

软件不随附真实的样例图片。

## 接上设备

```bash
uv run vispek-hc check               # 缺什么，每个问题带一条处理提示
uv run vispek-hc record -o first     # 约 10 秒；macOS 首次会请求相机权限
uv run vispek-hc view first -o first.png
```

环境不缺东西时，`check` 还会点亮 6 号灯（520 nm，可见光）0.3 秒，确认串口和
相机属于同一台设备；`devices --pair` 可单独做这项探测。三个系统上都会自动找到相机；用 `--camera` 可指定另一台。任何命令加 `--json`，标准输出就只有一个机器可读的对象
（[docs/cli.md](docs/cli.md)）。

| | 最短路径 |
|---|---|
| **macOS** | `brew install ffmpeg`，再运行 `vispek-hc setup`（在本机编译相机控制工具，需要 `xcode-select --install`）以锁定相机参数；`check`；`record`。没有构建该工具时用 `record --unlocked` |
| **Linux** | `apt install ffmpeg v4l-utils`；`sudo usermod -aG dialout $USER` 后重新登录；`check`；`record -o first`（已实现，尚未真机验证） |
| **Windows** | `winget install ffmpeg`；安装 CH341 串口驱动；允许桌面应用访问相机；`check`；`record -o first --unlocked`（参数锁定尚未实现） |

细节以及 `--unlocked` 的代价：[docs/platforms.md](docs/platforms.md)。

## 数据长什么样

一次采集是一个文件夹：无损帧加 `capture.json`。一个 cube 里有原始帧，以及每个像素、
每个波段一个数值：

```python
import json, numpy as np

z = np.load("cube.npz", allow_pickle=False)  # 由 'vispek-hc reflectance' 写出
values = z["values"].astype(np.float32)  # (H, W, B)，轴序 Y, X, B
valid = np.isfinite(values) & ((z["mask"] & 7) == 0)
rgb = z["rgb"]  # (H, W, B, 3) uint8 原始码值
band_ids, nm = z["band_ids"].tolist(), z["wavelengths_nm"]
meta = json.loads(str(z["metadata_json"]))
```

或者直接用 Python：

```python
import vispek_hc

capture = vispek_hc.capture("first-scan", simulate=True)  # 接真机时去掉 simulate
cube = capture.cube()  # .values (H, W, 波段数)、.rgb、.mask、.bands
```

**这些数值是什么。** 像素值是经过相机内部图像处理的 8 位码值，不是线性辐射量；相对反射
是一个指数，不是物理反射率；波段波长是 LED 的名义中心波长，不是传感器响应。比较两次
扫描之前请先读 [docs/calibration.md](docs/calibration.md)。

## 查看器

[`vispek-hc-viewer`](https://github.com/vispek/vispek-hc-viewer) 是建立在本包之上的另一个包：
在本机浏览器里打开的一个页面，有实时画面、LED 开关、扫描和白板参考按钮、单波段图、指数图
和光谱。它记录的就是普通的采集文件夹，命令行和查看器可以互相读取对方的数据。
`pip install vispek-hc-viewer`。

## 文档

- [AGENTS.md](AGENTS.md)：给编码 agent 的指南，也是给人看的最快概览
- [docs/cli.md](docs/cli.md)、[docs/api.md](docs/api.md)：命令与 Python 接口
- [docs/data-format.md](docs/data-format.md)、[docs/calibration.md](docs/calibration.md)：
  文件格式与数值含义
- [docs/integration-guide.md](docs/integration-guide.md)：**不用本软件包、用任意语言自行开发控制软件**的
  设备指南：接线、串口协议与参考代码、相机设置、会让数据出错的五个设备行为、如何采集与校正。
  自成一体，可直接交给你的编码 agent
- [docs/hardware.md](docs/hardware.md)、[docs/protocol.md](docs/protocol.md)：设备本身
- [docs/platforms.md](docs/platforms.md)、[docs/troubleshooting.md](docs/troubleshooting.md)
- [examples/](examples/)：13 个可离线运行的短脚本

文档正文为英文。

## 许可

Apache-2.0，见 [LICENSE](LICENSE) 与 [NOTICE](NOTICE)。商标见 [TRADEMARKS.md](TRADEMARKS.md)。
