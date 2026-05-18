#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量重命名与整理工具（Windows 兼容）
仅使用 Python 3.8+ 标准库。

内存友好：流式遍历文件，按父级分批处理，预览默认仅显示统计摘要。
"""

from __future__ import annotations

import argparse
import gc
import logging
import os
import queue
import re
import shutil
import sys
import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set

SUBFOLDERS = ("A", "B", "C", "D")
PREFIX_MAP = {"A": "M", "B": None, "C": "B", "D": "F"}
NUMERIC_STEM_PATTERN = re.compile(r"^\d+$")

def _app_dir() -> Path:
    """脚本或打包 exe 所在目录（日志写在 exe 旁边）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


SCRIPT_DIR = _app_dir()
LOG_FILE = SCRIPT_DIR / "rename_log.txt"

# 预览：每个父级最多展示几条样例；全局超过此条数则折叠为摘要模式
PREVIEW_SAMPLE_PER_PARENT = 6
PREVIEW_DETAIL_MAX_OPS = 300


@dataclass
class ParentStats:
    """单个父级的操作统计（仅占少量内存）。"""

    parent: Path
    step2_rename: int = 0
    step2_skip: int = 0
    step3_move: int = 0
    step4_rename: int = 0
    step3_rmdir: int = 0
    move_collisions: int = 0
    samples: List[str] = field(default_factory=list)

    @property
    def total_ops(self) -> int:
        return (
            self.step2_rename
            + self.step2_skip
            + self.step3_move
            + self.step4_rename
            + self.step3_rmdir
        )


class QueueLogHandler(logging.Handler):
    """将日志写入队列，供窗口界面轮询显示。"""

    def __init__(self, log_queue: queue.Queue[str]) -> None:
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.log_queue.put(self.format(record))
        except Exception:
            self.handleError(record)


