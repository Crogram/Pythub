"""HelloPythub —— Pythub 示例工程。

用于测试与验证 Pythub 的打包能力：
- 纯标准库实现，零第三方依赖
- 支持 GUI（tkinter）与控制台两种运行方式
- 显示当前运行环境信息，便于验证跨平台产物
- 展示 Pythub 注入的打包/运行信息（需求 63:
  sys.pythub_packaged 标志 + sys.pythub_info 信息字典，
  无需 import 任何额外模块）
"""

import platform
import sys


def info() -> str:
    lines = [
        f"Python {platform.python_version()} @ "
        f"{platform.system()} {platform.machine()}"
    ]
    # 打包信息由 Pythub 注入到标准库 sys 模块上（无需 import 额外模块）；
    # 源码直接运行时 sys 上没有这些属性，getattr 回退为 None
    if getattr(sys, "pythub_packaged", False):
        d = sys.pythub_info
        app, tgt, bld, rt = (
            d["app"], d["target"], d["build"], d["runtime"]
        )
        lines += [
            f"由 Pythub v{d['pythub_version']} 打包 | "
            f"引擎 {app['engine']} | 格式 {app['fmt']}",
            f"应用: {app['name']} v{app['version']}",
            f"目标平台: {tgt['platform']}/{tgt['arch']} | "
            f"目标 Python: {tgt['python']} | mem: {tgt['mem']}",
            f"构建平台: {bld['host_os']}/{bld['host_arch']} | "
            f"Python: {bld['python']} @ {bld['time']}",
            f"运行 Python: {rt['python']} ({rt['implementation']})",
            f"应用入口: {rt['app_origin'] or '未知'}",
            f"源码位置: {rt['source'] or '未知'} "
            f"（{'磁盘' if rt['source_on_disk'] else '内存'}）",
            f"解释器: {rt['executable']}",
            f"缓存目录: {rt['cache_dir']}",
        ]
    else:
        lines.append("(未打包直接运行: 无 Pythub 注入信息)")
    return "\n".join(lines)


def main() -> None:
    args = sys.argv[1:]
    try:
        import tkinter
    except ImportError:
        tkinter = None  # type: ignore

    if tkinter and "--console" not in args:
        root = tkinter.Tk()
        root.title("Hello Pythub")
        root.geometry("600x350")
        tkinter.Label(root, text="Hello, Pythub!", font=("Helvetica", 30)).pack(pady=14)
        # 信息区用只读 Text（wrap="char"）: 应用入口/源码位置/解释器/缓存目录
        # 等长路径自动换行显示，不超窗截断；且支持选中复制
        txt = tkinter.Text(
            root, font=("Menlo", 13), wrap="char", height=9,
            borderwidth=0, highlightthickness=0,
            background=root.cget("background"),
        )
        txt.insert("1.0", info())
        txt.configure(state="disabled")
        txt.pack(fill="both", expand=True, padx=24, pady=(10, 4))
        tkinter.Label(root, text="由 Pythub 打包 | CROGRAM", font=("Helvetica", 10)).pack(pady=10)
        root.mainloop()
    else:
        print("Hello, Pythub!")
        print(info())


if __name__ == "__main__":
    main()
