#!/usr/bin/env python3
"""ask_remote 的本地弹窗，独立进程运行。

单独成进程是因为 tkinter 的 mainloop 必须占据主线程，而 MCP server 的主线程
已经被 asyncio 事件循环占用，两者无法共存。

stdin  读 JSON: {"question": "...", "options": ["a", "b"]}
stdout 写 JSON: {"answer": "..."}  用户按 Esc 或关窗则不输出任何内容
"""

import json
import sys
import tkinter as tk

BG = "#262624"
BG_INPUT = "#1f1e1d"
BG_HOVER = "#343331"
FG = "#e8e6e3"
FG_DIM = "#8a8784"
ACCENT = "#d97757"
BORDER = "#3a3937"
FONT = ("Microsoft YaHei UI", 10)
FONT_SMALL = ("Microsoft YaHei UI", 8)


def _dark_titlebar(root):
    """Windows 10 1809+ 可以把系统标题栏也染成深色，失败就算了。"""
    try:
        import ctypes

        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        value = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE，新旧版本编号不同
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)
            )
    except Exception:
        pass


def main():
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")

    payload = json.load(sys.stdin)
    question = payload.get("question") or ""
    options = payload.get("options") or []

    answer = None

    root = tk.Tk()
    # 不用 overrideredirect：无边框窗口从 MCP 拉起的后台进程启动时不渲染
    root.title("Claude 想问你")
    root.attributes("-topmost", True)
    root.resizable(False, False)
    root.configure(bg=BG)
    _dark_titlebar(root)

    card = tk.Frame(root, bg=BG, padx=20, pady=16)
    card.pack(fill="both", expand=True)

    tk.Label(card, text="Claude 想问你", bg=BG, fg=ACCENT, font=FONT_SMALL).pack(anchor="w")

    tk.Label(
        card,
        text=question,
        bg=BG,
        fg=FG,
        font=FONT,
        wraplength=420,
        justify="left",
    ).pack(anchor="w", pady=(10, 14))

    def submit(value):
        nonlocal answer
        value = (value or "").strip()
        if not value:
            return
        answer = value
        root.destroy()

    if options:
        row = tk.Frame(card, bg=BG)
        row.pack(anchor="w", pady=(0, 12))
        for i, opt in enumerate(options):
            btn = tk.Label(
                row,
                text=f"{i + 1}. {opt}",
                bg=BG_INPUT,
                fg=FG,
                font=FONT,
                padx=12,
                pady=6,
                cursor="hand2",
            )
            btn.pack(side="left", padx=(0, 8))
            btn.bind("<Button-1>", lambda e, o=opt: submit(o))
            btn.bind("<Enter>", lambda e, b=btn: b.configure(bg=BG_HOVER))
            btn.bind("<Leave>", lambda e, b=btn: b.configure(bg=BG_INPUT))

    entry = tk.Entry(
        card,
        bg=BG_INPUT,
        fg=FG,
        font=FONT,
        relief="flat",
        insertbackground=ACCENT,
        highlightthickness=1,
        highlightbackground=BORDER,
        highlightcolor=ACCENT,
    )
    entry.pack(fill="x", ipady=7, ipadx=8)
    entry.bind("<Return>", lambda e: submit(entry.get()))

    tk.Label(
        card,
        text="回车提交 · Esc 关掉，改用微信回答",
        bg=BG,
        fg=FG_DIM,
        font=FONT_SMALL,
    ).pack(anchor="w", pady=(10, 0))

    root.bind("<Escape>", lambda e: root.destroy())

    root.update_idletasks()
    x = (root.winfo_screenwidth() - root.winfo_width()) // 2
    y = (root.winfo_screenheight() - root.winfo_height()) // 3
    root.geometry(f"+{x}+{y}")

    entry.focus_force()
    root.focus_force()
    root.mainloop()

    if answer is not None:
        json.dump({"answer": answer}, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
