# Pythub

**Pythub** 是一个开源的基于 Python 的 Python 应用打包工具：
**一条命令，把应用打包成免装 Python、可直接运行的产物**——在一个平台上即可交叉打包 Windows / macOS / Linux 多平台、多版本 Python、多架构的自包含产物，并支持打包/发布 PyPI 包。

## 核心亮点

> 这些能力共同构成 Pythub 相对其他打包工具的**绝对性优势**（详见下方对比说明）：

1. **pexe：自研单文件格式全平台化**：Windows `.exe`（PE）、macOS `.pexe`（Mach-O）、
   Linux `.pexe`（ELF，musl 静态链接）同属 Pythub **自研的 pexe 单文件格式**（自研
   C 引导器 + 自研 PythubEX 归档，格式 magic `PYTHUBEX`）——**每个平台都有原生单文件可执行**，而非只有
   Windows 有 exe、其他平台退回脚本的常见做法；
2. **自研引导器**：一套 C 源码编三平台（本机 cc 快速路径 / zig cc 交叉），自带
   双层 MD5 完整性校验、原子解压、并发互斥、缓存 LRU——不是 sh 头自解压脚本的
   弱实现，解释器与应用的装载全程可控、可演进；
3. **全平台交叉编译**：借助 zig cc 自带的全平台工具链与 libc 桩（Windows 亦可 mingw），
   **任意主机 ⇄ 任意目标平台互编**，跨架构同样支持——CI 上一个 runner 即可发布三平台
   全部产物，无需为每个平台搭构建环境；
4. **mem 内存执行 · 源码保护**：开启 mem 模式（exe/pexe 通用开关），应用源码以字节码
   内嵌进单文件，运行时经内存管道直传解释器、**全程内存执行**——目标磁盘上没有任何
   `.py` / `.pyc`，杜绝源码被翻查与缓存被篡改；关闭则为常规磁盘执行。一个开关，
   产物后缀随之切换（`.exe` ⇄ `.mem.exe`、`.pexe` ⇄ `.mem.pexe`）。
   保护边界说明：字节码仅经 zlib 压缩内嵌、**非加密**，属「防翻查」级保护——
   Python 3.11+ 目前缺少成熟的反编译工具，可显著提高还原源码的门槛，但运行时内存
   仍可被调试器读取；需要编译级（更强）保护请配合 Nuitka 等方案；
5. **自研引擎 · 多引擎兼容**：默认 **pythub 引擎**（自研、纯标准库实现、天然支持
   交叉；旧名 `zipapp` 保留为永久别名）——引擎以产品署名，不用匿名技术名词；
   同时接入三方引擎 PyInstaller / cx_Freeze / Nuitka / setuptools /
   py2exe（Windows）/ py2app（macOS）——一个工具既是全新引擎，也不放弃任何存量习惯。

## 特性

