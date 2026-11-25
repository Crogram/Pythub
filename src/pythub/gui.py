"""Pythub 可视化操作界面（tkinter，零第三方 UI 依赖）。

- 选择打包的 Python 脚本/项目、目标平台、架构、Python 版本
- 按平台列出可用文件类型（单文件/文件夹/平台专属格式）
- 执行打包（后台线程，界面不卡顿）
- 打包成功弹窗提示产物路径
- 发布打包好的应用/发行包到 PyPI
- 工具管理（需求 37）: zig 安装/卸载/设默认编译器、Python 运行时下载/移除/设默认版本、
  打包工具链状态检测；所有操作写入独立日志 logs/tools-YYYYMMDD.log
- 日志独立弹窗查看（需求 40/41）: 打包日志（实时队列）与工具日志（文件）均为
  独立弹窗、自动刷新，主窗口不再放置日志区
"""

from __future__ import annotations

import logging
import os
import queue
import re
import subprocess
import sys
import threading
import webbrowser
from collections import deque
from pathlib import Path
from typing import Optional

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    _TK_OK = True
except ImportError as e:
    tk = None  # type: ignore[assignment]
    _TK_OK = False
    _TK_ERROR = str(e)

from pythub import DEFAULT_FORMAT, PLATFORM_FORMATS, SUPPORTED_ARCHS, SUPPORTED_PYTHON
from pythub.engines import ENGINES as _ENGINES, ENGINE_NAMES as _ENGINE_NAMES
from pythub.utils import PYTHUB_CACHE_DIR


class _QueueHandler(logging.Handler):
    """logging -> 线程安全队列 -> GUI 日志区。"""

    def __init__(self, q: "queue.Queue"):
        super().__init__()
        self.q = q

    def emit(self, record):
        try:
            self.q.put((self.format(record) + "\n", record.levelname))
        except Exception:
            self.handleError(record)


# ------------------------------------------------ 日志弹窗（需求 40/41）