def setup_logging(
    *,
    extra_handlers: Optional[List[logging.Handler]] = None,
    console: bool = True,
) -> logging.Logger:
    """配置文件日志；可选控制台与额外 Handler。"""
    logger = logging.getLogger("rename_tool")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    formatter = logging.Formatter("%(message)s")

    if console and sys.stdout is not None:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setLevel(logging.INFO)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

    if extra_handlers:
        for handler in extra_handlers:
            handler.setFormatter(formatter)
            handler.setLevel(logging.INFO)
            logger.addHandler(handler)

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    logger.info("")
    logger.info("=" * 60)
    logger.info("[INFO]  运行开始: %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("=" * 60)
    return logger


def is_windowed_runtime() -> bool:
    """PyInstaller --windowed 打包后无控制台。"""
    if getattr(sys, "frozen", False):
        return sys.stdout is None
    return False


def resolve_subfolder_names(parent: Path, case_sensitive: bool) -> Optional[Dict[str, Path]]:
    """返回 parent 下实际存在的 A/B/C/D 子文件夹；至少有一个时返回 dict，否则 None。"""
    try:
        children = [p for p in parent.iterdir() if p.is_dir()]
    except OSError:
        return None

    if case_sensitive:
        names = {p.name: p for p in children}
        found = {name: names[name] for name in SUBFOLDERS if name in names}
    else:
        lower_map: Dict[str, Path] = {}
        for p in children:
            key = p.name.lower()
            if key not in lower_map:
                lower_map[key] = p
        found = {
            name: lower_map[name.lower()]
            for name in SUBFOLDERS
            if name.lower() in lower_map
        }

    return found if found else None


def find_target_parents(root: Path, case_sensitive: bool) -> List[Path]:
    """扫描根目录，仅收集父级路径（不缓存文件列表）。"""
    targets: List[Path] = []
    if not root.is_dir():
        return targets

    for current, _, _ in os.walk(root, topdown=True):
        folder = Path(current)
        if resolve_subfolder_names(folder, case_sensitive) is not None:
            targets.append(folder.resolve())

    return sorted(set(targets), key=lambda p: (-len(p.parts), str(p).lower()))


def log_folder_banner(
    logger: logging.Logger,
    phase: str,
    index: int,
    total: int,
    parent: Path,
) -> None:
    """打印当前正在处理的父级文件夹进度。"""
    logger.info("")
    logger.info("-" * 60)
    logger.info(
        "[INFO]  [%s] (%d/%d) 父级文件夹: %s",
        phase,
        index,
        total,
        parent,
    )
    logger.info("-" * 60)


def get_nested_targets(current_parent: Path, all_parents: List[Path]) -> Set[Path]:
    """当前父级内部、需由内层单独处理的嵌套 ABCD 父级。"""
    nested: Set[Path] = set()
    for other in all_parents:
        if other == current_parent:
            continue
        try:
            other.relative_to(current_parent)
            nested.add(other)
        except ValueError:
            pass
    return nested


def is_under_nested_target(file_path: Path, nested: Set[Path]) -> bool:
    for other in nested:
        if file_path == other or other in file_path.parents:
            return True
    return False


def iter_files_in_subfolder(folder: Path, nested: Set[Path]) -> Iterator[Path]:
    """
    用 os.scandir 栈式遍历，逐文件 yield，不一次性加载路径列表。
    跳过位于嵌套 ABCD 父级目录树内的文件。
    """
    stack: List[Path] = [folder]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                subdirs: List[Path] = []
                for entry in entries:
                    path = Path(entry.path)
                    if entry.is_dir(follow_symlinks=False):
                        if not is_under_nested_target(path, nested):
                            subdirs.append(path)
                    elif entry.is_file(follow_symlinks=False):
                        if not is_under_nested_target(path, nested):
                            yield path
                stack.extend(reversed(subdirs))
        except OSError:
            continue


def step2_new_filename(folder_key: str, filename: str) -> Optional[str]:
    path = Path(filename)
    if folder_key == "B":
        return filename
    if not NUMERIC_STEM_PATTERN.match(path.stem):
        return None
    return f"{PREFIX_MAP[folder_key]}{int(path.stem):03d}{path.suffix}"


def step4_new_filename(parent_name: str, filename: str) -> str:
    return f"{parent_name}-{filename}"


def _add_sample(stats: ParentStats, line: str) -> None:
    if len(stats.samples) < PREVIEW_SAMPLE_PER_PARENT:
        stats.samples.append(line)


def safe_rename(src: Path, dst: Path, logger: logging.Logger) -> bool:
    if src == dst:
        return True
    if dst.exists():
        logger.error("[ERROR] 目标文件已存在，跳过: %s", dst)
        return False
    try:
        src.rename(dst)
        return True
    except OSError as exc:
        logger.error("[ERROR] 重命名失败 %s → %s: %s", src, dst, exc)
        return False


def safe_move(src: Path, dst: Path, logger: logging.Logger) -> bool:
    if dst.exists():
        logger.error("[ERROR] 目标文件已存在，跳过: %s", dst)
        return False
    try:
        shutil.move(str(src), str(dst))
        return True
    except OSError as exc:
        logger.error("[ERROR] 移动失败 %s → %s: %s", src, dst, exc)
        return False


def safe_rmdir(folder: Path, logger: logging.Logger) -> bool:
    try:
        if folder.exists() and folder.is_dir():
            if any(folder.iterdir()):
                logger.warning("[WARN]  文件夹非空，未删除: %s", folder)
                return False
            folder.rmdir()
            return True
    except OSError as exc:
        logger.error("[ERROR] 删除文件夹失败 %s: %s", folder, exc)
        return False
    return False


def preview_parent(
    parent: Path,
    case_sensitive: bool,
    nested: Set[Path],
) -> ParentStats:
    """干跑统计：不修改文件，仅计数与采样（单父级完成后即可释放）。"""
    stats = ParentStats(parent=parent)
    subfolders = resolve_subfolder_names(parent, case_sensitive)
    if subfolders is None:
        return stats

    parent_name = parent.name
    move_names: Counter[str] = Counter()
    stats.step3_rmdir = len(subfolders)

    for key, folder in subfolders.items():
        for src in iter_files_in_subfolder(folder, nested):
            rel = src.relative_to(folder)
            new_name = step2_new_filename(key, src.name)
            if new_name is None:
                stats.step2_skip += 1
                _add_sample(
                    stats,
                    f"[WARN]  跳过非数字文件名: {folder.name}/{rel.as_posix()}",
                )
                move_names[src.name] += 1
                continue

            if src.name != new_name:
                stats.step2_rename += 1
                rel_dst = (
                    rel.parent / new_name if rel.parent != Path(".") else Path(new_name)
                )
                _add_sample(
                    stats,
                    f"[INFO]  {folder.name}/{rel.as_posix()} → "
                    f"{folder.name}/{rel_dst.as_posix()}",
                )
            move_names[new_name] += 1

    stats.step3_move = sum(move_names.values())
    stats.move_collisions = sum(c - 1 for c in move_names.values() if c > 1)
    if move_names:
        sample_name = next(iter(move_names))
        _add_sample(
            stats,
            f"[INFO]  移动文件: * → {parent_name}/{sample_name} （共 {stats.step3_move} 个）",
        )

    names_for_step4: Set[str] = set(move_names.keys())
    try:
        for item in parent.iterdir():
            if item.is_file():
                names_for_step4.add(item.name)
    except OSError:
        pass

    for name in sorted(names_for_step4, key=str.lower):
        final = step4_new_filename(parent_name, name)
        if final != name:
            stats.step4_rename += 1
            _add_sample(stats, f"[INFO]  重命名: {name} → {final}")

    return stats


def execute_parent(
    parent: Path,
    case_sensitive: bool,
    nested: Set[Path],
    logger: logging.Logger,
) -> ParentStats:
    """执行单父级：流式处理，处理完即释放，不保留全局计划列表。"""
    stats = ParentStats(parent=parent)
    subfolders = resolve_subfolder_names(parent, case_sensitive)
    if subfolders is None:
        return stats

    parent_name = parent.name
    logger.info(
        "[INFO]  处理子文件夹: %s",
        ", ".join(f"{k}({v.name})" for k, v in subfolders.items()),
    )

    # Step 2
    for key, folder in subfolders.items():
        for src in iter_files_in_subfolder(folder, nested):
            rel = src.relative_to(folder)
            new_name = step2_new_filename(key, src.name)
            if new_name is None:
                stats.step2_skip += 1
                logger.warning(
                    "[WARN]  跳过非数字文件名: %s/%s",
                    folder.name,
                    rel.as_posix(),
                )
                continue
            dst = src.parent / new_name
            if src.name == new_name:
                continue
            desc = (
                f"[INFO]  {folder.name}/{rel.as_posix()} → "
                f"{folder.name}/{(rel.parent / new_name).as_posix()}"
            )
            if safe_rename(src, dst, logger):
                stats.step2_rename += 1
                logger.info(desc)

    # Step 3：移动（再次流式遍历，不缓存路径列表）
    for key, folder in subfolders.items():
        for src in iter_files_in_subfolder(folder, nested):
            dst = parent / src.name
            rel_from = f"{folder.name}/{src.relative_to(folder).as_posix()}"
            desc = f"[INFO]  移动文件: {rel_from} → {parent_name}/{src.name}"
            if safe_move(src, dst, logger):
                stats.step3_move += 1
                logger.info(desc)
            else:
                stats.move_collisions += 1

    for key, folder in subfolders.items():
        desc = f"[INFO]  删除空文件夹: {parent_name}/{folder.name}"
        if safe_rmdir(folder, logger):
            stats.step3_rmdir += 1
            logger.info(desc)

    # Step 4（先收集文件名，避免遍历时重命名导致遗漏）
    try:
        file_names = [p.name for p in parent.iterdir() if p.is_file()]
    except OSError as exc:
        logger.error("[ERROR] 读取父级目录失败 %s: %s", parent, exc)
        file_names = []

    for name in file_names:
        item = parent / name
        new_name = step4_new_filename(parent_name, name)
        if new_name == name:
            continue
        desc = f"[INFO]  重命名: {name} → {new_name}"
        if safe_rename(item, parent / new_name, logger):
            stats.step4_rename += 1
            logger.info(desc)

    return stats


def log_single_parent_preview(
    logger: logging.Logger,
    stats: ParentStats,
    index: int,
    total: int,
    use_summary: bool,
) -> None:
    """输出单个父级的预览结果。"""
    logger.info("")
    logger.info("--- (%d/%d) 父级文件夹: %s ---", index, total, stats.parent)
    if stats.total_ops == 0:
        logger.info("  （无需变更）")
        return
    if use_summary:
        logger.info(
            "  Step2 重命名: %d  跳过: %d", stats.step2_rename, stats.step2_skip
        )
        logger.info(
            "  Step3 移动: %d  同名冲突风险: %d",
            stats.step3_move,
            stats.move_collisions,
        )
        logger.info(
            "  Step4 加前缀: %d  删除空子文件夹(最多): %d",
            stats.step4_rename,
            stats.step3_rmdir,
        )
        if stats.samples:
            logger.info("  样例:")
            for line in stats.samples[:PREVIEW_SAMPLE_PER_PARENT]:
                logger.info("    %s", line)
    else:
        for line in stats.samples:
            logger.info("  %s", line)
        if stats.total_ops > len(stats.samples):
            logger.info(
                "  ... 另有约 %d 条操作未列出",
                stats.total_ops - len(stats.samples),
            )


def log_preview_summary(
    logger: logging.Logger,
    parents: List[Path],
    total_ops: int,
    use_summary: bool,
) -> None:
    """输出预览汇总。"""
    logger.info("")
    logger.info("=" * 60)
    logger.info("【汇总】已按文件夹顺序逐个处理（预览后即时执行）")
    if use_summary:
        logger.info(
            "（总操作数 %d 超过 %d，已启用摘要模式；加 --full-preview 可查看全部样例）",
            total_ops,
            PREVIEW_DETAIL_MAX_OPS,
        )
    logger.info("=" * 60)
    if not parents:
        logger.info("（未找到符合条件的父级文件夹）")
    else:
        logger.info("共 %d 个父级文件夹，约 %d 条操作", len(parents), total_ops)
    logger.info("=" * 60)
    logger.info("")


def ask_confirmation_cli() -> bool:
    while True:
        try:
            answer = input("确认执行以上操作？请输入 y 继续，其他键取消: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消。")
            return False
        if answer == "y":
            return True
        if answer in ("n", "no", ""):
            print("已取消，未做任何修改。")
            return False
        print("请输入 y 确认，或按回车取消。")


def get_root_path(arg_path: Optional[str]) -> Path:
    if arg_path:
        return Path(arg_path).expanduser().resolve()
    return Path.cwd().resolve()


def should_use_gui(args: argparse.Namespace) -> bool:
    """未指定目录且未强制命令行/窗口模式时，弹出简易选目录界面。"""
    if args.root or args.cli or args.windowed:
        return False
    return True


def pick_folder_dialog(initial: Optional[Path] = None) -> Optional[Path]:
    """系统文件夹选择对话框；取消时返回 None。"""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        return None

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        chosen = filedialog.askdirectory(
            title="选择要处理的文件夹",
            initialdir=str(initial or Path.home()),
            mustexist=True,
        )
    finally:
        root.destroy()
    if not chosen:
        return None
    return Path(chosen).resolve()


def launch_gui(initial: Optional[Path] = None) -> Optional[argparse.Namespace]:
    """
    简易启动界面：选择目录与常用选项。
    用户取消时返回 None。
    """
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except ImportError:
        return None

    result: dict = {"cancelled": True}

    root = tk.Tk()
    root.title("批量重命名整理工具")
    root.resizable(False, False)
    root.attributes("-topmost", True)

    pad = {"padx": 12, "pady": 6}
    frame = ttk.Frame(root, padding=16)
    frame.grid(row=0, column=0, sticky="nsew")

    ttk.Label(
        frame,
        text="批量重命名与整理（A/B/C/D 子文件夹）",
        font=("", 12, "bold"),
    ).grid(row=0, column=0, columnspan=3, sticky="w", **pad)

    ttk.Label(frame, text="要处理的目录：").grid(row=1, column=0, sticky="w", **pad)
    path_var = tk.StringVar(
        value=str(initial) if initial and initial.is_dir() else ""
    )
    path_entry = ttk.Entry(frame, textvariable=path_var, width=48)
    path_entry.grid(row=1, column=1, sticky="ew", **pad)

    def browse() -> None:
        chosen = filedialog.askdirectory(
            title="选择要处理的文件夹",
            initialdir=path_var.get() or str(Path.home()),
            mustexist=True,
        )
        if chosen:
            path_var.set(chosen)

    ttk.Button(frame, text="浏览…", command=browse).grid(row=1, column=2, **pad)

    case_var = tk.BooleanVar(value=False)
    confirm_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(
        frame,
        text="A/B/C/D 文件夹名大小写不敏感",
        variable=case_var,
    ).grid(row=2, column=0, columnspan=3, sticky="w", padx=12)
    ttk.Checkbutton(
        frame,
        text="全部预览完成后再统一确认（否则每个文件夹预览后立即执行）",
        variable=confirm_var,
    ).grid(row=3, column=0, columnspan=3, sticky="w", padx=12)

    hint = "提示：也可将文件夹直接拖到 rename_tool.exe 图标上运行。"
    ttk.Label(frame, text=hint, foreground="gray").grid(
        row=4, column=0, columnspan=3, sticky="w", padx=12, pady=(0, 8)
    )

    btn_frame = ttk.Frame(frame)
    btn_frame.grid(row=5, column=0, columnspan=3, pady=(4, 0))

    def on_start() -> None:
        raw = path_var.get().strip().strip('"').strip("'")
        if not raw:
            messagebox.showwarning("提示", "请先选择要处理的文件夹。")
            return
        p = Path(raw).expanduser()
        if not p.is_dir():
            messagebox.showerror("错误", f"路径不存在或不是文件夹：\n{p}")
            return
        result["cancelled"] = False
        result["root"] = str(p.resolve())
        result["case_insensitive"] = case_var.get()
        result["confirm"] = confirm_var.get()
        root.destroy()

    def on_cancel() -> None:
        root.destroy()

    ttk.Button(btn_frame, text="开始处理", command=on_start, width=14).pack(
        side=tk.LEFT, padx=8
    )
    ttk.Button(btn_frame, text="退出", command=on_cancel, width=10).pack(
        side=tk.LEFT, padx=8
    )

    frame.columnconfigure(1, weight=1)
    root.bind("<Return>", lambda _e: on_start())
    root.bind("<Escape>", lambda _e: on_cancel())
    root.update_idletasks()
    w, h = root.winfo_reqwidth(), root.winfo_reqheight()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"+{(sw - w) // 2}+{(sh - h) // 3}")

    root.mainloop()

    if result.get("cancelled"):
        return None
    ns = argparse.Namespace(
        root=result["root"],
        case_insensitive=result["case_insensitive"],
        full_preview=False,
        confirm=result["confirm"],
        cli=False,
        windowed=False,
    )
    return ns


