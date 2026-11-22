"""Pythub 可视化操作界面（tkinter，零第三方 UI 依赖）。

- 选择打包的 Python 脚本/项目、目标平台、架构、Python 版本
- 按平台列出可用文件类型（单文件/文件夹/平台专属格式）
- 执行打包（后台线程，界面不卡顿）并实时输出日志
- 打包成功弹窗提示产物路径
- 发布打包好的应用/发行包到 PyPI
"""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

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


class PythubGUI:
    def __init__(self, root: "tk.Tk", log_queue: "queue.Queue"):
        self.root = root
        self.q = log_queue
        self.busy = False
        # 文件对话框默认目录：用户 home；选择后记住上次目录
        self.last_dir = Path.home()

        root.title("Pythub")
        root.geometry("880x680")

        nb = ttk.Notebook(root)
        self.nb = nb
        nb.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        self.tab_build = ttk.Frame(nb)
        self.tab_publish = ttk.Frame(nb)
        self.tab_settings = ttk.Frame(nb)
        self.tab_about = ttk.Frame(nb)
        nb.add(self.tab_build, text="  打包  ")
        nb.add(self.tab_publish, text="  发布 PyPI  ")
        nb.add(self.tab_settings, text="  设置  ")
        nb.add(self.tab_about, text="  关于  ")
        self._build_tab_build()
        self._build_tab_publish()
        self._build_tab_settings()
        self._build_tab_about()
        self._build_menubar()

        self.log = tk.Text(root, height=15, state="disabled",
                           bg="#141414", fg="#dcdcdc")
        self.log.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        for tag, color in (("WARNING", "#c08000"), ("ERROR", "#e05555"), ("INFO", "#dcdcdc")):
            self.log.tag_config(tag, foreground=color)
        self.log.delete("1.0", "end")  # 清掉默认空行，首条日志从第 1 行开始
        nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)
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

    # ------------------------------------------------ 标签切换

    def _on_tab_changed(self, _event=None):
        """切到"关于"页时隐藏底部日志区，切回其他页时恢复（问题9）。"""
        if self.nb.select() == str(self.tab_about):
            self.log.pack_forget()
        else:
            self.log.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    # ------------------------------------------------ 日志

    def _poll(self):
        """主线程定时取出队列日志并显示（批量插入，保证界面流畅）。"""
        lines = []
        try:
            for _ in range(200):
                text, level = self.q.get_nowait()
                tag = level if level in ("WARNING", "ERROR", "INFO") else "INFO"
                lines.append((text, tag))
        except queue.Empty:
            pass
        if lines:
            self.log.configure(state="normal")
            for text, tag in lines:
                self.log.insert("end", text, tag)
            # 限制日志区行数，避免文本框过大导致整块重排缓慢（问题10）
            total = int(self.log.index("end-1c").split(".")[0])
            if total > 5000:
                self.log.delete("1.0", f"{total - 5000}.0")
            self.log.configure(state="disabled")
            self.log.see("end")
        self.root.after(120, self._poll)

    # ------------------------------------------------ 打包页

    def _build_tab_build(self):
        f = self.tab_build
        frm = ttk.Frame(f)
        frm.pack(fill="x", padx=8, pady=6)
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="项目目录:").grid(row=0, column=0, sticky="e", padx=4, pady=4)
        self.var_project = tk.StringVar()
        ttk.Entry(frm, textvariable=self.var_project).grid(row=0, column=1, sticky="we", padx=4)
        ttk.Button(frm, text="浏览...", command=self._pick_project).grid(row=0, column=2, padx=4)

        ttk.Label(frm, text="入口脚本:").grid(row=1, column=0, sticky="e", padx=4, pady=4)
        self.var_entry = tk.StringVar(value="main.py")
        ttk.Entry(frm, textvariable=self.var_entry).grid(row=1, column=1, sticky="we", padx=4)

        ttk.Label(frm, text="应用名称:").grid(row=2, column=0, sticky="e", padx=4, pady=4)
        self.var_name = tk.StringVar(value="app")
        ttk.Entry(frm, textvariable=self.var_name).grid(row=2, column=1, sticky="we", padx=4)

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
        self.var_py = tk.StringVar(value="3.12")
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

        # mem 内存执行模式（仅 exe/pexe 格式生效）
        self.var_mem = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            opt, text="mem 内存执行（源码字节码内嵌、全程不落盘，后缀 .mem.exe/.mem.pexe）",
            variable=self.var_mem).grid(
            row=6, column=1, columnspan=4, sticky="w", padx=6)

        self.var_sign = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            opt, text="Authenticode 签名（仅 .exe，未提供证书时自动生成自签证书）",
            variable=self.var_sign).grid(
            row=7, column=1, columnspan=4, sticky="w", padx=6)

        # 可执行产物自动加执行权限（需求 29: run/pexe/pyz 等）
        self.var_chmod = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            opt, text="为可执行产物添加执行权限（run/pexe/pyz 等，chmod +x）",
            variable=self.var_chmod).grid(
            row=8, column=1, columnspan=4, sticky="w", padx=6)

        # mac pexe 再打包为 .app 壳（Finder 识别为应用）
        self.var_app_wrap = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            opt, text="mac pexe 再打包为 .app 壳（Finder 识别为应用，适合 GUI）",
            variable=self.var_app_wrap).grid(
            row=9, column=1, columnspan=4, sticky="w", padx=6)

        self.var_status = tk.StringVar(value="就绪")
        ttk.Label(f, textvariable=self.var_status, foreground="#367bf0").pack(padx=8, anchor="w")
        self.btn_build = ttk.Button(f, text="开始打包", command=self._start_build)
        self.btn_build.pack(padx=8, pady=6, anchor="w")

        # ---- 打包产物展示区（常驻主界面：左文案 / 中路径输入框 / 右按钮）----
        self.frm_result = ttk.LabelFrame(f, text="打包产物", padding=6)
        self.frm_result.pack(fill="x", padx=8, pady=(0, 8))
        self._result_rows: list = []
        self._result_vars: list = []  # 保持 StringVar 引用，防止被 GC 后输入框清空
        self._last_artifacts: list = []
        self._last_output_dir: Path | None = None
        self._show_result_hint()

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
        self._clear_result_rows()
        # 输出目录行：右侧 [打开输出目录] [复制路径]
        self._add_result_row(
            "输出目录", self._last_output_dir,
            [("打开输出目录", self._open_output_dir),
             ("复制路径", lambda: self._copy_to_clipboard(self._last_output_dir))],
        )
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
        if (Path(d) / "pythub.toml").exists():
            from pythub.config import app_config

            cfg = app_config(Path(d))
            self.var_name.set(cfg["name"])
            self.var_entry.set(cfg["entry"])

    def _start_build(self):
        if self.busy:
            messagebox.showinfo("提示", "打包正在进行中，请等待完成")
            return
        project = Path(self.var_project.get().strip())
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
            name=self.var_name.get().strip() or "app",
            entry=self.var_entry.get().strip() or "main.py",
            requirements=reqs,
            platform=plat,
            arch=self.var_arch.get(),
            python=self.var_py.get(),
            fmt=fmt,
            output=project.resolve() / "dist",
            engine=self.var_engine.get(),
            windowed=self.var_windowed.get(),
            mem=self.var_mem.get(),
            sign=self.var_sign.get(),
            chmod=self.var_chmod.get(),
            app_wrap=self.var_app_wrap.get(),
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

        ttk.Label(frm, text="发行文件:").grid(row=0, column=0, sticky="e", padx=4, pady=4)
        self.var_files = tk.StringVar(value=saved.get("files", ""))
        ttk.Entry(frm, textvariable=self.var_files).grid(row=0, column=1, sticky="we", padx=4)
        ttk.Button(frm, text="浏览...", command=self._pick_dist).grid(row=0, column=2, padx=4)

        ttk.Label(frm, text="仓库:").grid(row=1, column=0, sticky="e", padx=4, pady=4)
        self.var_repo = tk.StringVar(value=saved.get("repo", "pypi"))
        cmb_repo = ttk.Combobox(frm, textvariable=self.var_repo, values=["pypi", "testpypi"],
                                state="readonly", width=12)
        cmb_repo.grid(row=1, column=1, sticky="w", padx=4)
        cmb_repo.bind("<<ComboboxSelected>>", lambda _e: self._refresh_cred_status())

        # 需求 24: 凭据来源开关（系统已配置凭据 vs 手动输入）
        self.var_cred_sys = tk.BooleanVar(value=bool(load_pypi_credentials()))
        ttk.Label(frm, text="凭据来源:").grid(row=2, column=0, sticky="e", padx=4, pady=4)
        cred_row = ttk.Frame(frm)
        cred_row.grid(row=2, column=1, sticky="w", padx=4)
        ttk.Checkbutton(cred_row, text="使用系统已配置的凭据",
                        variable=self.var_cred_sys,
                        command=self._refresh_cred_status).pack(side="left")
        self.var_cred_status = tk.StringVar()
        ttk.Label(cred_row, textvariable=self.var_cred_status,
                  foreground="#777").pack(side="left", padx=8)

        ttk.Label(frm, text="用户名:").grid(row=3, column=0, sticky="e", padx=4, pady=4)
        self.var_user = tk.StringVar(value=saved.get("username", "__token__"))
        ttk.Entry(frm, textvariable=self.var_user).grid(row=3, column=1, sticky="we", padx=4)

        ttk.Label(frm, text="密码/Token:").grid(row=4, column=0, sticky="e", padx=4, pady=4)
        self.var_token = tk.StringVar(value=saved.get("token", ""))
        ttk.Entry(frm, textvariable=self.var_token, show="*").grid(
            row=4, column=1, sticky="we", padx=4)

        row = ttk.Frame(f)
        row.pack(padx=8, pady=6, anchor="w")
        ttk.Button(row, text="加载系统配置", command=self._load_sys_credentials).pack(side="left")
        ttk.Button(row, text="保存到系统配置", command=self._save_sys_credentials).pack(side="left", padx=6)
        ttk.Button(row, text="保存配置", command=self._save_publish_cfg).pack(side="left")
        ttk.Button(f, text="发布到 PyPI", command=self._start_publish).pack(padx=8, pady=6, anchor="w")
        self.var_pub_status = tk.StringVar(value="就绪")
        ttk.Label(f, textvariable=self.var_pub_status, foreground="#367bf0").pack(padx=8, anchor="w")
        ttk.Label(f, text="提示: 先用 `pythub pypa build` 或打包页生成 .whl/.tar.gz，再在此上传。",
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
            "files": self.var_files.get().strip(),
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

    def _pick_dist(self):
        # macOS Aqua Tk 的 filetypes 遇到多点扩展名（如 *.tar.gz）会在
        # setAllowedFileTypes 转 UTType 时崩溃（object cannot be nil），
        # 故 darwin 下用单点 *.gz 匹配 .tar.gz；其余平台通配项用 *
        if sys.platform == "darwin":
            ftypes = [("发行包", ("*.whl", "*.zip", "*.gz"))]
        else:
            ftypes = [("发行包", ("*.whl", "*.tar.gz", "*.zip")), ("全部文件", "*")]
        files = filedialog.askopenfilenames(
            title="选择发行文件", initialdir=str(self.last_dir),
            filetypes=ftypes)
        if files:
            self.last_dir = Path(files[0]).parent
            self.var_files.set(", ".join(files))

    def _start_publish(self):
        from pythub.pypa import load_pypi_credentials

        if self.busy:
            messagebox.showinfo("提示", "有任务正在进行中")
            return
        files = [Path(x.strip()) for x in self.var_files.get().split(",") if x.strip()]
        files = [f for f in files if f.exists()]
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


    # ------------------------------------------------ 设置页

    def _build_tab_settings(self):
        from pythub.utils import LOG_DIR, load_settings, set_file_logging

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
        self._refresh_cache_stats()

    @staticmethod
    def _fmt_size(n: float) -> str:
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024:
                return f"{int(n)} {unit}" if unit == "B" else f"{n:.1f} {unit}"
            n /= 1024.0
        return f"{n:.1f} PB"

    def _refresh_cache_stats(self):
        from pythub.utils import PYTHUB_CACHE_DIR, cache_stats

        stats = cache_stats()
        lines = [f"缓存目录: {PYTHUB_CACHE_DIR}"]
        lines += [f"  {sub}: {self._fmt_size(size)}" for sub, size in stats.items()]
        lines.append(f"  合计: {self._fmt_size(sum(stats.values()))}")
        self.lbl_cache_stats.config(text="\n".join(lines))

    def _prune_cache(self):
        from pythub.utils import prune_cache

        if not messagebox.askyesno(
                "确认清理",
                "按 LRU 把构建缓存裁剪到默认上限（4GB）？\n"
                "最久未使用的解释器/依赖缓存会被删除（下次打包自动重新下载）。\n"
                "日志与设置不受影响。"):
            return
        pruned = prune_cache(None)
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