def _center_window(win, parent=None, width=None, height=None):
    """窗口打开前自动居中（先隐藏、一次性定位、再显示，避免左上角闪烁）。

    先 withdraw 阻止窗口以默认位置映射，计算居中坐标后一次性写入完整
    "WxH+X+Y"，最后 deiconify——窗口首次显示即在居中位置。width/height
    未给时取窗口请求尺寸（winfo_reqwidth/reqheight，未映射前也可用；
    不能用 win.geometry()——withdraw 后返回 1x1）。parent 给定时相对
    父窗口居中（弹窗跟随主窗口），否则相对屏幕居中（主窗口）。
    """
    win.withdraw()
    win.update_idletasks()
    if width is None:
        width = win.winfo_reqwidth()
    if height is None:
        height = win.winfo_reqheight()
    if parent is not None and parent.winfo_exists() and parent.winfo_ismapped():
        parent.update_idletasks()
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        pw, ph = parent.winfo_width(), parent.winfo_height()
    else:
        px, py = 0, 0
        pw, ph = win.winfo_screenwidth(), win.winfo_screenheight()
    x = px + max((pw - width) // 2, 0)
    y = py + max((ph - height) // 2, 0)
    win.geometry(f"{width}x{height}+{x}+{y}")
    win.deiconify()


class _LogWindow:
    """独立日志查看弹窗: 暗色只读文本区 + 自动刷新。

    - 打包日志（queue 模式）: 由 PythubGUI._poll 增量推送日志行（append_lines）
    - 工具日志（file 模式）: 按日志文件路径定期重读（tools-YYYYMMDD.log）
    """

    COLORS = (("WARNING", "#c08000"), ("ERROR", "#e05555"), ("INFO", "#dcdcdc"))
    MAX_LINES = 8000

    def __init__(self, root: "tk.Tk", title: str, header: str,
                 file_path: Optional[Path] = None, reload_ms: int = 1500):
        self.win = tk.Toplevel(root)
        self.win.title(title)
        self.win.geometry("760x480")
        _center_window(self.win, root, 760, 480)
        self.file_path = file_path
        self.reload_ms = reload_ms
        self.bar = ttk.Frame(self.win)
        self.bar.pack(fill="x", padx=6, pady=4)
        ttk.Label(self.bar, text=header, foreground="#777").pack(side="left")
        self.txt = tk.Text(self.win, bg="#141414", fg="#dcdcdc",
                           state="disabled", wrap="none")
        self.txt.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        for tag, color in self.COLORS:
            self.txt.tag_config(tag, foreground=color)
        if file_path is not None:
            self.reload()
            self._schedule_reload()

    def append_lines(self, lines) -> None:
        """外部增量追加日志行（queue 模式），超出上限后丢弃最旧行。"""
        self.txt.configure(state="normal")
        for text, tag in lines:
            self.txt.insert("end", text, tag)
        total = int(self.txt.index("end-1c").split(".")[0])
        if total > self.MAX_LINES:
            self.txt.delete("1.0", f"{total - self.MAX_LINES}.0")
        self.txt.configure(state="disabled")
        self.txt.see("end")

    def reload(self) -> None:
        """file 模式: 重读日志文件（按级别着色）。"""
        if self.file_path is None:
            return
        try:
            content = self.file_path.read_text(encoding="utf-8") \
                if self.file_path.exists() else "（暂无日志 —— 今天还没有相关操作）"
        except OSError as e:
            content = f"（日志读取失败: {e}）"
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        for line in content.splitlines(keepends=True):
            lvl = "INFO"
            if "[WARNING]" in line or "[WARN]" in line:
                lvl = "WARNING"
            elif "[ERROR]" in line:
                lvl = "ERROR"
            self.txt.insert("end", line if line.endswith("\n") else line + "\n", lvl)
        self.txt.configure(state="disabled")
        self.txt.see("end")

    def _schedule_reload(self) -> None:
        """file 模式自动刷新: 周期性重读（定时器随窗口销毁自动取消）。"""
        def tick():
            self.reload()
            self.win.after(self.reload_ms, tick)
        self.win.after(self.reload_ms, tick)


class _PlaceholderEntry(ttk.Entry):
    """带 placeholder 提示文案的输入框（需求 43，tk/ttk 无原生支持）。

    - 空内容且未聚焦时灰字显示提示文案
    - 聚焦即清除提示、恢复正文颜色；失焦时若仍为空则重新显示
    - 程序写入真实值（如选中项目后回填）自动清除提示状态
    - 用 real_value() 取值: 显示占位文案时返回空串，避免把提示当成输入
    """

    def __init__(self, master, placeholder: str,
                 textvariable: "tk.StringVar | None" = None, **kw):
        super().__init__(master, textvariable=textvariable, **kw)
        self._placeholder = placeholder
        self._var = textvariable
        self._showing = False
        if self._var is not None:
            self._var.trace_add("write", self._on_var_write)
        self.bind("<FocusIn>", self._on_focus_in, add="+")
        self.bind("<FocusOut>", self._on_focus_out, add="+")
        self._refresh()

    def set_placeholder(self, placeholder: str) -> None:
        """外部更新占位文案（显示中的占位文案随之刷新；有真实值时不打扰）。"""
        self._placeholder = placeholder
        if self._showing:
            if self._var is not None:
                self._var.set(self._placeholder)
            else:
                self.delete(0, "end")
                self.insert(0, self._placeholder)

    # ---- 状态维护 ----

    def _refresh(self):
        val = self._var.get() if self._var is not None else self.get()
        if val:
            if self._showing:  # 外部写入了真实值
                self._showing = False
                self.configure(foreground="#000000")
            return
        if not self._showing and not self._has_focus():
            self._set_placeholder()

    def _set_placeholder(self):
        self._showing = True
        self.configure(foreground="#999999")
        if self._var is not None:
            self._var.set(self._placeholder)  # trace 中因 _showing 已置位而跳过
        else:
            self.delete(0, "end")
            self.insert(0, self._placeholder)

    def _on_var_write(self, *_):
        if self._var is None:
            return
        val = self._var.get()
        if self._showing:
            if val == self._placeholder:
                return  # 占位文案自身写入
            self._showing = False
            self.configure(foreground="#000000")
            if not val and not self._has_focus():
                # 外部清空（如更换项目重置输入框）: 恢复占位显示
                self._set_placeholder()
            return
        if not val and not self._has_focus():
            self._set_placeholder()

    def _on_focus_in(self, _e=None):
        if self._showing:
            self._showing = False
            if self._var is not None:
                self._var.set("")
            else:
                self.delete(0, "end")
            self.configure(foreground="#000000")

    def _on_focus_out(self, _e=None):
        val = self._var.get() if self._var is not None else self.get()
        if not val.strip() and not self._showing:
            self._set_placeholder()

    def _has_focus(self) -> bool:
        try:
            return bool(self.focus_get() is self)
        except KeyError:  # 窗口销毁瞬间
            return False

    # ---- 取值 ----

    def real_value(self) -> str:
        """显示占位文案时返回空串，其余返回当前内容。"""
        if self._showing:
            return ""
        val = self._var.get() if self._var is not None else self.get()
        return "" if val == self._placeholder else val


class PythubGUI:
    def __init__(self, root: "tk.Tk", log_queue: "queue.Queue"):
        self.root = root
        self.q = log_queue
        self.busy = False
        # 文件对话框默认目录：用户 home；选择后记住上次目录
        self.last_dir = Path.home()

        root.title("Pythub")
        root.geometry("880x700")
        root.minsize(880, 700)
        _center_window(root, width=880, height=700)

        nb = ttk.Notebook(root)
        self.nb = nb
        nb.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        self.tab_build = ttk.Frame(nb)
        self.tab_publish = ttk.Frame(nb)
        self.tab_tools = ttk.Frame(nb)
        self.tab_settings = ttk.Frame(nb)
        self.tab_about = ttk.Frame(nb)
        nb.add(self.tab_build, text="打包")
        nb.add(self.tab_publish, text="发布")
        nb.add(self.tab_tools, text="工具管理")
        nb.add(self.tab_settings, text="设置")
        nb.add(self.tab_about, text="关于")
        self._build_tab_build()
        self._build_tab_publish()
        self._build_tab_tools()
        self._build_tab_settings()
        self._build_tab_about()
        self._build_menubar()

        # 需求 41: 主窗口不再放置日志区，日志改为独立弹窗查看
        self._log_buffer: "deque" = deque(maxlen=8000)  # (text, tag) 环形缓冲
        self._build_log_win: Optional[_LogWindow] = None   # 打包日志弹窗（单例）
        self._tools_log_win: Optional[_LogWindow] = None   # 工具日志弹窗（单例）
        root.after(100, self._poll)

    # ------------------------------------------------ 菜单栏

    _URL_REPO = "https://github.com/Crogram/Pythub"
    _URL_HOME = "https://pythub.org"
    _URL_ISSUES = "https://github.com/Crogram/Pythub/issues"

    def _select_tab(self, tab) -> None:
        self.nb.select(tab)

    def _build_menubar(self):
        from pythub.utils import LOG_DIR

        menubar = tk.Menu(self.root)

        # 功能
        m_func = tk.Menu(menubar, tearoff=0)
        m_func.add_command(label="打包", accelerator="⌘1",
                           command=lambda: self._select_tab(self.tab_build))
        m_func.add_command(label="发布 PyPI", accelerator="⌘2",
                           command=lambda: self._select_tab(self.tab_publish))
        m_func.add_command(label="设置", accelerator="⌘3",
                           command=lambda: self._select_tab(self.tab_settings))
        m_func.add_command(label="工具管理", accelerator="⌘4",
                           command=lambda: self._select_tab(self.tab_tools))
        m_func.add_separator()
        m_func.add_command(label="关于 Pythub",
                           command=lambda: self._select_tab(self.tab_about))
        menubar.add_cascade(label="功能", menu=m_func)

        # 工具
        m_tool = tk.Menu(menubar, tearoff=0)
        m_tool.add_command(label="打开缓存目录",
                           command=lambda: self._open_dir(PYTHUB_CACHE_DIR))
        m_tool.add_command(label="打开日志目录",
                           command=lambda: self._open_dir(LOG_DIR))
        m_tool.add_separator()
        m_tool.add_command(label="清理缓存", command=self._prune_cache)
        menubar.add_cascade(label="工具", menu=m_tool)

        # 帮助
        m_help = tk.Menu(menubar, tearoff=0)
        m_help.add_command(label="GitHub 仓库",
                           command=lambda: webbrowser.open(self._URL_REPO))
        m_help.add_command(label="官网",
                           command=lambda: webbrowser.open(self._URL_HOME))
        m_help.add_command(label="技术支持（提交问题）",
                           command=lambda: webbrowser.open(self._URL_ISSUES))
        m_help.add_separator()
        m_help.add_command(label="关于 Pythub",
                           command=lambda: self._select_tab(self.tab_about))
        menubar.add_cascade(label="帮助", menu=m_help)

        self.root.config(menu=menubar)
        # 快捷键: ⌘1/⌘2/⌘3 切换页签
        self.root.bind("<Command-1>", lambda _e: self._select_tab(self.tab_build))
        self.root.bind("<Command-2>", lambda _e: self._select_tab(self.tab_publish))
        self.root.bind("<Command-3>", lambda _e: self._select_tab(self.tab_settings))
        self.root.bind("<Command-4>", lambda _e: self._select_tab(self.tab_tools))

    # ------------------------------------------------ 日志

    def _poll(self):
        """主线程定时取出队列日志 -> 环形缓冲；打包日志弹窗打开时增量显示（需求 41）。"""
        lines = []
        try:
            for _ in range(200):
                text, level = self.q.get_nowait()
                tag = level if level in ("WARNING", "ERROR", "INFO") else "INFO"
                lines.append((text, tag))
        except queue.Empty:
            pass
        if lines:
            self._log_buffer.extend(lines)
            win = self._build_log_win
            if win is not None:
                if win.win.winfo_exists():
                    win.append_lines(lines)
                else:
                    self._build_log_win = None
        self.root.after(120, self._poll)

    # ------------------------------------------------ 打包页

    def _build_tab_build(self):
        f = self.tab_build
        frm = ttk.Frame(f)
        frm.pack(fill="x", padx=8, pady=6)
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="项目目录:").grid(row=0, column=0, sticky="e", padx=4, pady=4)
        self.var_project = tk.StringVar()
        self.ent_project = _PlaceholderEntry(
            frm, "选择项目目录，如 ~/Projects/hello_app（含 pythub.toml 或入口脚本）",
            self.var_project)
        self.ent_project.grid(row=0, column=1, sticky="we", padx=4)
        ttk.Button(frm, text="浏览...", command=self._pick_project).grid(row=0, column=2, padx=4)
        ttk.Button(frm, text="打开",
                   command=lambda: self._open_folder_from(self.ent_project, "项目目录")
                   ).grid(row=0, column=3, padx=(0, 4))

        ttk.Label(frm, text="入口脚本:").grid(row=1, column=0, sticky="e", padx=4, pady=4)
        self.var_entry = tk.StringVar(value="")
        self.ent_entry = _PlaceholderEntry(
            frm, "如 main.py（留空默认 main.py）", self.var_entry)
        self.ent_entry.grid(row=1, column=1, sticky="we", padx=4)

        ttk.Label(frm, text="应用名称:").grid(row=2, column=0, sticky="e", padx=4, pady=4)
        self.var_name = tk.StringVar(value="")
        self.ent_name = _PlaceholderEntry(
            frm, "如 hello_app（留空默认 app；选项目后自动读取 pythub.toml）",
            self.var_name)
        self.ent_name.grid(row=2, column=1, sticky="we", padx=4)

        # 需求 42: 输出目录与打包产物展示上移，紧跟应用名称
        ttk.Label(frm, text="输出目录:").grid(row=3, column=0, sticky="e", padx=4, pady=4)
        self.var_output = tk.StringVar(value="")
        # 需求 43: 「留空默认为...」提示移入 placeholder
        self.ent_output = _PlaceholderEntry(frm, "留空默认为 <项目>/dist", self.var_output)
        self.ent_output.grid(row=3, column=1, sticky="we", padx=4)
        ttk.Button(frm, text="浏览...", command=self._pick_output).grid(row=3, column=2, padx=4)
        ttk.Button(frm, text="打开",
                   command=lambda: self._open_folder_from(self.ent_output, "输出目录")
                   ).grid(row=3, column=3, padx=(0, 4))

        # macOS .app 的 CFBundleIdentifier（pythub 引擎与 PyInstaller/py2app 均同步）
        ttk.Label(frm, text="应用标识:").grid(row=4, column=0, sticky="e", padx=4, pady=4)
        self.var_bundle_id = tk.StringVar(value="")
        self.ent_bundle_id = _PlaceholderEntry(
            frm, "如 com.example.myapp（macOS .app 标识，留空自动生成 org.crogram.<名称>）",
            self.var_bundle_id)
        self.ent_bundle_id.grid(row=4, column=1, columnspan=3, sticky="we", padx=4)

        # ---- 打包产物展示区（需求 42: 紧跟输出目录）----
        self.frm_result = ttk.LabelFrame(f, text="打包产物", padding=6)
        self.frm_result.pack(fill="x", padx=8, pady=(0, 8))
        self._result_rows: list = []
        self._result_vars: list = []  # 保持 StringVar 引用，防止被 GC 后输入框清空
        self._last_artifacts: list = []
        self._last_output_dir: Path | None = None
        self._show_result_hint()

        opt = ttk.Frame(f)
        opt.pack(fill="x", padx=8, pady=6)
        ttk.Label(opt, text="目标平台:").grid(row=0, column=0, sticky="e", padx=4, pady=4)
        self.var_platforms = {}
        for i, p in enumerate(PLATFORM_FORMATS):
            v = tk.BooleanVar(value=False)
            ttk.Checkbutton(opt, text=p, variable=v, command=self._refresh_formats).grid(
                row=0, column=1 + i, sticky="w", padx=6)
            self.var_platforms[p] = v

        ttk.Label(opt, text="架构:").grid(row=1, column=0, sticky="e", padx=4, pady=4)
        try:
            from pythub.exe_archive import native_host_arch as _host_arch
            _default_arch = _host_arch() if _host_arch() in SUPPORTED_ARCHS else "x86_64"
        except Exception:  # noqa: BLE001
            _default_arch = "x86_64"
        self.var_arch = tk.StringVar(value=_default_arch)
        ttk.Combobox(opt, textvariable=self.var_arch, values=list(SUPPORTED_ARCHS),
                     state="readonly", width=10).grid(row=1, column=1, sticky="w", padx=6)

        ttk.Label(opt, text="Python 版本:").grid(row=1, column=2, sticky="e", padx=4)
        from pythub.tools import default_python as _default_python
        self.var_py = tk.StringVar(value=_default_python())  # 需求 37: 默认运行版本可在工具管理页设置
        ttk.Combobox(opt, textvariable=self.var_py, values=list(SUPPORTED_PYTHON),
                     state="readonly", width=8).grid(row=1, column=3, sticky="w", padx=6)

        ttk.Label(opt, text="输出类型:").grid(row=2, column=0, sticky="e", padx=4, pady=4)
        self.var_fmt = tk.StringVar()
        self.cmb_fmt = ttk.Combobox(opt, textvariable=self.var_fmt, state="readonly", width=18)
        self.cmb_fmt.grid(row=2, column=1, sticky="w", padx=6)
        self._refresh_formats()

        ttk.Label(opt, text="引擎:").grid(row=2, column=2, sticky="e", padx=4)
        self.var_engine = tk.StringVar(value="pythub")
        cmb_engine = ttk.Combobox(opt, textvariable=self.var_engine,
                                  values=["pythub", "zipapp"] + list(_ENGINE_NAMES),
                                  state="readonly", width=12)
        cmb_engine.grid(row=2, column=3, sticky="w", padx=6)
        cmb_engine.bind("<<ComboboxSelected>>", lambda _e: self._refresh_formats())

        ttk.Label(opt, text="额外依赖:").grid(row=3, column=0, sticky="e", padx=4)
        self.var_reqs = tk.StringVar()
        ttk.Entry(opt, textvariable=self.var_reqs, width=32).grid(
            row=3, column=1, columnspan=3, sticky="w", padx=6)
        ttk.Label(opt, text="(逗号分隔，留空自动分析)").grid(row=3, column=4, sticky="w")

        self.var_windowed = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            opt, text="窗口模式（GUI 应用隐藏控制台黑框）",
            variable=self.var_windowed).grid(
            row=4, column=1, columnspan=4, sticky="w", padx=6)
        self.lbl_windowed_hint = ttk.Label(opt, text="", foreground="#777")
        self.lbl_windowed_hint.grid(row=5, column=1, columnspan=4, sticky="w", padx=6)
        self.cmb_fmt.bind("<<ComboboxSelected>>",
                          lambda _e: self._update_windowed_hint())
        self._update_windowed_hint()

        # 内存模式（需求 23/64，仅 exe/pexe 格式生效）: disk/mem/embed
        row_mem = ttk.Frame(opt)
        row_mem.grid(row=6, column=1, columnspan=4, sticky="w", padx=6)
        ttk.Label(row_mem, text="内存模式:").pack(side="left")
        self.var_mem_mode = tk.StringVar(value="disk")
        self.cmb_mem = ttk.Combobox(
            row_mem, textvariable=self.var_mem_mode,
            values=["disk", "mem", "embed", "static"], state="readonly", width=7)
        self.cmb_mem.pack(side="left", padx=6)
        self.lbl_mem_hint = ttk.Label(row_mem, text="", foreground="#777")
        self.lbl_mem_hint.pack(side="left", padx=4)
        self.cmb_mem.bind("<<ComboboxSelected>>",
                          lambda e: self._mem_mode_hint())
        self._mem_mode_hint()

        self.var_sign = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            opt, text="签名（.exe: Authenticode / macos: codesign / deb·rpm: GPG）",
            variable=self.var_sign).grid(
            row=7, column=1, columnspan=4, sticky="w", padx=6)

        # 签名与证书管理（需求 57/58/62）: 独立弹窗配置，选定后回显在打包页
        row_sign = ttk.Frame(opt)
        row_sign.grid(row=8, column=1, columnspan=4, sticky="w", padx=6, pady=(2, 0))
        ttk.Button(row_sign, text="签名管理…", width=12,
                   command=self._open_sign_dialog).pack(side="left")
        self.var_sign_identity = tk.StringVar(value="")   # 空 = ad-hoc
        self.var_sign_gpg_key = tk.StringVar(value="")    # deb/rpm GPG 密钥，空 = 默认
        self.var_sign_choice = tk.StringVar(
            value="codesign: Ad-hoc（默认） | GPG: 默认密钥 | 时间戳: 无")
        ttk.Label(row_sign, textvariable=self.var_sign_choice, foreground="#777").pack(
            side="left", padx=6)

        row_cert = ttk.Frame(opt)
        row_cert.grid(row=9, column=1, columnspan=4, sticky="w", padx=6, pady=(2, 0))
        ttk.Button(row_cert, text="证书管理…", width=12,
                   command=self._open_cert_dialog).pack(side="left")
        self.var_sign_cert = tk.StringVar(value="")
        self.var_sign_key = tk.StringVar(value="")
        self.var_sign_pfx = tk.StringVar(value="")        # PKCS#12 单文件证书
        self.var_sign_pfx_pass = tk.StringVar(value="")   # .p12/.pfx 密码
        self.var_sign_timestamp = tk.StringVar(value="")  # RFC3161 时间戳地址
        self.var_notarize = tk.BooleanVar(value=False)    # macOS 公证
        self.var_apple_id = tk.StringVar(value="")        # 公证 Apple ID
        self.var_apple_password = tk.StringVar(value="")  # 公证应用专用密码
        self.var_team_id = tk.StringVar(value="")         # 公证 Team ID
        self.var_entitlements = tk.StringVar(value="")    # entitlements plist
        self.var_cert_choice = tk.StringVar(value="自动自签证书（默认）")
        ttk.Label(row_cert, textvariable=self.var_cert_choice, foreground="#777").pack(
            side="left", padx=6)

        # 可执行产物自动加执行权限（需求 29: run/pexe/pyz 等）
        self.var_chmod = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            opt, text="为可执行产物添加执行权限（run/pexe/pyz 等，chmod +x）",
            variable=self.var_chmod).grid(
            row=10, column=1, columnspan=4, sticky="w", padx=6)

        # mac pexe 再打包为 .app 壳（Finder 识别为应用）
        self.var_app_wrap = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            opt, text="mac pexe 再打包为 .app 壳（Finder 识别为应用，适合 GUI）",
            variable=self.var_app_wrap).grid(
            row=11, column=1, columnspan=4, sticky="w", padx=6)

        # rpm 包引擎选择（需求 36: mac/linux 构建机可选自研或 rpmbuild）
        ttk.Label(opt, text="RPM 引擎:").grid(row=12, column=0, sticky="e", padx=4, pady=2)
        self.var_rpm_engine = tk.StringVar(value="auto")
        ttk.Combobox(opt, textvariable=self.var_rpm_engine,
                     values=["auto", "self", "rpmbuild"],
                     state="readonly", width=10).grid(row=12, column=1, sticky="w", padx=6)
        ttk.Label(opt, text="auto=有 rpmbuild 则用；self=自研（三端可交叉）；"
                           "rpmbuild=仅 mac/linux 构建机", foreground="#777").grid(
            row=12, column=2, columnspan=3, sticky="w")

        self.var_status = tk.StringVar(value="就绪")
        ttk.Label(f, textvariable=self.var_status, foreground="#367bf0").pack(padx=8, anchor="w")
        row_bb = ttk.Frame(f)
        row_bb.pack(padx=8, pady=6, anchor="w")
        self.btn_build = ttk.Button(row_bb, text="开始打包", command=self._start_build)
        self.btn_build.pack(side="left")
        # 需求 41: 打包日志按钮跟在「开始打包」按钮后面
        ttk.Button(row_bb, text="查看打包日志",
                   command=self._show_build_log).pack(side="left", padx=6)

    def _clear_result_rows(self):
        for w in self._result_rows:
            w.destroy()
        self._result_rows.clear()
        self._result_vars.clear()

    def _add_result_row(self, label_text: str, path, buttons: list) -> None:
        """添加一行：左侧文案 + 中间只读输入框 + 最右侧按钮组。"""
        row = ttk.Frame(self.frm_result)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text=label_text, width=8, anchor="e").pack(
            side="left", padx=(0, 4))
        var = tk.StringVar(value=str(path))
        self._result_vars.append(var)  # 防止 StringVar 被 GC
        ent = ttk.Entry(row, textvariable=var, state="readonly")
        ent.pack(side="left", fill="x", expand=True, padx=(0, 6))
        for text, cmd in buttons:
            ttk.Button(row, text=text, width=12, command=cmd).pack(
                side="left", padx=(0, 4))
        self._result_rows.append(row)

    def _copy_to_clipboard(self, path):
        self.root.clipboard_clear()
        self.root.clipboard_append(str(path))
        self.var_status.set(f"已复制到剪贴板 ✓  {path}")

    def _open_output_dir(self):
        if not self._last_output_dir:
            return
        import subprocess
        d = str(self._last_output_dir)
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", d])
            elif sys.platform == "win32":
                os.startfile(d)  # noqa: S606
            else:
                subprocess.Popen(["xdg-open", d])
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("错误", f"无法打开目录: {e}")

    def _show_result_hint(self):
        self._clear_result_rows()
        row = ttk.Frame(self.frm_result)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text="（打包完成后，产物路径将显示在这里）", foreground="#999").pack(
            side="left", padx=(0, 4))
        self._result_rows.append(row)

    def _show_artifacts(self, artifacts: list, output_dir):
        self._last_artifacts = list(artifacts)
        self._last_output_dir = Path(output_dir)
        # 需求 42: 打包完成后回填实际输出目录到上方输入框
        self.var_output.set(str(self._last_output_dir))
        self._clear_result_rows()
        # 每个产物一行：右侧 [复制路径]
        for i, a in enumerate(artifacts, 1):
            self._add_result_row(
                f"产物 {i}", a,
                [("复制路径", lambda p=a: self._copy_to_clipboard(p))],
            )

    def _refresh_formats(self):
        plats = [p for p, v in self.var_platforms.items() if v.get()]
        # 控件构建早期（var_engine 创建前）可能被调用，需守卫
        engine = self.var_engine.get() if hasattr(self, "var_engine") else "pythub"
        fmts = []
        for p in plats:
            # 三方引擎按引擎支持格式过滤（pythub/zipapp 用平台全量格式）
            efmts = PLATFORM_FORMATS[p] if engine in ("pythub", "zipapp") \
                else list(_ENGINES[engine]["fmts"])
            for fm in efmts:
                if f"{p}:{fm}" not in fmts:
                    fmts.append(f"{p}:{fm}")
        self.cmb_fmt["values"] = fmts
        self.var_fmt.set(fmts[0] if fmts else "")
        self._update_windowed_hint()

    def _mem_mode_hint(self):
        """内存模式说明（需求 23/64）。"""
        hints = {
            "disk": "常规解压执行（默认）",
            "mem": "源码字节码内嵌、内存执行，产物 .mem.*",
            "embed": "内嵌解释器: dlopen PBS libpython，落盘 2-3 文件，产物 .embed.*",
            "static": "静态内嵌: 解释器链进入口二进制，零落盘，产物 .static.*；Windows 需本机构建",
        }
        self.lbl_mem_hint.config(
            text=hints.get(self.var_mem_mode.get(), ""), foreground="#777")

    def _update_windowed_hint(self):
        """窗口模式选项提示: 仅 pythub 引擎的 exe/pexe 引导器消费 console 参数。"""
        if not hasattr(self, "lbl_windowed_hint"):
            return  # 打包页控件尚未创建完成（_refresh_formats 首次调用）
        fmt = self.var_fmt.get().split(":")[-1] if self.var_fmt.get() else ""
        engine = self.var_engine.get() if hasattr(self, "var_engine") else "pythub"
        if engine not in ("pythub", "zipapp"):
            self.lbl_windowed_hint.config(text="三方引擎由引擎自身处理窗口模式")
        elif fmt in ("exe", "pexe"):
            self.lbl_windowed_hint.config(text="当前格式支持窗口模式（仅 Windows 生效）")
        else:
            self.lbl_windowed_hint.config(text="当前格式不支持窗口模式，该选项将被忽略")

    def _pick_project(self):
        d = filedialog.askdirectory(title="选择项目目录", initialdir=str(self.last_dir))
        if not d:
            return
        self.last_dir = Path(d)
        self.var_project.set(d)
        # 更换项目后，入口脚本/应用名称/输出目录一律重置为新项目的值:
        # pythub.toml 存在则读取其配置，否则清空（显示 placeholder）
        cfg = None
        if (Path(d) / "pythub.toml").exists():
            from pythub.config import app_config

            cfg = app_config(Path(d))
        self.var_name.set(cfg["name"] if cfg else "")
        self.var_entry.set(cfg["entry"] if cfg else "")
        self.var_output.set(str(Path(d) / "dist"))
        self.var_bundle_id.set(cfg["bundle_id"] if cfg else "")
        # 发布页项目目录的占位提示动态跟随打包页项目（留空时生效）
        if hasattr(self, "ent_pub_project"):
            self.ent_pub_project.set_placeholder(
                f"留空默认使用打包页的项目目录（当前: {d}）")

    def _pick_output(self):
        """需求 42: 选择打包产物输出目录。"""
        d = filedialog.askdirectory(
            title="选择输出目录",
            initialdir=self.ent_output.real_value().strip() or str(self.last_dir))
        if d:
            self.var_output.set(str(Path(d)))

    def _open_folder_from(self, entry, label: str):
        """需求 43: 打开输入框对应路径的文件夹（项目目录/输出目录）。"""
        raw = entry.real_value().strip()
        if not raw:
            messagebox.showinfo("提示", f"请先填写{label}")
            return
        d = Path(raw).expanduser()
        if not d.is_dir():
            messagebox.showinfo("提示", f"目录不存在:\n{d}")
            return
        self._open_dir(d)

    def _start_build(self):
        if self.busy:
            messagebox.showinfo("提示", "打包正在进行中，请等待完成")
            return
        # 需求 43: 用 real_value() 取值，placeholder 显示时视为空串
        project = Path(self.ent_project.real_value().strip())
        if not project.is_dir():
            messagebox.showerror("错误", "请选择有效的项目目录")
            return
        if not self.var_fmt.get():
            messagebox.showerror("错误", "请选择目标平台与输出类型")
            return
        plat, fmt = self.var_fmt.get().split(":", 1)
        reqs = [r.strip() for r in self.var_reqs.get().split(",") if r.strip()]

        from pythub.builder import BuildSpec, build
        from pythub.utils import get_logger

        logger = get_logger()
        spec = BuildSpec(
            project_dir=project.resolve(),
            name=self.ent_name.real_value().strip() or "app",
            entry=self.ent_entry.real_value().strip() or "main.py",
            requirements=reqs,
            platform=plat,
            arch=self.var_arch.get(),
            python=self.var_py.get(),
            fmt=fmt,
            output=Path(self.ent_output.real_value().strip() or project.resolve() / "dist"),  # 需求 42/43: 自定义输出目录，placeholder 视为空
            engine=self.var_engine.get(),
            windowed=self.var_windowed.get(),
            mem=self.var_mem_mode.get() == "mem",
            mem_mode=self.var_mem_mode.get(),
            sign=self.var_sign.get(),
            sign_cert=self.var_sign_cert.get().strip(),
            sign_key=self.var_sign_key.get().strip(),
            sign_identity=self.var_sign_identity.get().strip(),
            sign_gpg_key=self.var_sign_gpg_key.get().strip(),
            sign_pfx=self.var_sign_pfx.get().strip(),
            sign_pfx_pass=self.var_sign_pfx_pass.get().strip(),
            sign_timestamp=self.var_sign_timestamp.get().strip(),
            notarize=self.var_notarize.get(),
            apple_id=self.var_apple_id.get().strip(),
            apple_password=self.var_apple_password.get(),
            team_id=self.var_team_id.get().strip(),
            entitlements=self.var_entitlements.get().strip(),
            chmod=self.var_chmod.get(),
            app_wrap=self.var_app_wrap.get(),
            rpm_engine=self.var_rpm_engine.get(),
            bundle_id=self.var_bundle_id.get().strip(),
        )

        attach_queue_handler(self.q)
        self.busy = True
        self.var_status.set(f"正在打包 {spec.name} -> {plat}:{fmt} ...")
        self.btn_build.configure(state="disabled")

        def finish_ok(artifacts):
            self.busy = False
            self.var_status.set("打包完成 ✓")
            self.btn_build.configure(state="normal")
            self._show_artifacts(artifacts, spec.output)
            messagebox.showinfo(
                "打包成功",
                "产物:\n" + "\n".join(str(a) for a in artifacts)
                + "\n\n输出目录: " + str(spec.output),
            )

        def finish_err(err):
            self.busy = False
            self.var_status.set("打包失败 ✗")
            self.btn_build.configure(state="normal")
            messagebox.showerror("打包失败", str(err))

        def work():
            try:
                artifacts = build(spec, logger)
                self.root.after(0, lambda: finish_ok(artifacts))
            except Exception as e:  # noqa: BLE001
                logger.error("打包失败: %s", e)
                self.root.after(0, lambda err=e: finish_err(err))

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------ 签名 / 证书管理（需求 57/58）

    def _open_sign_dialog(self):
        """签名管理弹窗（统一签名中心）: macOS codesign 身份 + deb/rpm GPG 密钥
        （需求 58/62），选定后合并回显到打包页。"""
        from pythub.signing import codesign_tool, list_codesign_identities, list_gpg_keys

        has_codesign = bool(codesign_tool())
        has_gpg = bool(list_gpg_keys())  # 先探测，供提示文案
        identities = list_codesign_identities() if has_codesign else []

        win = tk.Toplevel(self.root)
        win.title("签名管理")
        win.geometry("640x640")
        win.transient(self.root)
        win.grab_set()
        _center_window(win, self.root, 640, 640)

        hint = ("macOS: 选择 codesign 签名身份（构建机为 mac 且钥匙串含有效"
                "证书时可枚举）；\n"
                "deb/rpm: 选择 GPG 密钥（rpm 签名必须指定，deb 留空用 gpg 默认密钥）；\n"
                "Windows .exe 的 Authenticode 证书请在「证书管理」中配置。")
        if not has_codesign:
            hint += "\n（本机未找到 codesign，仅可选择 Ad-hoc；构建时将跳过 mac 签名）"
        ttk.Label(win, text=hint, foreground="#777",
                  wraplength=600, justify="left").pack(anchor="w", padx=12, pady=(10, 6))

        def _pick(ent, title, ft=None):
            p = filedialog.askopenfilename(title=f"选择{title}", parent=win,
                                           filetypes=ft or [])
            if p:
                ent.delete(0, "end")
                ent.insert(0, p)

        # 区块 1: macOS codesign 身份（需求 58）
        lbl_cs = ttk.Label(win, text="macOS codesign 签名身份:")
        lbl_cs.pack(anchor="w", padx=12)
        lb = tk.Listbox(win, activestyle="dotbox", height=6)
        lb.pack(fill="both", expand=True, padx=12, pady=(2, 6))

        def _fill_codesign():
            nonlocal identities
            identities = list_codesign_identities() if codesign_tool() else []
            lb.delete(0, "end")
            lb.insert("end", "Ad-hoc 临时签名（无需证书，不获系统信任）")
            for _h, name in identities:
                lb.insert("end", name)
            lb.selection_clear(0, "end")
            cur = self.var_sign_identity.get().strip()
            if cur:
                for i, (_h, name) in enumerate(identities, 1):
                    if name == cur:
                        lb.selection_set(i)
                        lb.see(i)
                        break
                else:
                    lb.selection_set(0)
            else:
                lb.selection_set(0)

        _fill_codesign()

        # 区块 2: deb/rpm GPG 密钥（需求 62）
        lbl_gpg = ttk.Label(win, text="deb/rpm 签名 GPG 密钥（gpg 私钥）:")
        lbl_gpg.pack(anchor="w", padx=12)
        lb_gpg = tk.Listbox(win, activestyle="dotbox", height=5)
        lb_gpg.pack(fill="both", expand=True, padx=12, pady=(2, 6))
        gpg_keys = list_gpg_keys()

        def _fill_gpg():
            nonlocal gpg_keys
            gpg_keys = list_gpg_keys()
            lb_gpg.delete(0, "end")
            lb_gpg.insert("end", "不使用 GPG 密钥（deb 签名将尝试默认密钥）")
            for kid, name in gpg_keys:
                lb_gpg.insert("end", f"{name} ({kid})")
            lb_gpg.selection_clear(0, "end")
            cur = self.var_sign_gpg_key.get().strip()
            if cur:
                for i, (kid, _name) in enumerate(gpg_keys, 1):
                    if kid == cur:
                        lb_gpg.selection_set(i)
                        lb_gpg.see(i)
                        break
                else:
                    lb_gpg.selection_set(0)
            else:
                lb_gpg.selection_set(0)
            if not gpg_keys:
                lbl_gpg.config(text="deb/rpm 签名 GPG 密钥（本机未找到 gpg 或无私钥，"
                                    "brew install gnupg / apt install gnupg）:")

        _fill_gpg()

        # 区块 3: Windows .exe RFC3161 时间戳（可选，正式证书建议填写）
        frm_ts = ttk.Frame(win)
        frm_ts.pack(fill="x", padx=12, pady=(0, 4))
        ttk.Label(frm_ts, text="时间戳(RFC3161):").pack(side="left")
        ent_ts = ttk.Entry(frm_ts)
        ent_ts.pack(side="left", fill="x", expand=True, padx=6)
        ent_ts.insert(0, self.var_sign_timestamp.get())
        ttk.Label(frm_ts, text="如 http://timestamp.digicert.com（空=不加）",
                  foreground="#777").pack(side="left")

        # 区块 4: macOS 公证（Gatekeeper 分发）与 entitlements（需求 65）
        lf_not = ttk.LabelFrame(win, text="macOS 公证（notarization）与权限声明", padding=8)
        lf_not.pack(fill="x", padx=12, pady=(4, 6))
        row_n0 = ttk.Frame(lf_not)
        row_n0.pack(anchor="w", pady=(0, 4))
        ttk.Checkbutton(row_n0, text="构建后公证并 staple（分发到其他 mac 的 Gatekeeper 要求）",
                        variable=self.var_notarize).pack(side="left")
        row_n1 = ttk.Frame(lf_not)
        row_n1.pack(fill="x", pady=2)
        ttk.Label(row_n1, text="Apple ID:", width=12).pack(side="left")
        ent_aid = ttk.Entry(row_n1)
        ent_aid.pack(side="left", fill="x", expand=True, padx=(0, 10))
        ent_aid.insert(0, self.var_apple_id.get())
        ttk.Label(row_n1, text="专用密码:", width=10).pack(side="left")
        ent_apass = ttk.Entry(row_n1, show="*")
        ent_apass.pack(side="left", fill="x", expand=True)
        ent_apass.insert(0, self.var_apple_password.get())
        row_n2 = ttk.Frame(lf_not)
        row_n2.pack(fill="x", pady=2)
        ttk.Label(row_n2, text="Team ID:", width=12).pack(side="left")
        ent_tid = ttk.Entry(row_n2, width=16)
        ent_tid.pack(side="left", padx=(0, 10))
        ent_tid.insert(0, self.var_team_id.get())
        ttk.Label(row_n2, text="entitlements:", width=13).pack(side="left")
        ent_ent = ttk.Entry(row_n2)
        ent_ent.pack(side="left", fill="x", expand=True, padx=(0, 4))
        ent_ent.insert(0, self.var_entitlements.get())
        ttk.Button(row_n2, text="浏览", width=6,
                   command=lambda: _pick(
                       ent_ent, "entitlements plist",
                       ft=[("Plist", "*.plist"), ("全部文件", "*")])).pack(side="left")
        ttk.Label(lf_not, text="应用专用密码在 appleid.apple.com 生成（非登录密码）；"
                              "密码仅保存在本次会话，不会写入 settings.json。",
                  foreground="#777").pack(anchor="w", pady=(4, 0))

        def confirm():
            sel = lb.curselection()
            if not sel:
                messagebox.showwarning("签名管理", "请选择 macOS 签名身份", parent=win)
                return
            idx = sel[0]
            if idx == 0:
                self.var_sign_identity.set("")
            else:
                self.var_sign_identity.set(identities[idx - 1][1])
            sel_gpg = lb_gpg.curselection()
            if not sel_gpg:
                messagebox.showwarning("签名管理", "请选择 GPG 密钥项", parent=win)
                return
            gidx = sel_gpg[0]
            if gidx == 0:
                self.var_sign_gpg_key.set("")
            else:
                self.var_sign_gpg_key.set(gpg_keys[gidx - 1][0])
            cs = self.var_sign_identity.get().strip() or "Ad-hoc"
            gpg = self.var_sign_gpg_key.get().strip()
            gpg_disp = gpg if gpg else "默认密钥"
            ts = ent_ts.get().strip()
            self.var_sign_timestamp.set(ts)
            # 公证信息写回
            self.var_apple_id.set(ent_aid.get().strip())
            self.var_apple_password.set(ent_apass.get())
            self.var_team_id.set(ent_tid.get().strip())
            self.var_entitlements.set(ent_ent.get().strip())
            if self.var_notarize.get() and not (
                    self.var_apple_id.get() and self.var_apple_password.get()):
                messagebox.showwarning(
                    "签名管理", "勾选公证需填写 Apple ID 与应用专用密码", parent=win)
                return
            notar = (self.var_apple_id.get().split("@")[0]
                     if self.var_notarize.get() else "否")
            ts_disp = "无" if not ts else ts.split("//")[-1].rstrip("/")
            self.var_sign_choice.set(
                f"codesign: {cs} | GPG: {gpg_disp} | 时间戳: {ts_disp}"
                f" | 公证: {notar}")
            win.destroy()

        btnrow = ttk.Frame(win)
        btnrow.pack(pady=6)
        ttk.Button(btnrow, text="刷新列表",
                   command=lambda: (_fill_codesign(), _fill_gpg())).pack(
            side="left", padx=4)
        ttk.Button(btnrow, text="确定", command=confirm).pack(side="left", padx=4)
        ttk.Button(btnrow, text="取消", command=win.destroy).pack(side="left", padx=4)

    def _open_cert_dialog(self):
        """证书管理弹窗（需求 57）: 缓存路径展示、打开目录、清理自签证书，
        自定义 Authenticode 证书/私钥选择（Windows .exe），以及 macOS
        开发者证书的钥匙串加载与 .p12 导入。"""
        import tkinter.simpledialog as simpledialog
        from pythub.signing import CERT_DIR, list_codesign_identities

        win = tk.Toplevel(self.root)
        win.title("证书管理")
        win.geometry("640x580")
        win.transient(self.root)
        win.grab_set()
        _center_window(win, self.root, 640, 580)

        # 1) 自签证书缓存（需求 57）
        lf1 = ttk.LabelFrame(win, text="自签证书缓存（Authenticode 自动生成）", padding=8)
        lf1.pack(fill="x", padx=12, pady=(10, 6))
        ttk.Label(lf1, text=f"缓存目录: {CERT_DIR}", foreground="#777").pack(anchor="w")
        var_cert_status = tk.StringVar()
        ttk.Label(lf1, textvariable=var_cert_status, foreground="#777").pack(
            anchor="w", pady=(2, 6))

        def _refresh_cert_status():
            crt = CERT_DIR / "selfsigned.crt"
            key = CERT_DIR / "selfsigned.key"
            if crt.exists() and key.exists():
                var_cert_status.set("状态: 存在 selfsigned.crt / selfsigned.key")
            else:
                var_cert_status.set("状态: 暂无（首次签名时自动生成，10 年有效期）")

        row1 = ttk.Frame(lf1)
        row1.pack(anchor="w")
        ttk.Button(row1, text="打开证书目录",
                   command=self._open_cert_cache_dir).pack(side="left")
        ttk.Button(row1, text="清理自签证书",
                   command=lambda: (self._clean_selfsigned_cert(),
                                    _refresh_cert_status())).pack(side="left", padx=6)
        _refresh_cert_status()

        # 2) 自定义证书（Windows .exe Authenticode）
        lf2 = ttk.LabelFrame(win, text="自定义证书（Windows .exe Authenticode）", padding=8)
        lf2.pack(fill="x", padx=12, pady=6)
        ttk.Label(lf2, text="PEM: 证书 (.crt/.pem) 与私钥 (.key/.pem) 成对提供；"
                            "PKCS12: 单文件 .p12/.pfx（正式 CA 证书常见格式）+ 密码；"
                            "均留空则自动自签。\n"
                            "deb/rpm 的 GPG 签名密钥在「签名管理」弹窗中选择"
                            "（gpg 钥匙串，非文件证书）。").pack(anchor="w")
        row2 = ttk.Frame(lf2)
        row2.pack(fill="x", pady=4)
        ttk.Label(row2, text="证书:", width=6).pack(side="left")
        ent_cert = ttk.Entry(row2)
        ent_cert.pack(side="left", fill="x", expand=True, padx=4)
        ent_cert.insert(0, self.var_sign_cert.get())
        row3 = ttk.Frame(lf2)
        row3.pack(fill="x", pady=4)
        ttk.Label(row3, text="私钥:", width=6).pack(side="left")
        ent_key = ttk.Entry(row3)
        ent_key.pack(side="left", fill="x", expand=True, padx=4)
        ent_key.insert(0, self.var_sign_key.get())

        def _pick(ent, title, ft=None):
            p = filedialog.askopenfilename(title=f"选择{title}", parent=win,
                                           filetypes=ft or [])
            if p:
                ent.delete(0, "end")
                ent.insert(0, p)

        ttk.Button(row2, text="浏览", width=6,
                   command=lambda: _pick(ent_cert, "证书文件")).pack(side="left")
        ttk.Button(row3, text="浏览", width=6,
                   command=lambda: _pick(ent_key, "私钥文件")).pack(side="left")
        # PKCS#12 单文件模式（CA 颁发的正式代码签名证书常见格式）
        row4 = ttk.Frame(lf2)
        row4.pack(fill="x", pady=4)
        ttk.Label(row4, text="PKCS12:", width=6).pack(side="left")
        ent_pfx = ttk.Entry(row4)
        ent_pfx.pack(side="left", fill="x", expand=True, padx=4)
        ent_pfx.insert(0, self.var_sign_pfx.get())
        ent_pass = ttk.Entry(row4, show="*", width=14)
        ent_pass.pack(side="left", padx=(0, 4))
        ent_pass.insert(0, self.var_sign_pfx_pass.get())
        ttk.Label(row4, text="密码", foreground="#777").pack(side="left")
        ttk.Button(row4, text="浏览", width=6,
                   command=lambda: _pick(
                       ent_pfx, "PKCS#12 证书",
                       ft=[("PKCS#12 证书", "*.p12 *.pfx"), ("全部文件", "*")])).pack(side="left")
        if sys.platform == "darwin":
            lf3 = ttk.LabelFrame(win, text="macOS 开发者证书（登录钥匙串）", padding=8)
            lf3.pack(fill="both", expand=True, padx=12, pady=6)
            ttk.Label(lf3, text="钥匙串中的 Apple 开发者证书"
                               "（Apple Development / Apple Distribution /"
                               " Developer ID）即 codesign 签名身份，导入后可在"
                               "「签名管理」中选用。").pack(anchor="w")
            lb_cert = tk.Listbox(lf3, activestyle="dotbox", height=5)
            lb_cert.pack(fill="both", expand=True, pady=(4, 4))

            def _fill_mac_certs():
                lb_cert.delete(0, "end")
                idents = list_codesign_identities()
                if not idents:
                    lb_cert.insert("end", "（钥匙串暂无可用的代码签名身份）")
                    lb_cert.itemconfig(0, foreground="#999")
                    return
                for _h, name in idents:
                    lb_cert.insert("end", name)

            def _import_mac_cert():
                p = filedialog.askopenfilename(
                    title="选择开发者证书文件",
                    filetypes=[("开发者证书", "*.p12 *.pfx *.cer *.crt"),
                               ("全部文件", "*")],
                    parent=win)
                if not p:
                    return
                password = ""
                if p.lower().endswith((".p12", ".pfx")):
                    password = simpledialog.askstring(
                        "证书密码", "请输入 .p12 导出密码（无密码可留空）:",
                        show="*", parent=win)
                    if password is None:  # 用户取消
                        return
                try:
                    from pythub.signing import import_macos_cert

                    out = import_macos_cert(p, password)
                except RuntimeError as e:
                    messagebox.showerror("导入证书", str(e), parent=win)
                    return
                _fill_mac_certs()
                messagebox.showinfo(
                    "导入证书",
                    f"导入完成。\n{out}\n\n导入的身份已可在「签名管理」弹窗中选用。",
                    parent=win)
                self.var_status.set("开发者证书导入完成 ✓")

            def _open_keychain_access():
                subprocess.Popen(["open", "-a", "Keychain Access"])

            row_cert_mac = ttk.Frame(lf3)
            row_cert_mac.pack(anchor="w")
            ttk.Button(row_cert_mac, text="导入 .p12/.cer 证书",
                       command=_import_mac_cert).pack(side="left")
            ttk.Button(row_cert_mac, text="刷新列表",
                       command=_fill_mac_certs).pack(side="left", padx=6)
            ttk.Button(row_cert_mac, text="打开钥匙串访问",
                       command=_open_keychain_access).pack(side="left")
            _fill_mac_certs()
        else:
            ttk.Label(win, text="macOS 开发者证书管理仅支持 macOS 构建机。",
                      foreground="#999").pack(anchor="w", padx=12)

        def confirm():
            cert = ent_cert.get().strip()
            key = ent_key.get().strip()
            pfx = ent_pfx.get().strip()
            if pfx and (cert or key):
                messagebox.showwarning(
                    "证书管理",
                    "PKCS#12 单文件与 PEM 证书/私钥二选一，不可混用", parent=win)
                return
            if bool(cert) != bool(key):
                messagebox.showwarning(
                    "证书管理", "证书与私钥必须成对提供（或均留空以自动自签）", parent=win)
                return
            self.var_sign_cert.set(cert)
            self.var_sign_key.set(key)
            self.var_sign_pfx.set(pfx)
            self.var_sign_pfx_pass.set(ent_pass.get())
            if pfx:
                self.var_cert_choice.set(f"已选: {Path(pfx).name} (PKCS#12)")
            elif cert:
                self.var_cert_choice.set(f"已选: {Path(cert).name}")
            else:
                self.var_cert_choice.set("自动自签证书")
            win.destroy()

        btnrow = ttk.Frame(win)
        btnrow.pack(pady=8)
        ttk.Button(btnrow, text="确定", command=confirm).pack(side="left", padx=4)
        ttk.Button(btnrow, text="取消", command=win.destroy).pack(side="left", padx=4)

    def _open_cert_cache_dir(self):
        """需求 57: 打开自签证书缓存目录（不存在时先创建）。"""
        from pythub.signing import CERT_DIR

        CERT_DIR.mkdir(parents=True, exist_ok=True)
        self._open_dir(CERT_DIR)

    def _clean_selfsigned_cert(self):
        """需求 57: 清理自签证书（下次签名自动重新生成）。"""
        from pythub.signing import CERT_DIR

        if not messagebox.askyesno(
                "清理自签证书",
                "删除 selfsigned.crt / selfsigned.key？\n下次签名时自动重新生成。"):
            return
        for name in ("selfsigned.crt", "selfsigned.key"):
            (CERT_DIR / name).unlink(missing_ok=True)
        self.var_status.set("自签证书已清理 ✓")

    # ------------------------------------------------ 发布页

    def _build_tab_publish(self):
        from pythub.pypa import load_pypi_credentials
        from pythub.utils import load_settings

        f = self.tab_publish
        frm = ttk.Frame(f)
        frm.pack(fill="x", padx=8, pady=6)
        frm.columnconfigure(1, weight=1)

        # 需求 19: 启动时从 settings.json 恢复上次保存的发布配置
        saved = load_settings().get("publish", {})
        # 兼容旧配置: 单一 files 字段按扩展名拆分到 sdist/wheel 两个输入框
        saved_sdist = saved.get("sdist", "")
        saved_whl = saved.get("whl", "")
        if not saved_sdist and not saved_whl and saved.get("files"):
            for part in str(saved["files"]).split(","):
                part = part.strip()
                if not part:
                    continue
                if part.endswith(".whl"):
                    saved_whl = saved_whl or part
                else:
                    saved_sdist = saved_sdist or part

        # 需求 48: 发布页项目目录（留空默认跟随打包页，手动输入的优先）
        ttk.Label(frm, text="项目目录:").grid(row=0, column=0, sticky="e", padx=4, pady=4)
        self.var_pub_project = tk.StringVar(value="")
        self.ent_pub_project = _PlaceholderEntry(
            frm, "留空默认使用打包页的项目目录（手动输入的目录优先）",
            self.var_pub_project)
        self.ent_pub_project.grid(row=0, column=1, sticky="we", padx=4)
        ttk.Button(frm, text="浏览...",
                   command=self._pick_pub_project).grid(row=0, column=2, padx=4)

        # 发行文件拆分 sdist / wheel 两个输入框，构建发行包后自动分类回填
        ttk.Label(frm, text="sdist:").grid(row=1, column=0, sticky="e", padx=4, pady=4)
        self.var_file_sdist = tk.StringVar(value=saved_sdist)
        ttk.Entry(frm, textvariable=self.var_file_sdist).grid(
            row=1, column=1, sticky="we", padx=4)
        ttk.Button(frm, text="浏览...",
                   command=self._pick_pub_sdist).grid(row=1, column=2, padx=4)

        ttk.Label(frm, text="wheel:").grid(row=2, column=0, sticky="e", padx=4, pady=4)
        self.var_file_whl = tk.StringVar(value=saved_whl)
        ttk.Entry(frm, textvariable=self.var_file_whl).grid(
            row=2, column=1, sticky="we", padx=4)
        ttk.Button(frm, text="浏览...",
                   command=self._pick_pub_whl).grid(row=2, column=2, padx=4)

        ttk.Label(frm, text="仓库:").grid(row=3, column=0, sticky="e", padx=4, pady=4)
        self.var_repo = tk.StringVar(value=saved.get("repo", "pypi"))
        cmb_repo = ttk.Combobox(frm, textvariable=self.var_repo, values=["pypi", "testpypi"],
                                state="readonly", width=12)
        cmb_repo.grid(row=3, column=1, sticky="w", padx=4)
        cmb_repo.bind("<<ComboboxSelected>>", lambda _e: self._refresh_cred_status())

        # 需求 24: 凭据来源开关（系统已配置凭据 vs 手动输入）
        self.var_cred_sys = tk.BooleanVar(value=bool(load_pypi_credentials()))
        ttk.Label(frm, text="凭据来源:").grid(row=4, column=0, sticky="e", padx=4, pady=4)
        cred_row = ttk.Frame(frm)
        cred_row.grid(row=4, column=1, sticky="w", padx=4)
        ttk.Checkbutton(cred_row, text="使用系统已配置的凭据",
                        variable=self.var_cred_sys,
                        command=self._refresh_cred_status).pack(side="left")
        self.var_cred_status = tk.StringVar()
        ttk.Label(cred_row, textvariable=self.var_cred_status,
                  foreground="#777").pack(side="left", padx=8)

        ttk.Label(frm, text="用户名:").grid(row=5, column=0, sticky="e", padx=4, pady=4)
        self.var_user = tk.StringVar(value=saved.get("username", "__token__"))
        ttk.Entry(frm, textvariable=self.var_user).grid(row=5, column=1, sticky="we", padx=4)

        ttk.Label(frm, text="密码/Token:").grid(row=6, column=0, sticky="e", padx=4, pady=4)
        self.var_token = tk.StringVar(value=saved.get("token", ""))
        ttk.Entry(frm, textvariable=self.var_token, show="*").grid(
            row=6, column=1, sticky="we", padx=4)

        row = ttk.Frame(f)
        row.pack(padx=8, pady=6, anchor="w")
        ttk.Button(row, text="加载系统配置", command=self._load_sys_credentials).pack(side="left")
        ttk.Button(row, text="保存到系统配置", command=self._save_sys_credentials).pack(side="left", padx=6)
        ttk.Button(row, text="保存配置", command=self._save_publish_cfg).pack(side="left")
        # 需求 48: 「构建发行包」按钮，构建→上传 GUI 闭环
        act_row = ttk.Frame(f)
        act_row.pack(padx=8, pady=6, anchor="w")
        ttk.Button(act_row, text="构建发行包（sdist + wheel）",
                   command=self._start_build_dist).pack(side="left")
        self.btn_pub = ttk.Button(act_row, text="发布到 PyPI",
                                  command=self._start_publish)
        self.btn_pub.pack(side="left", padx=6)
        self.var_pub_status = tk.StringVar(value="就绪")
        ttk.Label(f, textvariable=self.var_pub_status, foreground="#367bf0").pack(padx=8, anchor="w")
        ttk.Label(f, text="提示: 项目目录留空则跟随打包页的项目目录（手动输入的优先）；"
                          "「构建发行包」自动按类型回填 sdist / wheel 输入框，"
                          "「发布到 PyPI」上传两个输入框中的全部有效文件。",
                  foreground="#777").pack(padx=8, anchor="w")
        ttk.Label(f, text="系统凭据来源: 环境变量 TWINE_USERNAME/TWINE_PASSWORD 或 ~/.pypirc；"
                          "「保存到系统配置」写入 ~/.pypirc（权限 600，明文，注意机器安全）；"
                          "「保存配置」存入本地 settings.json，下次打开自动恢复。",
                  foreground="#777").pack(padx=8, anchor="w")
        self._refresh_cred_status()

    def _save_publish_cfg(self):
        from pythub.utils import load_settings, save_settings

        s = load_settings()
        s["publish"] = {
            "sdist": self.var_file_sdist.get().strip(),
            "whl": self.var_file_whl.get().strip(),
            "repo": self.var_repo.get(),
            "username": self.var_user.get().strip(),
            "token": self.var_token.get().strip(),
        }
        save_settings(s)
        self.var_pub_status.set("配置已保存 ✓（下次打开自动恢复）")

    # ------------------------------------------------ 需求 24: 系统凭据

    def _refresh_cred_status(self):
        from pythub.pypa import load_pypi_credentials

        if self.var_cred_sys.get():
            creds = load_pypi_credentials(self.var_repo.get())
            if creds:
                user = creds["username"]
                self.var_cred_status.set(
                    f"已检测: {creds['source']}（用户: {user}）")
            else:
                self.var_cred_status.set("未检测到系统凭据，发布时将提示手动输入")
        else:
            self.var_cred_status.set("使用输入框中的用户名与密码/Token")

    def _load_sys_credentials(self):
        from pythub.pypa import load_pypi_credentials

        creds = load_pypi_credentials(self.var_repo.get())
        if not creds:
            messagebox.showinfo(
                "未检测到系统凭据",
                "系统未配置 PyPI 凭据。\n\n"
                "请直接在下方输入用户名与密码/Token，发布成功后可选择"
                "「保存到系统配置」写入 ~/.pypirc，下次即可直接使用。\n\n"
                "也支持环境变量 TWINE_USERNAME / TWINE_PASSWORD。")
            self.var_cred_sys.set(False)
            self._refresh_cred_status()
            return
        self.var_user.set(creds["username"])
        self.var_token.set(creds["password"])
        self.var_cred_sys.set(True)
        self._refresh_cred_status()
        self.var_pub_status.set(f"已加载系统凭据 ✓（{creds['source']}）")

    def _save_sys_credentials(self):
        from pythub.pypa import save_pypi_credentials

        user = self.var_user.get().strip()
        token = self.var_token.get().strip()
        if not user or not token:
            messagebox.showerror("错误", "请先填写用户名与密码/Token 再保存")
            return
        if not messagebox.askyesno(
                "保存到系统配置",
                f"将把凭据以明文写入 ~/.pypirc（权限 600），继续？"):
            return
        path = save_pypi_credentials(user, token, self.var_repo.get())
        self.var_cred_sys.set(True)
        self._refresh_cred_status()
        self.var_pub_status.set(f"系统凭据已保存 ✓（{path}）")

    def _pick_pub_project(self):
        """选择发布页项目目录。"""
        d = filedialog.askdirectory(
            title="选择项目目录", initialdir=str(self.last_dir))
        if d:
            self.last_dir = Path(d)
            self.var_pub_project.set(d)

    def _pick_pub_sdist(self):
        """选择 sdist 发行包（.tar.gz/.zip），可多选，逗号分隔。"""
        # darwin 下多点扩展名（*.tar.gz）会让 Aqua Tk 转 UTType 崩溃，
        # 故 darwin 用单点 *.gz 匹配
        if sys.platform == "darwin":
            ftypes = [("sdist 包", ("*.gz", "*.zip")), ("全部文件", "*")]
        else:
            ftypes = [("sdist 包", ("*.tar.gz", "*.zip")), ("全部文件", "*")]
        files = filedialog.askopenfilenames(
            title="选择 sdist 发行包", initialdir=str(self.last_dir),
            filetypes=ftypes)
        if files:
            self.last_dir = Path(files[0]).parent
            self.var_file_sdist.set(", ".join(files))

    def _pick_pub_whl(self):
        """选择 wheel 发行包（.whl），可多选，逗号分隔。"""
        files = filedialog.askopenfilenames(
            title="选择 wheel 发行包", initialdir=str(self.last_dir),
            filetypes=[("wheel 包", ("*.whl",)), ("全部文件", "*")])
        if files:
            self.last_dir = Path(files[0]).parent
            self.var_file_whl.set(", ".join(files))

    def _pub_files(self) -> list:
        """收集 sdist/wheel 两个输入框中的有效发行文件路径（发布用）。"""
        vals = []
        for var in (self.var_file_sdist, self.var_file_whl):
            for part in var.get().split(","):
                part = part.strip()
                if part and Path(part).exists():
                    vals.append(Path(part))
        return vals

    def _start_build_dist(self):
        """需求 48: 一键构建发行包（sdist+wheel），构建→上传 GUI 闭环。

        项目目录/输出目录取打包页当前值（输出留空则用 <项目>/dist），
        后台线程执行，完成后把产物回填到「发行文件」输入框。"""
        from pythub.pypa import build_package
        from pythub.utils import get_logger

        if self.busy:
            messagebox.showinfo("提示", "有任务正在进行中，请等待完成")
            return
        # 项目目录: 手动输入优先，留空默认跟随打包页
        manual_proj = self.ent_pub_project.real_value().strip()
        build_proj = self.ent_project.real_value().strip()
        project = Path(manual_proj or build_proj)
        if not project.is_dir():
            messagebox.showerror(
                "错误", "请输入有效的项目目录（留空则使用打包页的项目目录）")
            return
        # 输出目录: 与打包页同一项目时沿用其输出目录，否则用 <项目>/dist
        if manual_proj and manual_proj != build_proj:
            out_dir = project / "dist"
        else:
            out_dir = Path(self.ent_output.real_value().strip()) or project / "dist"
        logger = get_logger()

        self.busy = True
        self.var_pub_status.set(f"正在构建发行包（{project.name} -> {out_dir}）...")

        def finish_ok(files):
            self.busy = False
            self.var_pub_status.set("构建完成 ✓")
            sdist = [f for f in files if not f.name.endswith(".whl")]
            wheels = [f for f in files if f.name.endswith(".whl")]
            if sdist:
                self.var_file_sdist.set(", ".join(str(f) for f in sdist))
            if wheels:
                self.var_file_whl.set(", ".join(str(f) for f in wheels))
            messagebox.showinfo(
                "构建成功",
                "发行包已生成:\n" + "\n".join(str(f) for f in files)
                + "\n\n已回填到 sdist / wheel 输入框，可直接点「发布到 PyPI」上传。",
            )

        def finish_err(err):
            self.busy = False
            self.var_pub_status.set("构建失败 ✗")
            messagebox.showerror("构建发行包失败", str(err))

        def work():
            try:
                files = build_package(project, out_dir, logger)
                self.root.after(0, lambda: finish_ok(files))
            except Exception as e:  # noqa: BLE001
                logger.error("构建发行包失败: %s", e)
                self.root.after(0, lambda err=e: finish_err(err))

        threading.Thread(target=work, daemon=True).start()

    def _start_publish(self):
        from pythub.pypa import load_pypi_credentials

        if self.busy:
            messagebox.showinfo("提示", "有任务正在进行中")
            return
        files = self._pub_files()
        if not files:
            messagebox.showerror("错误", "请选择有效的发行文件 (.whl/.tar.gz)")
            return

        # 凭据解析（需求 24）: 系统凭据开关优先，其次输入框；系统未配置时回退输入框
        manual = False
        username = password = None
        creds = None
        if self.var_cred_sys.get():
            creds = load_pypi_credentials(self.var_repo.get())
        if creds:
            username, password = creds["username"], creds["password"]
        else:
            manual = True
            username = self.var_user.get().strip()
            password = self.var_token.get().strip()
            if not username or not password:
                messagebox.showerror("错误", "系统未配置凭据，请输入用户名与密码/Token")
                return

        from pythub.pypa import publish
        from pythub.utils import get_logger, set_file_logging

        logger = get_logger()
        attach_queue_handler(self.q)
        # 需求 25: 发布过程日志强制落盘（cache/pythub/logs/pythub-YYYYMMDD.log）
        set_file_logging(True)
        self.busy = True
        self.var_pub_status.set("正在上传...")
        self.btn_pub.configure(state="disabled")
        repo = self.var_repo.get()

        def finish_ok():
            self.busy = False
            self.var_pub_status.set("发布成功 ✓")
            self.btn_pub.configure(state="normal")
            if manual and messagebox.askyesno(
                    "保存凭据",
                    "是否将本次输入的凭据保存到系统配置 ~/.pypirc，"
                    "下次发布时直接使用？"):
                self._save_sys_credentials()
            messagebox.showinfo("发布成功", f"已上传 {len(files)} 个文件到 {repo}")

        def finish_err(err):
            self.busy = False
            self.var_pub_status.set("发布失败 ✗")
            self.btn_pub.configure(state="normal")
            messagebox.showerror("发布失败", str(err))

        def progress(msg):
            # 上传线程 -> 主线程状态栏实时更新
            self.root.after(0, lambda m=msg: self.var_pub_status.set(m))

        def work():
            try:
                publish(files, repository=repo, username=username,
                        password=password, logger=logger, progress=progress)
                self.root.after(0, finish_ok)
            except Exception as e:  # noqa: BLE001
                logger.error("发布失败: %s", e)
                self.root.after(0, lambda err=e: finish_err(err))

        threading.Thread(target=work, daemon=True).start()


    # ------------------------------------------------ 工具管理页（需求 37）

    def _build_tab_tools(self):
        from pythub import SUPPORTED_ARCHS, SUPPORTED_PYTHON
        from pythub.tools import (
            COMPILER_CHOICES,
            ZIG_MANAGED_DIR,
            default_compiler,
            default_python,
            get_tools_logger,
        )

        # 工具管理专用日志器: 独立落盘 logs/tools-YYYYMMDD.log（需求 37/40）
        # （不再转发到主窗口日志区 —— 需求 41: 主窗口已无日志区，
        #   工具日志由「查看工具日志」弹窗展示）
        self.tools_logger = get_tools_logger()

        f = self.tab_tools

        # ---- 中: 编译器 / Python 运行时 / 打包工具，标签切换（需求 38/39）----
        sub = ttk.Notebook(f)
        sub.pack(fill="x", padx=8, pady=(8, 4))
        tab_cc = ttk.Frame(sub, padding=8)
        tab_rt = ttk.Frame(sub, padding=8)
        tab_pm = ttk.Frame(sub, padding=8)
        sub.add(tab_cc, text="编译器")
        sub.add(tab_rt, text="Python运行时")
        sub.add(tab_pm, text="打包工具")

        # 打包工具子页: rpmbuild 托管管理（需求 39）+ 其余工具链状态说明
        self.lbl_rpm_status = ttk.Label(tab_pm, text="", justify="left")
        self.lbl_rpm_status.pack(anchor="w")
        row_rpm = ttk.Frame(tab_pm)
        row_rpm.pack(anchor="w", pady=4)
        ttk.Button(row_rpm, text="安装 rpmbuild（经系统包管理器）",
                   command=self._tools_install_rpmbuild).pack(side="left")
        ttk.Button(row_rpm, text="卸载 rpmbuild",
                   command=self._tools_uninstall_rpmbuild).pack(side="left", padx=6)
        ttk.Label(tab_pm, text="RPM 引擎选 rpmbuild 时依赖此工具；缺失时自动用自研引擎（无需安装）。",
                  foreground="#777").pack(anchor="w", pady=(0, 8))

        # 编译器子页: 顶部默认编译器设置，下方三个编译器具体管理区块
        row2 = ttk.Frame(tab_cc)
        row2.pack(anchor="w")
        ttk.Label(row2, text="默认编译器:").pack(side="left")
        self.var_default_cc = tk.StringVar(value=default_compiler())
        ttk.Combobox(row2, textvariable=self.var_default_cc,
                     values=list(COMPILER_CHOICES), state="readonly", width=8).pack(
            side="left", padx=6)
        ttk.Button(row2, text="应用", command=self._tools_set_default_cc).pack(side="left")
        ttk.Label(tab_cc, text="auto=自动探测（mingw→zig→cc）；zig/mingw/cc=强制优先该编译器",
                  foreground="#777").pack(anchor="w")

        # zig 区块（交叉编译 linux/macos pexe）
        ttk.Separator(tab_cc, orient="horizontal").pack(fill="x", pady=8)
        self.lbl_zig_status = ttk.Label(tab_cc, text="", justify="left")
        self.lbl_zig_status.pack(anchor="w")
        row1 = ttk.Frame(tab_cc)
        row1.pack(anchor="w", pady=4)
        ttk.Button(row1, text="安装 zig（下载到 pythub 托管目录）",
                   command=self._tools_install_zig).pack(side="left")
        ttk.Button(row1, text="卸载托管 zig",
                   command=self._tools_uninstall_zig).pack(side="left", padx=6)
        ttk.Label(tab_cc, text="交叉编译（linux/macos pexe，以及没有 mingw 时的 Windows exe）依赖 zig。",
                  foreground="#777", wraplength=660, justify="left").pack(
            anchor="w", pady=(2, 4))

        # mingw-w64 区块（Windows PE 交叉编译）
        ttk.Separator(tab_cc, orient="horizontal").pack(fill="x", pady=8)
        self.lbl_mingw_status = ttk.Label(tab_cc, text="", justify="left")
        self.lbl_mingw_status.pack(anchor="w")
        row_mingw = ttk.Frame(tab_cc)
        row_mingw.pack(anchor="w", pady=4)
        ttk.Button(row_mingw, text="安装 mingw-w64（经系统包管理器）",
                   command=self._tools_install_mingw).pack(side="left")
        ttk.Button(row_mingw, text="卸载 mingw-w64",
                   command=self._tools_uninstall_mingw).pack(side="left", padx=6)
        ttk.Label(tab_cc, text="Windows PE 优先 mingw-w64 编译引导器；缺失时自动改用 zig cc。",
                  foreground="#777").pack(anchor="w")

        # cc 区块（系统原生编译器，POSIX 本机编译兜底）
        ttk.Separator(tab_cc, orient="horizontal").pack(fill="x", pady=8)
        self.lbl_cc_status = ttk.Label(tab_cc, text="", justify="left")
        self.lbl_cc_status.pack(anchor="w")
        row_cc = ttk.Frame(tab_cc)
        row_cc.pack(anchor="w", pady=4)
        ttk.Button(row_cc, text="安装 cc（经系统包管理器）",
                   command=self._tools_install_cc).pack(side="left")
        ttk.Button(row_cc, text="卸载 cc",
                   command=self._tools_uninstall_cc).pack(side="left", padx=6)
        ttk.Label(tab_cc, text="POSIX 本机编译引导器的兜底编译器（clang/gcc）；缺失时自动改用 zig cc。",
                  foreground="#777").pack(anchor="w")

        # Python 运行时子页
        self.tree_runtimes = ttk.Treeview(
            tab_rt, columns=("name", "size"), show="headings", height=8)
        self.tree_runtimes.heading("name", text="缓存条目")
        self.tree_runtimes.heading("size", text="大小")
        self.tree_runtimes.column("name", width=430)
        self.tree_runtimes.column("size", width=90, anchor="e")
        self.tree_runtimes.pack(fill="x")
        row3 = ttk.Frame(tab_rt)
        row3.pack(anchor="w", pady=4)
        ttk.Label(row3, text="下载:").pack(side="left")
        try:
            from pythub.runtime import detect_current as _detect
            cur_plat, cur_arch, _ = _detect()
        except Exception:  # noqa: BLE001
            cur_plat, cur_arch = "macos", "x86_64"
        self.var_rt_platform = tk.StringVar(value=cur_plat)
        ttk.Combobox(row3, textvariable=self.var_rt_platform,
                     values=["macos", "linux", "windows"], state="readonly",
                     width=8).pack(side="left", padx=4)
        self.var_rt_arch = tk.StringVar(value=cur_arch)
        ttk.Combobox(row3, textvariable=self.var_rt_arch,
                     values=list(SUPPORTED_ARCHS), state="readonly", width=9).pack(
            side="left", padx=4)
        self.var_rt_dl_py = tk.StringVar(value=default_python())
        ttk.Combobox(row3, textvariable=self.var_rt_dl_py,
                     values=list(SUPPORTED_PYTHON), state="readonly", width=7).pack(
            side="left", padx=4)
        ttk.Button(row3, text="下载运行时",
                   command=self._tools_download_runtime).pack(side="left", padx=8)
        ttk.Button(row3, text="移除选中",
                   command=self._tools_remove_runtime).pack(side="left")
        row4 = ttk.Frame(tab_rt)
        row4.pack(anchor="w")
        ttk.Label(row4, text="默认 Python 版本:").pack(side="left")
        self.var_rt_pyver = tk.StringVar(value=default_python())
        ttk.Combobox(row4, textvariable=self.var_rt_pyver,
                     values=list(SUPPORTED_PYTHON), state="readonly", width=7).pack(
            side="left", padx=6)
        ttk.Button(row4, text="设为默认",
                   command=self._tools_set_default_python).pack(side="left")

        # 运行时下载源切换（需求: GUI 可配置 runtime.mirror）
        from pythub.runtime import GITHUB_RELEASE_BASE, NPM_MIRROR_BASE
        from pythub.utils import load_settings
        self._rt_auto_label = "自动（GitHub → npmmirror 镜像兜底）"
        self._rt_source_map = {
            self._rt_auto_label: "",
            "GitHub 官方": GITHUB_RELEASE_BASE,
            "npmmirror 镜像（大陆加速）": NPM_MIRROR_BASE,
            "自定义...": None,
        }
        _cur_mirror = str(load_settings().get("runtime", {}).get("mirror", ""))
        if _cur_mirror:
            _cur_src = next(
                (k for k, v in self._rt_source_map.items() if v == _cur_mirror),
                "自定义...")
        else:
            _cur_src = self._rt_auto_label
        row_rt_src = ttk.Frame(tab_rt)
        row_rt_src.pack(anchor="w", pady=(6, 0))
        ttk.Label(row_rt_src, text="下载源:").pack(side="left")
        self.var_rt_source = tk.StringVar(value=_cur_src)
        _src_cb = ttk.Combobox(row_rt_src, textvariable=self.var_rt_source,
                               values=list(self._rt_source_map),
                               state="readonly", width=32)
        _src_cb.pack(side="left", padx=6)
        # ttk.Combobox 无 command 选项，选择变化走虚拟事件
        _src_cb.bind("<<ComboboxSelected>>", lambda _e: self._tools_apply_source())
        ttk.Label(tab_rt,
                  text="自动: GitHub 失败自动切换 npmmirror 镜像（推荐大陆网络）；"
                       "「自定义...」可填入自建镜像的 base URL，选择后立即保存到 settings.json。",
                  foreground="#777", wraplength=660, justify="left").pack(
            anchor="w", pady=(2, 0))
        ttk.Label(tab_rt, text="打包页「Python 版本」初始值即取默认版本设置；"
                               "「设为默认」会持久化到 settings.json。",
                  foreground="#777").pack(anchor="w")

        # ---- 下: 刷新/日志按钮 + 工具链状态清单（需求 40）----
        self.var_tools_status = tk.StringVar(value="就绪")
        row5 = ttk.Frame(f)
        row5.pack(anchor="w", padx=8, pady=4)
        ttk.Button(row5, text="刷新工具状态",
                   command=self._tools_refresh_all).pack(side="left")
        ttk.Button(row5, text="查看工具日志",
                   command=self._tools_show_log).pack(side="left", padx=6)
        ttk.Label(row5, textvariable=self.var_tools_status,
                  foreground="#367bf0").pack(side="left", padx=8)
        frm4 = ttk.LabelFrame(f, text="工具链状态清单", padding=8)
        frm4.pack(fill="x", padx=8, pady=4)
        self.lbl_pkg_tools = ttk.Label(frm4, text="", justify="left")
        self.lbl_pkg_tools.pack(anchor="w")
        ttk.Label(f, text=f"以上所有操作（下载/安装/卸载/移除/设默认）均写入独立日志文件: "
                          f"logs/tools-YYYYMMDD.log（托管 zig 目录: {ZIG_MANAGED_DIR}）",
                  foreground="#777", wraplength=680, justify="left").pack(
            anchor="w", padx=8, pady=(0, 8))
        self._tools_refresh_all()

    def _tools_show_log(self):
        """工具管理日志独立弹窗（需求 40/41）: 文件内容自动刷新。"""
        from pythub.tools import tools_log_file_path
        from pythub.utils import LOG_DIR

        path = tools_log_file_path()
        if self._tools_log_win is not None and self._tools_log_win.win.winfo_exists():
            self._tools_log_win.win.lift()
            self._tools_log_win.win.focus_set()
            return

        w = _LogWindow(self.root, f"工具管理日志 — {path.name}",
                       f"日志文件: {path}（自动刷新）", file_path=path)
        ttk.Button(w.bar, text="刷新", command=w.reload).pack(side="left", padx=8)
        ttk.Button(w.bar, text="打开日志目录",
                   command=lambda: self._open_dir(LOG_DIR)).pack(side="left")
        self._tools_log_win = w

    def _show_build_log(self):
        """打包日志独立弹窗（需求 41）: 打开时回放缓冲区，之后实时增量显示。"""
        if self._build_log_win is not None and self._build_log_win.win.winfo_exists():
            self._build_log_win.win.lift()
            self._build_log_win.win.focus_set()
            return
        w = _LogWindow(self.root, "打包日志", "打包 / 发布过程日志（实时自动刷新）")
        w.append_lines(list(self._log_buffer))
        self._build_log_win = w

    # ---- 工具管理: 状态刷新 ----

    def _tools_refresh_all(self):
        from pythub.tools import cc_status, mingw_status, packaging_tools_status, rpmbuild_status, zig_status

        # 编译器子页: cc 状态行
        c = cc_status()
        if c["found"]:
            self.lbl_cc_status.config(
                text=f"cc（系统原生编译器）: ✓ 已安装  版本: {(c['version'] or '未知').splitlines()[0]}\n"
                     f"      路径: {c['paths'][0]}")
        else:
            manage = "可一键安装" if c["can_manage"] else \
                "（本机未找到 brew/apt/dnf/yum，请手动安装）"
            self.lbl_cc_status.config(
                text=f"cc（系统原生编译器）: ✗ 未安装（{manage}）")

        # 编译器子页: mingw 状态行
        m = mingw_status()
        if m["found"]:
            self.lbl_mingw_status.config(
                text=f"mingw-w64: ✓ 已安装  版本: {m['version'] or '未知'}\n"
                     f"      路径: {m['paths'][0]}")
        else:
            manage = "可一键安装" if m["can_manage"] else \
                "（本机未找到 brew/apt/dnf/yum，请手动安装）"
            self.lbl_mingw_status.config(
                text=f"mingw-w64: ✗ 未安装（{manage}）")

        # rpmbuild 专属状态行（需求 39）
        rpm = rpmbuild_status()
        if rpm["found"]:
            self.lbl_rpm_status.config(
                text=f"rpmbuild: ✓ 已安装 — {rpm['path']}")
        else:
            manage = "可一键安装" if rpm["can_manage"] else "（本机未找到 brew/apt/dnf/yum/zypper，请手动安装）"
            self.lbl_rpm_status.config(
                text=f"rpmbuild: ✗ 未安装（{manage}）")

        z = zig_status()
        if z["found"]:
            tag = "pythub 托管" if z["managed"] else "系统安装"
            self.lbl_zig_status.config(
                text=f"zig: ✓ 已安装（{tag}）  版本: {z['version']}\n"
                     f"      路径: {z['path']}")
        else:
            self.lbl_zig_status.config(
                text="zig: ✗ 未安装（交叉编译 linux/macos pexe 及无 mingw 时的 Windows exe 需要）")

        lines = []
        for t in packaging_tools_status():
            if t["found"]:
                lines.append(f"[✓] {t['label']} — {t['path']}")
            else:
                lines.append(f"[✗] {t['label']} — 未安装（{t['hint']}）")
        self.lbl_pkg_tools.config(text="\n".join(lines))

        # 运行时缓存列表（iid = 缓存目录路径，供移除使用）
        from pythub.tools import list_runtimes
        self.tree_runtimes.delete(*self.tree_runtimes.get_children())
        for it in list_runtimes():
            self.tree_runtimes.insert(
                "", "end", iid=str(it["path"]),
                values=(it["name"], self._fmt_size(it["size"])))

    # ---- 工具管理: 后台任务 ----

    def _tools_task(self, status_text: str, fn, ok_msg: str = "完成 ✓"):
        """耗时工具操作跑后台线程（界面不卡顿），结束/失败后刷新状态。"""
        if self.busy:
            messagebox.showinfo("提示", "有任务正在进行中")
            return
        self.busy = True
        self.var_tools_status.set(status_text)
        logger = self.tools_logger

        def done_ok(result):
            self.busy = False
            self.var_tools_status.set(ok_msg)
            self._tools_refresh_all()
            if result:
                messagebox.showinfo("完成", str(result))

        def done_err(err):
            self.busy = False
            self.var_tools_status.set("操作失败 ✗")
            messagebox.showerror("工具管理", str(err))

        def work():
            try:
                result = fn()
                self.root.after(0, lambda: done_ok(result))
            except Exception as e:  # noqa: BLE001
                logger.error("工具管理操作失败: %s", e)
                self.root.after(0, lambda err=e: done_err(err))

        threading.Thread(target=work, daemon=True).start()

    # ---- 工具管理: 具体操作 ----

    def _tools_install_zig(self):
        from pythub.tools import ZIG_MANAGED_DIR

        if not messagebox.askyesno(
                "安装 zig",
                f"将下载最新稳定版 zig 并安装到 pythub 托管目录:\n{ZIG_MANAGED_DIR}\n\n"
                "安装后构建时自动识别，无需配置 PATH。继续？"):
            return
        from pythub.tools import install_zig

        def _do():
            binary = install_zig(logger=self.tools_logger)
            return (f"zig 已下载并安装完成 ✓\n"
                    f"安装路径: {binary}\n"
                    "构建时自动识别（PYTHUB_ZIG > 托管目录 > PATH），无需配置 PATH")

        self._tools_task(
            "正在下载/安装 zig ...", _do,
            ok_msg="zig 已下载并安装完成 ✓")

    def _tools_uninstall_zig(self):
        from pythub.tools import uninstall_zig

        if not messagebox.askyesno(
                "卸载 zig",
                "仅卸载 pythub 托管安装的 zig（系统其他位置安装的不受影响）。继续？"):
            return

        def _do():
            uninstall_zig(logger=self.tools_logger)
            return "pythub 托管 zig 已卸载 ✓\n（如需重新安装，随时点「安装 zig」即可）"

        self._tools_task(
            "正在卸载托管 zig ...", _do,
            ok_msg="托管 zig 已卸载 ✓")

    def _tools_set_default_cc(self):
        from pythub.tools import set_default_compiler

        set_default_compiler(self.var_default_cc.get(), logger=self.tools_logger)
        self.var_tools_status.set(f"默认编译器已设为 {self.var_default_cc.get()} ✓")

    def _tools_download_runtime(self):
        from pythub.tools import download_runtime

        plat, arch, pyver = (
            self.var_rt_platform.get(), self.var_rt_arch.get(), self.var_rt_dl_py.get())

        def _do():
            path = download_runtime(plat, arch, pyver, logger=self.tools_logger)
            return (f"Python 运行时下载完成 ✓\n"
                    f"版本: Python {pyver} ({plat}/{arch})\n"
                    f"缓存路径: {path}")

        self._tools_task(
            f"正在下载运行时 Python {pyver} ({plat}/{arch}) ...", _do,
            ok_msg=f"运行时 Python {pyver} ({plat}/{arch}) 就绪 ✓")

    def _tools_remove_runtime(self):
        from pythub.tools import remove_runtime

        sel = self.tree_runtimes.selection()
        if not sel:
            messagebox.showinfo("提示", "请先在列表中选中要移除的运行时缓存")
            return
        path = Path(sel[0])
        if not messagebox.askyesno(
                "确认移除", f"移除运行时缓存:\n{path}\n\n下次打包用到时会自动重新下载。"):
            return

        def _do():
            remove_runtime(path, logger=self.tools_logger)
            return f"运行时缓存已移除 ✓\n{path.name}\n下次打包用到时会自动重新下载"

        self._tools_task(
            f"正在移除 {path.name} ...", _do,
            ok_msg="运行时缓存已移除 ✓")

    def _tools_set_default_python(self):
        from pythub.tools import set_default_python

        set_default_python(self.var_rt_pyver.get(), logger=self.tools_logger)
        self.var_rt_dl_py.set(self.var_rt_pyver.get())
        self.var_tools_status.set(f"默认 Python 版本已设为 {self.var_rt_pyver.get()} ✓")

    def _tools_apply_source(self):
        """应用运行时下载源选择（立即持久化到 settings.json runtime.mirror）。"""
        from pythub.runtime import runtime_sources
        from pythub.utils import load_settings, save_settings

        label = self.var_rt_source.get()
        if label == "自定义...":
            import tkinter.simpledialog as simpledialog

            url = simpledialog.askstring(
                "自定义下载源",
                "输入镜像 base URL（目录形式，如 https://registry.npmmirror.com"
                "/-/binary/python-build-standalone）。\n留空恢复自动模式:",
                initialvalue=load_settings().get("runtime", {}).get("mirror", ""),
                parent=self.root)
            if url is None:
                return  # 取消
            mirror = url.strip().rstrip("/")
            if not mirror:
                self.var_rt_source.set(self._rt_auto_label)
                mirror = ""
        else:
            mirror = self._rt_source_map.get(label, "")

        settings = load_settings()
        settings.setdefault("runtime", {})["mirror"] = mirror
        save_settings(settings)
        self.tools_logger.info("运行时下载源已切换: %s", label)
        self.var_tools_status.set(
            f"运行时下载源: {label}（生效: {', '.join(runtime_sources())}）")

    def _tools_install_rpmbuild(self):
        from pythub.tools import install_rpmbuild

        if not messagebox.askyesno(
                "安装 rpmbuild",
                "将通过系统包管理器安装 rpmbuild:\n"
                "  macOS: brew install rpm（免 sudo）\n"
                "  Linux: sudo apt/dnf/yum/zypper（需要输入 sudo 密码，"
                "若当前无法交互输入密码请改在终端手动执行）\n\n继续？"):
            return

        def _do():
            return install_rpmbuild(logger=self.tools_logger)

        self._tools_task(
            "正在安装 rpmbuild ...", _do,
            ok_msg="rpmbuild 安装完成 ✓")

    def _tools_uninstall_rpmbuild(self):
        from pythub.tools import uninstall_rpmbuild

        if not messagebox.askyesno(
                "卸载 rpmbuild",
                "将通过系统包管理器卸载 rpmbuild。\n"
                "（缺失时 rpm 格式仍可自动改用自研引擎，不影响打包）\n\n继续？"):
            return

        def _do():
            return uninstall_rpmbuild(logger=self.tools_logger)

        self._tools_task(
            "正在卸载 rpmbuild ...", _do,
            ok_msg="rpmbuild 已卸载 ✓")

    def _tools_install_mingw(self):
        from pythub.tools import install_mingw

        if not messagebox.askyesno(
                "安装 mingw-w64",
                "将通过系统包管理器安装 mingw-w64:\n"
                "  macOS: brew install mingw-w64（免 sudo）\n"
                "  Linux: sudo apt/dnf/yum（需要输入 sudo 密码，"
                "若当前无法交互输入密码请改在终端手动执行）\n\n继续？"):
            return

        def _do():
            return install_mingw(logger=self.tools_logger)

        self._tools_task(
            "正在安装 mingw-w64 ...", _do,
            ok_msg="mingw-w64 安装完成 ✓")

    def _tools_uninstall_mingw(self):
        from pythub.tools import uninstall_mingw

        if not messagebox.askyesno(
                "卸载 mingw-w64",
                "将通过系统包管理器卸载 mingw-w64。\n"
                "（缺失时 Windows PE 交叉编译将自动改用 zig cc）\n\n继续？"):
            return

        def _do():
            return uninstall_mingw(logger=self.tools_logger)

        self._tools_task(
            "正在卸载 mingw-w64 ...", _do,
            ok_msg="mingw-w64 已卸载 ✓")

    def _tools_install_cc(self):
        from pythub.tools import install_cc

        if not messagebox.askyesno(
                "安装 cc",
                "将通过系统包管理器安装本机 C 编译器:\n"
                "  macOS: brew install gcc（免 sudo）\n"
                "  Linux: sudo apt/dnf/yum（需要输入 sudo 密码，"
                "若当前无法交互输入密码请改在终端手动执行）\n\n继续？"):
            return

        def _do():
            return install_cc(logger=self.tools_logger)

        self._tools_task(
            "正在安装 cc ...", _do,
            ok_msg="cc 已安装 ✓")

    def _tools_uninstall_cc(self):
        from pythub.tools import uninstall_cc

        if not messagebox.askyesno(
                "卸载 cc",
                "将通过系统包管理器卸载本机 C 编译器。\n"
                "（缺失时 POSIX 本机编译将自动改用 zig / mingw）\n\n继续？"):
            return

        def _do():
            return uninstall_cc(logger=self.tools_logger)

        self._tools_task(
            "正在卸载 cc ...", _do,
            ok_msg="cc 已卸载 ✓")

    # ------------------------------------------------ 设置页

    def _build_tab_settings(self):
        from pythub.utils import (
            LOG_DIR,
            cache_max_bytes,
            load_settings,
            set_file_logging,
        )

        f = self.tab_settings
        frm = ttk.Frame(f)
        frm.pack(fill="x", padx=8, pady=8)

        # 日志记录开关（需求 17/18）：按持久化设置初始化
        self.var_logfile = tk.BooleanVar(value=bool(load_settings().get("log_to_file")))
        if self.var_logfile.get():
            set_file_logging(True)
        ttk.Checkbutton(
            frm, text="启用日志记录（按天写入文件）",
            variable=self.var_logfile, command=self._toggle_file_log,
        ).pack(anchor="w", padx=4, pady=(6, 2))
        ttk.Label(frm, text=f"日志目录: {LOG_DIR}", foreground="#777").pack(
            anchor="w", padx=8)
        ttk.Label(frm, text="开启后，打包/发布等操作日志会同步记录到日志目录下"
                            "（pythub-YYYYMMDD.log），重启后自动恢复。",
                  foreground="#777", wraplength=680, justify="left").pack(
            anchor="w", padx=8, pady=(2, 10))

        row = ttk.Frame(f)
        row.pack(anchor="w", padx=8)
        ttk.Button(row, text="打开日志目录",
                   command=lambda: self._open_dir(LOG_DIR)).pack(side="left")
        ttk.Button(row, text="复制日志目录路径",
                   command=lambda: self._copy_to_clipboard(LOG_DIR)).pack(
            side="left", padx=6)

        # ---- 本地缓存管理（需求 14: 缓存大小上限） ----
        frm2 = ttk.LabelFrame(f, text="本地缓存管理", padding=8)
        frm2.pack(fill="x", padx=8, pady=8)
        self.lbl_cache_stats = ttk.Label(frm2, text="", justify="left")
        self.lbl_cache_stats.pack(anchor="w")
        ttk.Label(frm2, text="构建缓存 = runtimes（嵌入式解释器）/ pyzlib（依赖 zip）/"
                             "bootloader（引导器编译缓存）；运行时用户缓存（exe/、run/）"
                             "由引导器自动 LRU 管理，不在此列。",
                  foreground="#777", wraplength=680, justify="left").pack(
            anchor="w", pady=(2, 6))
        btnrow = ttk.Frame(frm2)
        btnrow.pack(anchor="w")
        ttk.Button(btnrow, text="刷新统计", command=self._refresh_cache_stats).pack(side="left")
        ttk.Button(btnrow, text="打开缓存目录",
                   command=lambda: self._open_dir(PYTHUB_CACHE_DIR)).pack(side="left", padx=6)
        ttk.Button(btnrow, text="按上限清理（LRU）", command=self._prune_cache).pack(
            side="left", padx=6)

        # 缓存大小上限（需求 47）: settings.json cache.max_mb，默认 4096 MB
        row_cap = ttk.Frame(frm2)
        row_cap.pack(anchor="w", pady=(6, 0))
        ttk.Label(row_cap, text="缓存大小上限 (MB):").pack(side="left")
        self.var_cache_cap = tk.StringVar(value=str(cache_max_bytes() >> 20))
        ttk.Spinbox(row_cap, from_=64, to=1048576, increment=256,
                    textvariable=self.var_cache_cap, width=8).pack(
            side="left", padx=6)
        ttk.Button(row_cap, text="保存上限",
                   command=self._save_cache_cap).pack(side="left")
        ttk.Label(frm2, text="「按上限清理」与引导器运行缓存驱逐均按此上限执行（LRU）；"
                             "默认 4096 MB = 4GB，保存后持久化到 settings.json。",
                  foreground="#777", wraplength=680, justify="left").pack(
            anchor="w", pady=(2, 0))
        self._refresh_cache_stats()

        # ---- 证书管理（需求 57）----
        from pythub.signing import CERT_DIR

        frm_cert = ttk.LabelFrame(f, text="证书管理", padding=8)
        frm_cert.pack(fill="x", padx=8, pady=8)
        ttk.Label(frm_cert, text=f"自签证书缓存目录: {CERT_DIR}", justify="left").pack(
            anchor="w")
        ttk.Label(frm_cert,
                  text="Windows .exe Authenticode 自动自签证书"
                       "（selfsigned.crt / selfsigned.key）存储于此，10 年有效期；"
                       "清理后下次签名时自动重新生成。打包页「证书管理」弹窗亦可维护；"
                       "deb/rpm 签名使用 gpg 钥匙串密钥，在「签名管理」弹窗中选择。",
                  foreground="#777", wraplength=680, justify="left").pack(
            anchor="w", pady=(2, 6))
        row_cert = ttk.Frame(frm_cert)
        row_cert.pack(anchor="w")
        ttk.Button(row_cert, text="打开证书目录",
                   command=self._open_cert_cache_dir).pack(side="left")
        ttk.Button(row_cert, text="清理自签证书",
                   command=self._clean_selfsigned_cert).pack(side="left", padx=6)

    def _save_cache_cap(self):
        from pythub.utils import cache_max_bytes, set_cache_max_mb

        raw = self.var_cache_cap.get().strip()
        try:
            mb = int(raw)
        except ValueError:
            messagebox.showerror("缓存上限", f"请输入正整数（MB），当前输入: {raw!r}")
            return
        if mb <= 0:
            messagebox.showerror("缓存上限", "缓存上限必须为正整数（MB）")
            return
        try:
            set_cache_max_mb(mb)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("缓存上限", f"保存失败: {e}")
            return
        self.var_cache_cap.set(str(cache_max_bytes() >> 20))
        messagebox.showinfo("缓存上限", f"缓存大小上限已保存为 {mb} MB ✓\n"
                                        "（立即对「按上限清理」生效）")

    @staticmethod
    def _fmt_size(n: float) -> str:
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024:
                return f"{int(n)} {unit}" if unit == "B" else f"{n:.1f} {unit}"
            n /= 1024.0
        return f"{n:.1f} PB"

    def _refresh_cache_stats(self):
        from pythub.utils import PYTHUB_CACHE_DIR, cache_max_bytes, cache_stats

        stats = cache_stats()
        cap = cache_max_bytes()
        lines = [f"缓存目录: {PYTHUB_CACHE_DIR}"]
        lines += [f"  {sub}: {self._fmt_size(size)}" for sub, size in stats.items()]
        total = sum(stats.values())
        lines.append(f"  合计: {self._fmt_size(total)} / 上限 {self._fmt_size(cap)}")
        self.lbl_cache_stats.config(text="\n".join(lines))

    def _prune_cache(self):
        from pythub.utils import cache_max_bytes, prune_cache

        cap_mb = cache_max_bytes() >> 20
        cap = (f"{cap_mb} MB" if cap_mb < 1024
               else f"{cap_mb / 1024:.1f} GB")
        if not messagebox.askyesno(
                "确认清理",
                f"按 LRU 把构建缓存裁剪到上限（{cap}）？\n"
                "最久未使用的解释器/依赖缓存会被删除（下次打包自动重新下载）。\n"
                "日志与设置不受影响。"):
            return
        pruned = prune_cache(cache_max_bytes())
        self._refresh_cache_stats()
        if pruned:
            messagebox.showinfo(
                "清理完成",
                f"已清理 {len(pruned)} 个缓存条目:\n"
                + "\n".join(f"  - {p.name}" for p in pruned[:10])
                + ("\n  ..." if len(pruned) > 10 else ""))
        else:
            messagebox.showinfo("清理完成", "缓存未超上限，无需清理。")

    def _toggle_file_log(self):
        from pythub.utils import set_file_logging

        if self.var_logfile.get():
            path = set_file_logging(True)
            self.q.put((f"[INFO] 日志记录已开启: {path}\n", "INFO"))
        else:
            set_file_logging(False)
            self.q.put(("[INFO] 日志记录已关闭\n", "INFO"))

    def _open_dir(self, d):
        import subprocess

        d = str(d)
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", d])
            elif os.name == "nt":
                os.startfile(d)  # noqa: S606
            else:
                subprocess.Popen(["xdg-open", d])
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("错误", f"无法打开目录: {e}")

    # ------------------------------------------------ 关于页

    def _build_tab_about(self):
        import platform

        from pythub import __author__, __url__, __version__

        f = self.tab_about
        wrap = ttk.Frame(f)
        wrap.pack(expand=True, fill="both")

        def row(label: str, value: str, link: bool = False):
            fr = ttk.Frame(wrap)
            fr.pack(fill="x", pady=3)
            ttk.Label(fr, text=label, foreground="#777", width=10,
                      anchor="e").pack(side="left", padx=(0, 8))
            if link:
                lbl = ttk.Label(fr, text=value, foreground="#367bf0", cursor="hand2")
                lbl.pack(side="left")
                lbl.bind("<Button-1>", lambda e: self._open_url(value))
            else:
                ttk.Label(fr, text=value).pack(side="left")

        ttk.Label(wrap, text="Pythub", font=("Helvetica", 40, "bold")).pack(pady=(40, 20))
        ttk.Label(wrap, text="Pythub —— 开源的基于 Python 的 Python 应用打包工具",
                  font=("Helvetica", 12)).pack(pady=(0, 24))

        row("版本", __version__)
        row(
            "介绍",
            "一条命令，把应用打包成免装 Python、可直接运行的产物："
            "跨平台、多 Python 版本、多架构，并支持打包/发布 PyPI 包",
        )
        row("系统信息", f"{platform.system()} {platform.release()} {platform.machine()}")
        row("运行环境", f"Python {platform.python_version()}  |  Tk {tk.TkVersion}")
        row("官网", __url__, link=True)
        row("开源协议", "BSD 3-Clause License")
        row("版权所有", f"Copyright © {__author__}")
        ttk.Label(wrap, text="开源组织 CROGRAM 出品", foreground="#999").pack(pady=18)

    def _open_url(self, url: str):
        import subprocess

        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", url])
            elif sys.platform == "win32":
                os.startfile(url)  # noqa: S606
            else:
                subprocess.Popen(["xdg-open", url])
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("错误", f"无法打开链接: {e}")