def launch_windowed_app(args: argparse.Namespace) -> int:
    """
    无控制台窗口模式：选目录 + 实时日志 + 后台处理。
    阻塞直到用户关闭窗口。
    """
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, scrolledtext, ttk
    except ImportError:
        print("错误: 当前环境不支持图形界面 (tkinter)。")
        return 1

    log_queue: queue.Queue[str] = queue.Queue()
    ui_state = {"running": False, "exit_code": 0}

    root = tk.Tk()
    root.title("批量重命名整理工具")
    root.minsize(640, 480)
    root.geometry("720x560")

    main = ttk.Frame(root, padding=12)
    main.pack(fill=tk.BOTH, expand=True)
    main.columnconfigure(1, weight=1)
    main.rowconfigure(4, weight=1)

    ttk.Label(main, text="批量重命名与整理", font=("", 13, "bold")).grid(
        row=0, column=0, columnspan=3, sticky="w", pady=(0, 8)
    )

    ttk.Label(main, text="要处理的目录：").grid(row=1, column=0, sticky="w")
    path_var = tk.StringVar(value=args.root or "")
    path_entry = ttk.Entry(main, textvariable=path_var)
    path_entry.grid(row=1, column=1, sticky="ew", padx=(6, 6))

    def browse() -> None:
        chosen = filedialog.askdirectory(
            title="选择要处理的文件夹",
            initialdir=path_var.get() or str(Path.home()),
            mustexist=True,
        )
        if chosen:
            path_var.set(chosen)

    ttk.Button(main, text="浏览…", command=browse).grid(row=1, column=2)

    opts = ttk.Frame(main)
    opts.grid(row=2, column=0, columnspan=3, sticky="w", pady=6)
    case_var = tk.BooleanVar(value=args.case_insensitive)
    confirm_var = tk.BooleanVar(value=args.confirm)
    ttk.Checkbutton(
        opts, text="A/B/C/D 大小写不敏感", variable=case_var
    ).pack(anchor="w")
    ttk.Checkbutton(
        opts,
        text="全部预览后再确认执行（否则每文件夹预览后立即执行）",
        variable=confirm_var,
    ).pack(anchor="w")

    status_var = tk.StringVar(value="就绪：请选择目录后点击「开始处理」")
    ttk.Label(main, textvariable=status_var, foreground="#333").grid(
        row=3, column=0, columnspan=3, sticky="w", pady=(0, 4)
    )

    log_frame = ttk.LabelFrame(main, text="运行日志", padding=4)
    log_frame.grid(row=4, column=0, columnspan=3, sticky="nsew", pady=(0, 8))
    log_frame.columnconfigure(0, weight=1)
    log_frame.rowconfigure(0, weight=1)

    log_text = scrolledtext.ScrolledText(
        log_frame, height=16, state=tk.DISABLED, wrap=tk.WORD, font=("Menlo", 11)
    )
    if sys.platform == "win32":
        log_text.configure(font=("Consolas", 10))
    log_text.grid(row=0, column=0, sticky="nsew")

    def append_log(line: str) -> None:
        log_text.configure(state=tk.NORMAL)
        log_text.insert(tk.END, line + "\n")
        log_text.see(tk.END)
        log_text.configure(state=tk.DISABLED)

    def poll_log_queue() -> None:
        while True:
            try:
                append_log(log_queue.get_nowait())
            except queue.Empty:
                break
        root.after(80, poll_log_queue)

    poll_log_queue()

    btn_row = ttk.Frame(main)
    btn_row.grid(row=5, column=0, columnspan=3)

    start_btn = ttk.Button(btn_row, text="开始处理", width=12)
    log_btn = ttk.Button(btn_row, text="打开日志文件", width=12)
    close_btn = ttk.Button(btn_row, text="关闭", width=10)

    def set_busy(busy: bool) -> None:
        state = tk.DISABLED if busy else tk.NORMAL
        start_btn.configure(state=state)
        path_entry.configure(state=state)
        for child in opts.winfo_children():
            child.configure(state=state)

    def open_log_file() -> None:
        if LOG_FILE.exists():
            if sys.platform == "win32":
                os.startfile(LOG_FILE)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                os.system(f'open "{LOG_FILE}"')
            else:
                os.system(f'xdg-open "{LOG_FILE}"')
        else:
            messagebox.showinfo("提示", "日志文件尚未生成。")

    def gui_confirm() -> bool:
        """在工作线程中调用，转到主线程弹窗。"""
        evt = threading.Event()
        choice = [False]

        def _ask() -> None:
            choice[0] = messagebox.askyesno(
                "确认执行",
                "预览已完成。是否执行以上重命名与整理操作？",
                parent=root,
            )
            evt.set()

        root.after(0, _ask)
        evt.wait()
        return choice[0]

    def worker(run_args: argparse.Namespace) -> None:
        handler = QueueLogHandler(log_queue)
        logger = setup_logging(extra_handlers=[handler], console=False)
        try:
            code = run(run_args, logger=logger, confirm_fn=gui_confirm)
            ui_state["exit_code"] = code
        except Exception as exc:  # noqa: BLE001
            logger.error("[ERROR] 未预期的错误: %s", exc)
            ui_state["exit_code"] = 1
        finally:
            root.after(0, on_finished)

    def on_finished() -> None:
        ui_state["running"] = False
        set_busy(False)
        if ui_state["exit_code"] == 0:
            status_var.set("处理完成")
            messagebox.showinfo(
                "完成",
                f"处理完成。\n详细日志：\n{LOG_FILE}",
                parent=root,
            )
        else:
            status_var.set("处理结束（存在错误或失败项）")
            messagebox.showwarning(
                "完成",
                f"处理结束，请查看日志。\n{LOG_FILE}",
                parent=root,
            )

    def on_start() -> None:
        if ui_state["running"]:
            return
        raw = path_var.get().strip().strip('"').strip("'")
        if not raw:
            messagebox.showwarning("提示", "请先选择要处理的文件夹。", parent=root)
            return
        p = Path(raw).expanduser()
        if not p.is_dir():
            messagebox.showerror(
                "错误", f"路径不存在或不是文件夹：\n{p}", parent=root
            )
            return

        run_args = argparse.Namespace(
            root=str(p.resolve()),
            case_insensitive=case_var.get(),
            full_preview=args.full_preview,
            confirm=confirm_var.get(),
            cli=False,
            windowed=True,
        )

        log_text.configure(state=tk.NORMAL)
        log_text.delete("1.0", tk.END)
        log_text.configure(state=tk.DISABLED)

        ui_state["running"] = True
        ui_state["exit_code"] = 0
        set_busy(True)
        status_var.set(f"正在处理：{p}")
        threading.Thread(target=worker, args=(run_args,), daemon=True).start()

    def on_close() -> None:
        if ui_state["running"]:
            if not messagebox.askyesno(
                "确认",
                "正在处理中，确定要关闭吗？",
                parent=root,
            ):
                return
        root.destroy()

    start_btn.configure(command=on_start)
    log_btn.configure(command=open_log_file)
    close_btn.configure(command=on_close)
    start_btn.pack(side=tk.LEFT, padx=(0, 8))
    log_btn.pack(side=tk.LEFT, padx=(0, 8))
    close_btn.pack(side=tk.LEFT)

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.bind("<Return>", lambda _e: on_start() if not ui_state["running"] else None)

    if args.root and Path(args.root).is_dir():
        status_var.set("已载入路径，点击「开始处理」运行")

    root.mainloop()
    return ui_state["exit_code"]


