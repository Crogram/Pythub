"""hello_app —— Pythub 示例工程。

用于测试与验证 Pythub 的打包能力：
- 纯标准库实现，零第三方依赖
- 支持 GUI（tkinter）与控制台两种运行方式
- 显示当前运行环境信息，便于验证跨平台产物
"""

import platform
import sys


def info() -> str:
    return (
        f"Python {platform.python_version()} @ "
        f"{platform.system()} {platform.machine()}"
    )


def main() -> None:
    args = sys.argv[1:]
    try:
        import tkinter
    except ImportError:
        tkinter = None  # type: ignore

    if tkinter and "--console" not in args:
        root = tkinter.Tk()
        root.title("Hello Pythub")
        root.geometry("460x200")
        tkinter.Label(root, text="Hello, Pythub!", font=("Helvetica", 22)).pack(pady=24)
        tkinter.Label(root, text=info(), font=("Menlo", 12)).pack()
        tkinter.Label(root, text="由 Pythub 打包 | CROGRAM", font=("Helvetica", 10)).pack(pady=12)
        root.mainloop()
    else:
        print("Hello, Pythub!")
        print(info())


if __name__ == "__main__":
    main()