# ---------------------------------------------------------------- 全局日志桥接

def attach_queue_handler(log_queue: "queue.Queue") -> None:
    """把 pythub 日志转发到指定队列（显式传入，避免全局状态混乱）。"""
    logger = logging.getLogger("pythub")
    for h in logger.handlers:
        if isinstance(h, _QueueHandler):
            h.q = log_queue  # 复用，更新目标队列
            return
    handler = _QueueHandler(log_queue)
    handler.setFormatter(logging.Formatter("[%(levelname).1s] %(message)s"))
    handler.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    # 关键修复: 显式设置级别，确保 INFO 日志不会被 root logger 的 WARNING 级别吞掉
    if logger.level == logging.NOTSET or logger.level > logging.INFO:
        logger.setLevel(logging.INFO)


def _tk_install_hint() -> str:
    """按当前系统返回 Tk 缺失时的安装建议。"""
    if sys.platform == "darwin":
        ver = f"{sys.version_info.major}.{sys.version_info.minor}"
        return (
            f"macOS: brew install python-tk@{ver}（Homebrew 安装的 Python 需单独装 Tk）\n"
            "       或安装 python.org 官方解释器（自带 Tk）"
        )
    if sys.platform == "win32":
        return (
            "Windows: 官方 python.org 安装包默认自带 Tk，重新运行安装程序勾选\n"
            "         \"tcl/tk and IDLE\" 组件即可"
        )
    if sys.platform.startswith("linux"):
        return (
            "Linux (Debian/Ubuntu): sudo apt install python3-tk\n"
            "  (Fedora/RHEL):        sudo dnf install python3-tkinter\n"
            "  (Arch):               sudo pacman -S tk"
        )
    return "请安装包含 Tk 支持的 Python 解释器"