def resolve_args(argv: Optional[List[str]] = None) -> Optional[argparse.Namespace]:
    """解析参数；图形界面取消时返回 None。"""
    parser = argparse.ArgumentParser(
        description="批量重命名与整理 A/B/C/D 目录结构下的文件（Windows 兼容）"
    )
    parser.add_argument(
        "root",
        nargs="?",
        help="要扫描的根目录；省略则弹出图形界面选择",
    )
    parser.add_argument(
        "--cli",
        action="store_true",
        help="命令行模式：不弹窗，未指定目录时使用当前工作目录",
    )
    parser.add_argument(
        "--case-insensitive",
        action="store_true",
        help="A/B/C/D 文件夹名大小写不敏感匹配（默认大小写敏感）",
    )
    parser.add_argument(
        "--full-preview",
        action="store_true",
        help="预览时显示更多样例（默认超过 300 条操作时仅用摘要）",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="全部预览完成后再统一确认执行（默认：每个文件夹预览后立即执行）",
    )
    parser.add_argument(
        "--windowed",
        action="store_true",
        help="无黑窗口模式：图形界面内显示实时日志（适合 rename_tool_windowed.exe）",
    )
    args = parser.parse_args(argv)

    if is_windowed_runtime():
        args.windowed = True

    if should_use_gui(args):
        gui_args = launch_gui()
        if gui_args is None:
            # 图形界面不可用或未选择：尝试系统文件夹对话框
            picked = pick_folder_dialog()
            if picked is None:
                print("已取消，未选择文件夹。")
                return None
            args.root = str(picked)
        else:
            args = gui_args
            args.windowed = getattr(args, "windowed", False)

    if not hasattr(args, "windowed"):
        args.windowed = False
    return args