- **全平台交叉编译**：在 Mac 上打包 Windows `.exe` / `.exe.zip`、Linux `tar.gz`/`.run`/AppImage/**`.pexe`**；在 Windows 上打包 macOS `tar.gz` 等。真单文件二进制（exe/pexe）经 mingw / zig cc 任意互编，跨架构同支持
- **多 Python 版本**：3.10 / 3.11 / 3.12（产物内嵌目标版本解释器，目标机器**免装 Python**）
- **运行时自动瘦身**：官方 stripped 运行时 + 剪除 test/idlelib/pip/调试符号等冗余，Windows `.exe.zip` 体积降低约 76%（55M→13M）
- **多架构**：x86_64 / aarch64（含 Apple Silicon、Windows ARM64）
- **平台专属格式**：`.app` / `.dmg` / `.pkg`（macOS）、AppImage / `tar.gz` / 单文件自解压 `.run` / 原生单文件 `.pexe`（Linux）、`.exe.zip` / 单文件 `.exe`（Windows，自研 C 引导器 + 自研压缩归档 + stdlib zip 化，实测 12.3MB）、单文件 `pyz`；macOS/Linux 的 `.pexe` 为原生单文件可执行（同一套自研 C 引导器与归档格式，安装 zig 后支持全平台交叉生成）；`mem` 是 exe/pexe 的**内存执行模式开关**（`mem = true` / `--mem`：源码字节码内嵌、全程内存执行不落盘（字节码压缩非加密，属防翻查级保护），产物后缀 `.mem.exe`/`.mem.pexe`；关闭则为磁盘执行）
- **智能组件选择**：自动分析 import，按目标平台下载对应平台的第三方组件（Mac 打包只带 Mac 组件，Windows 打包只带 Windows 组件）
- **多打包引擎**：默认 `pythub` 引擎（自研、支持交叉；旧名 `zipapp` 兼容）；另接入本机三方引擎 PyInstaller / cx_Freeze / Nuitka / setuptools / py2exe（Windows）/ py2app（macOS）
- **命令行 + GUI**：`pythub`（argparse）与 `pythub-gui`（tkinter），组件全部为 Python 内置或自研实现
- **PyPI 生态**：`pythub pypa build` 打 sdist/wheel，`pythub pypa publish` 直接发布到 PyPI（stdlib 实现）；Pythub 自身亦可发布到 PyPI（`pip install crogram-pythub`）

## 安装

```bash
# 克隆源码
git clone https://github.com/crogram/pythub.git
cd pythub

# 生产模式安装，本地仓库
pip install .

# 开发模式安装（修改代码立即生效）
pip install -e .

# 发布后:
pip install crogram-pythub
```

## 快速开始

```bash
# 1. 创建示例工程
pythub init

# 2. 分析依赖
pythub analyze hello_app

# 3. 交叉打包：在任意平台打包所有平台产物（默认 Python 3.12）
pythub build hello_app -p all

# 指定平台/格式/Python 版本/架构
pythub build hello_app -p windows -f exe.zip --py 3.11
pythub build hello_app -p macos -f app --arch aarch64
pythub build hello_app -p linux -f appimage
pythub build hello_app -p linux -f run

# 产物平铺在 dist/ 下（无第一级版本目录），
# 应用版本/Python 版本/平台/架构差异直接体现在文件名上:
#   dist/hello_app-0.1.0-3.12-windows-x86_64.exe
#   dist/hello_app-0.1.0-3.11-macos-aarch64.app
#   dist/hello_app-0.1.0-3.12-linux-x86_64.run

# 本机三方引擎打包（真二进制，需安装对应工具）
pythub build hello_app -p macos -f onefile --engine pyinstaller
pythub build hello_app -p macos -f app --engine py2app
pythub build hello_app -p macos -f onefile --engine nuitka
pythub build hello_app -p macos -f wheel --engine setuptools
pythub build hello_app -p windows -f dir --engine py2exe      # 仅 Windows 机器
pythub build hello_app -p linux -f dir --engine cx_freeze

# 4. 可视化界面（默认后台运行不占控制台，关闭终端不影响 GUI）
pythub gui
pythub gui --debug           # 前台运行，日志输出到当前终端（调试用）

PYTHONPATH=src python3.11 -m pythub gui
```

## 打包为 PyPI 包并发布

```bash
pythub pypa build hello_app -o dist          # 生成 sdist + wheel
pythub pypa publish dist/*.whl --repo pypi --token pypi-xxxx
```

GUI 的「发布 PyPI」页同样可选择 dist 中的文件直接上传。

## 产物格式矩阵

| 目标平台 | 格式 | 默认 | 交叉支持 |
|---|---|---|---|
| Windows | `exe.zip` / `exe` / `dir` / `pyz` | exe | ✅（`.exe` 单文件需构建机有 mingw-w64 或 zig，自研 C 引导器交叉编译） |
| Linux | `tar.gz` / `pexe` / `run` / `appimage` / `dir` / `pyz` | pexe | ✅（`pexe` 原生 ELF 单文件：本机平台用 cc，跨平台用 zig cc（musl 静态链接）；`.run` 零依赖可交叉；AppImage 需 appimagetool，缺失时降级 `.run`） |
| macOS | `app` / `dmg` / `pkg` / `tar.gz` / `pexe` / `dir` / `pyz` | app | ✅（`pexe` 原生 Mach-O 单文件：本机用 cc，跨平台用 zig cc；`.app`/`.dmg`/`.pkg` 仅 macOS；其余可交叉） |


说明：Python 生态中真正的"交叉编译二进制"受解释器限制。Pythub 的 pythub 引擎采用
**自包含产物**方案（内嵌 python-build-standalone 独立解释器 + zipapp + 平台启动器），
目标机器解压即可运行、无需安装 Python；需要真正单文件二进制时，在本机平台使用三方引擎。

## mem 模式：exe/pexe 单文件的内存执行开关

**mem 不是独立输出格式**，而是自研单文件二进制（Windows `exe`、macOS/Linux `pexe`，
同一套自研 C 引导器与自研归档）之上的**执行模式开关**——`.mem` 只是加在原产物
后缀之上的标识（`appname.exe` → `appname.mem.exe`、`appname.pexe` →
`appname.mem.pexe`），用于区分两种执行方式：

- **开启 mem（内存执行）**：应用源码以字节码内嵌为内存载荷条目，引导器解压时
  跳过（永不写盘）；启动时引导器源码经环境变量注入解释器、应用字节码经管道直传，
  在内存中 marshal 反序列化并执行——全程内存、不产生任何临时 `.py`/`.pyc` 文件；
- **关闭 mem（磁盘执行，默认）**：源码 zip 解压到缓存目录后运行，产物后缀
  `appname.exe` / `appname.pexe`；
- **二进制内嵌**：嵌入式 Python 运行时（免装 Python）+ 自研引导器 + 应用载荷；
- **运行时**：引导器把运行时解压到缓存目录（仅首次，解释器二进制必须落盘才能
  exec，这是操作系统限制）；
- **配置**：pythub.toml `[app] mem = true` / CLI `--mem` / GUI 复选框，三者等效
  （仅 exe/pexe 格式生效，需自研打包引擎；本机平台 cc 快速编译、跨平台 zig cc）；
- **保护边界**：字节码仅 zlib 压缩内嵌、非加密，属「防翻查」级保护——3.11+ 缺少
  成熟反编译工具、还原门槛高，但归档可被提取、运行时内存可被调试器读取，并非
  加密级防护；需要更强保护请配合 Nuitka 等编译方案；
- **限制**：仅纯 Python 模块（`.so`/`.pyd` 会报错提示改用其他格式）；字节码与
  CPython 版本强绑定（内嵌运行时同版本构建，无此问题）；
- **内存资源**：非代码文件（数据文件等）一并打包为内存资源条目，运行期经
  `pythub.memres` 访问（`mem_data` / `mem_data_names` / `materialize`，全程内存，
  需要真实文件时可一次性物化到缓存）；
- **自打包**：pythub 自身可用 mem 模式打包成单文件二进制（`pythub build src/pythub
  --entry __main__.py --fmt pexe --mem`），打包出的工具可继续打包任意应用（引导器
  C 源码随内存资源内嵌，构建期自动物化编译）；
- **二次启动秒开**：运行时缓存按应用分组 LRU 管理，同应用旧版本自动清理。

```bash
pythub build --platform macos --arch x86_64 --python 3.11 --fmt pexe --mem ./hello_app
# 产物: dist/hello_app-0.1.0-3.11-macos-x86_64.mem.pexe  （直接运行，免装 Python）
```

## 打包引擎

| 引擎 | 平台 | 支持格式 | 依赖 |
|---|---|---|---|
| `pythub`（默认） | 任意→任意（交叉） | 见上表 | 无（自研） |
| `pyinstaller` | 本机 | onefile / onedir / app / dir | `pip install pyinstaller` |
| `cx_freeze` | 本机 | onedir / dir | `pip install cx_Freeze` |
| `nuitka` | 本机 | onefile / onedir / dir / app | `pip install nuitka` |
| `setuptools` | 本机 | wheel | 无（pip） |
| `py2exe` | 仅 Windows | onedir / dir | `pip install py2exe` |
| `py2app` | 仅 macOS | app / dir | `pip install py2app` |

三方引擎在本机平台运行（产物内嵌本机 Python），不支持交叉；跨平台请用默认 pythub 引擎。

引擎可在 `pythub.toml` 中传参（借鉴旧版 Pythub Builder 的界面选项）：

```toml
[pyinstaller]
icon = "app.ico"            # 图标（.ico/.icns/.png），自动映射各引擎参数
version_file = "ver.rc"     # Windows VERSIONINFO 版本信息文件
upx = false                 # false 禁用 UPX 压缩（默认启用，需已安装 UPX）
extra_args = ["--add-data", "data:.", "--hidden-import", "requests"]  # 透传底层工具

[nuitka]
icon = "app.png"

[cx_freeze]
icon = "app.ico"

[py2app]
icon = "app.icns"
```

## 工程配置 `pythub.toml`

```toml
[app]
name = "hello-pythub"
entry = "main.py"
version = "0.1.0"
description = "示例应用"
author = "CROGRAM"
requirements = ["requests"]   # 第三方依赖；也可用 requirements.txt
windowed = false              # true: GUI 应用隐藏控制台黑框（exe 引导器 -mwindows；app/.app 同样生效）
mem = false                  # mem 内存执行模式（exe/pexe）: true=源码字节码内嵌、
                             # 全程不落盘（后缀 .mem.exe/.mem.pexe）
chmod = false                # true: 自动为 run/pexe/pyz 等可执行产物添加执行权限
                             # （等效 CLI --chmod）
app_wrap = false             # true: mac 平台把 pexe 再次打包成 .app 壳（产物
                             # {name}-pexe.app / mem 变体 {name}-mem.app，Finder
                             # 直接识别为应用、双击运行，适合 GUI；原 .pexe 产物
                             # 保留，等效 CLI --app-wrap）
```

## 项目结构

```
src/pythub/
├── cli.py          # 命令行入口
├── gui.py          # tkinter 可视化界面
├── config.py       # pythub.toml / requirements 解析
├── analyzer.py     # AST 依赖分析（标准库/第三方/发行包映射）
├── deps.py         # 按目标平台下载 wheel（pip 交叉下载）
├── runtime.py      # 嵌入式 Python 运行时下载与缓存
├── builder.py      # 打包主流程（pythub 引擎）
├── formats.py      # .app/.dmg/.pkg/AppImage/.exe.zip/.run/.exe 格式实现
├── exe_archive.py  # 自研 Windows 单文件 .exe 打包器（zlib 压缩归档 + stdlib zip 化 + 引导器交叉编译缓存）
├── bootloader/
│   └── bootex.c    # 自研单文件引导器源码（Windows PE / POSIX 双分支）
├── engines.py      # 多打包引擎适配层（PyInstaller/cx_Freeze/Nuitka/setuptools/py2exe/py2app）
├── native_builder.py  # 兼容层（旧 PyInstaller 引擎接口，转调 engines）
└── pypa.py         # sdist/wheel 构建 + PyPI 上传
examples/hello_app/ # 示例工程（测试与验证用）
docs/               # 原始需求与需求梳理清单
```

## 常见问题

**Q：打包时报错 "PyInstaller 未找到"**

✅ 解决方案：

```bash
pip install --upgrade pyinstaller
```

其他三方引擎同理（`cx_Freeze` / `nuitka` / `py2exe` / `py2app`），或改用默认 `pythub` 引擎（无需额外依赖）。

**Q：如何添加自定义图标？**

✅ 操作步骤：

1. 准备图标文件：`.ico` (Windows) 或 `.icns` (macOS)
2. 在 `pythub.toml` 对应引擎段配置：

```toml
[pyinstaller]
icon = "app.ico"

[py2app]
icon = "app.icns"
```

**Q：版本信息文件格式要求**

🔍 参考：[微软版本资源文件规范（VERSIONINFO resource）](https://learn.microsoft.com/zh-cn/windows/win32/menurc/versioninfo-resource)，配置示例：

```toml
[pyinstaller]
version_file = "ver.rc"
```

## 问题反馈

Please file an issue at [Issues](https://github.com/Crogram/Pythub/issues).

## 许可证

Pythub is released under the [BSD 3-Clause License](LICENSE)

## 作者

[Jackson Dou](https://github.com/jksdou)

## 版权

© 2025 [Crogram](https://crogram.org/)

## 其他

网站：[pythub.org](https://pythub.org/)