def main(debug: bool = False) -> int:
    if not _TK_OK:
        print(
            "错误: 当前 Python 未编译 Tk 支持，无法启动 GUI。\n"
            f"详情: {_TK_ERROR}\n"
            f"建议: {_tk_install_hint()}\n"
            "或改用命令行: pythub build ..."
        )
        return 1

    # 默认后台运行: 派生独立进程启动 GUI，控制台命令立即返回（关闭终端/Ctrl+C
    # 不影响 GUI）。pythub gui --debug（或设 PYTHUB_GUI_DEBUG=1）可前台运行，
    # 日志输出到终端，调试用（Ctrl+C 退出）。
    if not os.environ.get("PYTHUB_GUI_CHILD") \
            and not (debug or os.environ.get("PYTHUB_GUI_DEBUG")):
        env = {**os.environ, "PYTHUB_GUI_CHILD": "1"}
        if "__pythub_memdata__" in sys.modules:
            # mem 单文件二进制: 解释器无法重新执行内存载荷，
            # 改为自再执行二进制自身（bootex 引导器会重新注入内存载荷，
            # 子进程设 PYTHUB_GUI_CHILD=1 后直接进 Tk 主循环）
            child_cmd = [sys.argv[0], "gui"]
        else:
            child_cmd = [sys.executable, "-m", "pythub.gui"]
        if os.name == "posix":
            subprocess.Popen(
                child_cmd,
                env=env, start_new_session=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        elif os.name == "nt":
            subprocess.Popen(
                child_cmd, env=env,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.DETACHED_PROCESS,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            subprocess.Popen(child_cmd, env=env)
        return 0

    root = tk.Tk()
    log_queue: "queue.Queue" = queue.Queue()
    attach_queue_handler(log_queue)
    PythubGUI(root, log_queue)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