def pause_before_exit() -> None:
    """双击控制台 exe 时，处理结束后等待用户查看结果。"""
    if not getattr(sys, "frozen", False):
        return
    try:
        input("\n处理结束，按回车键退出…")
    except (EOFError, KeyboardInterrupt):
        pass


def run(
    args: argparse.Namespace,
    *,
    logger: Optional[logging.Logger] = None,
    confirm_fn=None,
) -> int:
    if logger is None:
        logger = setup_logging(console=not getattr(args, "windowed", False))
    case_sensitive = not args.case_insensitive

    root = get_root_path(args.root)
    if not root.exists():
        logger.error("[ERROR] 根目录不存在: %s", root)
        return 1
    if not root.is_dir():
        logger.error("[ERROR] 路径不是文件夹: %s", root)
        return 1

    logger.info("[INFO]  扫描目标（当前目录）: %s", root)
    logger.info(
        "[INFO]  文件夹匹配模式: %s",
        "大小写敏感" if case_sensitive else "大小写不敏感",
    )
    mode = "预览后统一确认" if args.confirm else "预览后立即执行"
    logger.info("[INFO]  运行模式: %s（按文件夹逐个处理）", mode)

    parents = find_target_parents(root, case_sensitive)
    if not parents:
        logger.warning(
            "[WARN]  在目标目录下未找到任何包含 A/B/C/D 子文件夹的父级"
        )
        log_preview_summary(logger, [], 0, False)
        return 0

    total_parents = len(parents)
    logger.info(
        "[INFO]  共找到 %d 个含 A/B/C/D 子文件夹的父级（可部分缺失）:",
        total_parents,
    )
    for idx, p in enumerate(parents, start=1):
        logger.info("[INFO]    %d. %s", idx, p)

    logger.info("")
    logger.info("=" * 60)
    if args.confirm:
        logger.info("【预览】逐文件夹扫描（全部预览后再确认执行）：")
    else:
        logger.info("【处理】逐文件夹：预览 → 立即执行 → 下一个：")
    logger.info("=" * 60)

    total_ops = 0
    success_parents = 0
    fail_parents = 0
    skipped_parents = 0
    pending_execute: List[tuple[int, Path, Set[Path]]] = []

    for idx, parent in enumerate(parents, start=1):
        log_folder_banner(logger, "预览扫描", idx, total_parents, parent)
        nested = get_nested_targets(parent, parents)
        subfolders = resolve_subfolder_names(parent, case_sensitive)
        if subfolders:
            logger.info(
                "[INFO]  检测到子文件夹: %s",
                ", ".join(f"{k}({p.name})" for k, p in subfolders.items()),
            )
        preview_stats = preview_parent(parent, case_sensitive, nested)
        total_ops += preview_stats.total_ops
        use_summary = not args.full_preview and total_ops > PREVIEW_DETAIL_MAX_OPS
        log_single_parent_preview(
            logger, preview_stats, idx, total_parents, use_summary
        )
        logger.info(
            "[INFO]  预览完成 (%d/%d): Step2=%d 跳过=%d Step3=%d Step4=%d",
            idx,
            total_parents,
            preview_stats.step2_rename,
            preview_stats.step2_skip,
            preview_stats.step3_move,
            preview_stats.step4_rename,
        )

        if preview_stats.total_ops == 0:
            logger.info("[INFO]  跳过执行 (%d/%d): 无需变更", idx, total_parents)
            skipped_parents += 1
            del preview_stats, nested
            continue

        if args.confirm:
            pending_execute.append((idx, parent, nested))
            del preview_stats
            continue

        log_folder_banner(logger, "立即执行", idx, total_parents, parent)
        try:
            exec_stats = execute_parent(parent, case_sensitive, nested, logger)
            logger.info(
                "[INFO]  执行完成 (%d/%d) %s | Step2 重命名 %d | "
                "Step3 移动 %d | Step4 加前缀 %d | 跳过 %d",
                idx,
                total_parents,
                parent.name,
                exec_stats.step2_rename,
                exec_stats.step3_move,
                exec_stats.step4_rename,
                exec_stats.step2_skip,
            )
            success_parents += 1
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "[ERROR] 执行失败 (%d/%d) %s: %s", idx, total_parents, parent, exc
            )
            fail_parents += 1
        finally:
            del preview_stats, nested
            gc.collect()

    use_summary = not args.full_preview and total_ops > PREVIEW_DETAIL_MAX_OPS
    log_preview_summary(logger, parents, total_ops, use_summary)

    if total_ops == 0:
        logger.info("[INFO]  无需执行任何操作")
        return 0

    if args.confirm:
        confirmed = confirm_fn() if confirm_fn else ask_confirmation_cli()
        if not confirmed:
            logger.info("[INFO]  用户取消执行")
            return 0
        for idx, parent, nested in pending_execute:
            log_folder_banner(logger, "开始执行", idx, total_parents, parent)
            try:
                exec_stats = execute_parent(parent, case_sensitive, nested, logger)
                logger.info(
                    "[INFO]  执行完成 (%d/%d) %s | Step2 重命名 %d | "
                    "Step3 移动 %d | Step4 加前缀 %d | 跳过 %d",
                    idx,
                    total_parents,
                    parent.name,
                    exec_stats.step2_rename,
                    exec_stats.step3_move,
                    exec_stats.step4_rename,
                    exec_stats.step2_skip,
                )
                success_parents += 1
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "[ERROR] 执行失败 (%d/%d) %s: %s",
                    idx,
                    total_parents,
                    parent,
                    exc,
                )
                fail_parents += 1
            finally:
                del nested
                gc.collect()

    logger.info(
        "[INFO]  全部完成: 成功 %d, 失败 %d, 跳过(无变更) %d",
        success_parents,
        fail_parents,
        skipped_parents,
    )
    logger.info("[INFO]  日志已写入: %s", LOG_FILE)
    return 0 if fail_parents == 0 else 1


def main(argv: Optional[List[str]] = None) -> int:
    args = resolve_args(argv)
    if args is None:
        return 0
    if args.windowed:
        return launch_windowed_app(args)
    code = run(args)
    pause_before_exit()
    return code


if __name__ == "__main__":
    sys.exit(main())
