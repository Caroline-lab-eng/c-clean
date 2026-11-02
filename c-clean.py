#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cdisk_cleaner.py —— Windows C 盘深度清理工具

仅依赖 Python 标准库。

常用命令：
    python cdisk_cleaner.py --list                  # 查看所有清理项
    python cdisk_cleaner.py --dry-run               # 预演：只扫描统计，不删除
    python cdisk_cleaner.py                         # 交互式清理（默认项）
    python cdisk_cleaner.py -y                      # 直接清理默认项
    python cdisk_cleaner.py --all -y                # 清理全部（含高级项）
    python cdisk_cleaner.py --only user_temp,browser_cache -y
    python cdisk_cleaner.py --skip recycle_bin -y

安全说明：
    * 只清理公认的临时/缓存目录，不触碰用户文档、程序本体、系统关键文件
    * 内置受保护路径黑名单，杜绝误删根目录
    * 删除失败（文件被占用/无权限）会静默跳过并计入统计
    * 强烈建议首次运行加 --dry-run 查看将要删除的内容
"""

import argparse
import ctypes
import glob
import os
import stat
import subprocess
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import List

# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #

FILE_ATTRIBUTE_REPARSE_POINT = 0x400

# 受保护路径：绝不删除这些目录本身
_PROTECTED = {
    "c:", "c:\\",
    "c:\\windows",
    "c:\\windows\\system32",
    "c:\\windows\\syswow64",
    "c:\\windows\\winsxs",
    "c:\\users",
    "c:\\programdata",
    "c:\\program files",
    "c:\\program files (x86)",
    "c:\\$recycle.bin",
}


class Color:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    MAGENTA = "\033[35m"


def enable_vt() -> bool:
    """在 Windows 控制台启用 ANSI 转义序列。"""
    try:
        k = ctypes.windll.kernel32
        handle = k.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = wintypes.DWORD()
        if not k.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(k.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


def is_admin() -> bool:
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def relaunch_as_admin() -> bool:
    """以管理员身份重新启动当前脚本。"""
    try:
        script = os.path.abspath(sys.argv[0])
        params = " ".join(f'"{a}"' for a in sys.argv[1:])
        exe = sys.executable
        if getattr(sys, "frozen", False):  # 打包成 exe 的情况
            exe, params = script, params
        else:
            params = f'"{script}" {params}'
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, params, None, 1
        )
        return ret > 32
    except Exception:
        return False


def fmt_size(num: float) -> str:
    if num < 1024:
        return f"{int(num)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        num /= 1024.0
        if num < 1024:
            return f"{num:.2f} {unit}"
    return f"{num:.2f} PB"


def is_protected(path: str) -> bool:
    """判断路径是否属于禁止删除的保护范围。"""
    try:
        p = os.path.normcase(os.path.abspath(path)).rstrip("\\")
    except Exception:
        return True
    if len(p) <= 3:
        return True
    return p in _PROTECTED


def expand_pattern(pattern: str) -> List[str]:
    """展开环境变量与通配符；无法展开时返回空列表。"""
    try:
        p = os.path.expandvars(pattern)
    except Exception:
        return []
    if "%" in p or not os.path.isabs(p):
        return []
    try:
        return glob.glob(p)
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# 统计与删除
# --------------------------------------------------------------------------- #

@dataclass
class Stats:
    files: int = 0      # 删除的文件数
    size: int = 0       # 释放的字节数
    failed: int = 0     # 失败/跳过的数量

    def add(self, n: int) -> None:
        self.files += 1
        self.size += n

    def merge(self, other: "Stats") -> None:
        self.files += other.files
        self.size += other.size
        self.failed += other.failed


def _force_remove_file(path: str) -> bool:
    """尝试删除单个文件，处理只读属性与超长路径。"""
    try:
        os.remove(path)
        return True
    except OSError:
        pass
    try:
        os.chmod(path, stat.S_IWRITE)
    except OSError:
        pass
    try:
        os.remove(path)
        return True
    except OSError:
        pass
    try:  # 超长路径（>260 字符）
        os.remove("\\\\?\\" + os.path.abspath(path))
        return True
    except OSError:
        return False


def remove_tree(path: str, stats: Stats, dry_run: bool = False) -> None:
    """递归删除文件或目录内容；单个失败不影响整体流程。"""
    try:
        st = os.lstat(path)
    except OSError:
        return

    is_dir = stat.S_ISDIR(st.st_mode)
    is_reparse = stat.S_ISLNK(st.st_mode) or bool(
        getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
    )

    if is_dir and not is_reparse:
        # 普通目录：先清空内容，再删除自身
        try:
            with os.scandir(path) as it:
                entries = [e.path for e in it]
        except OSError:
            stats.failed += 1
            return
        for child in entries:
            remove_tree(child, stats, dry_run)
        if not dry_run:
            try:
                os.rmdir(path)
            except OSError:
                pass  # 目录里还有被占用的文件，保留即可
    elif is_dir and is_reparse:
        # 目录联接 / 符号链接：只删链接本身，绝不递归进入
        if not dry_run:
            try:
                os.rmdir(path)
            except OSError:
                stats.failed += 1
    else:
        if dry_run:
            stats.add(st.st_size)
        elif _force_remove_file(path):
            stats.add(st.st_size)
        else:
            stats.failed += 1


# --------------------------------------------------------------------------- #
# 清理项定义
# --------------------------------------------------------------------------- #

@dataclass
class CleanItem:
    key: str                       # 命令行使用的标识
    name: str                      # 显示名称
    desc: str                      # 说明
    patterns: List[str] = field(default_factory=list)
    handler: str = ""              # 特殊处理器：recyclebin / dism / hibernate
    needs_admin: bool = False
    default_on: bool = True        # 默认是否执行（--all 时全部执行）


def build_items() -> List[CleanItem]:
    items: List[CleanItem] = []
    add = items.append

    # ---------------- 系统临时文件 ----------------
    add(CleanItem(
        key="user_temp",
        name="用户临时文件",
        desc="各账户 AppData\\Local\\Temp 下的临时文件（程序运行残留）",
        patterns=[
            r"%LOCALAPPDATA%\Temp\*",
            r"%TEMP%\*",
            r"C:\Users\*\AppData\Local\Temp\*",
        ],
    ))

    add(CleanItem(
        key="system_temp",
        name="系统临时文件",
        desc="C:\\Windows\\Temp 下的系统级临时文件",
        patterns=[r"C:\Windows\Temp\*"],
        needs_admin=True,
    ))

    # ---------------- Windows 更新相关 ----------------
    add(CleanItem(
        key="windows_update",
        name="Windows 更新缓存",
        desc="已下载的更新安装包缓存 SoftwareDistribution\\Download",
        patterns=[
            r"C:\Windows\SoftwareDistribution\Download\*",
            r"C:\Windows\SoftwareDistribution\DataStore\Logs\*",
        ],
        needs_admin=True,
    ))

    add(CleanItem(
        key="delivery_optimization",
        name="传递优化缓存",
        desc="Windows 更新的 P2P 分发缓存（通常几百 MB ~ 数 GB）",
        patterns=[
            r"C:\Windows\ServiceProfiles\NetworkService\AppData\Local"
            r"\Microsoft\Windows\DeliveryOptimization\Cache\*",
        ],
        needs_admin=True,
    ))

    # ---------------- 界面缓存 ----------------
    add(CleanItem(
        key="thumbnail_cache",
        name="缩略图 / 图标缓存",
        desc="资源管理器的 thumbcache / iconcache 数据库（会自动重建）",
        patterns=[
            r"%LOCALAPPDATA%\Microsoft\Windows\Explorer\thumbcache_*.db",
            r"%LOCALAPPDATA%\Microsoft\Windows\Explorer\iconcache_*.db",
        ],
    ))

    add(CleanItem(
        key="system_cache",
        name="系统网络缓存 (INetCache / WebCache)",
        desc="IE / 系统组件的网络缓存与 WebCache 数据库",
        patterns=[
            r"%LOCALAPPDATA%\Microsoft\Windows\INetCache\*",
            r"%LOCALAPPDATA%\Microsoft\Windows\WebCache\*",
        ],
    ))

    # ---------------- 日志与崩溃转储 ----------------
    add(CleanItem(
        key="wer",
        name="错误报告与内存转储",
        desc="WER 错误报告、蓝屏 minidump、内核报告、memory.dmp",
        patterns=[
            r"C:\ProgramData\Microsoft\Windows\WER\ReportArchive\*",
            r"C:\ProgramData\Microsoft\Windows\WER\ReportQueue\*",
            r"C:\ProgramData\Microsoft\Windows\WER\Temp\*",
            r"C:\Users\*\AppData\Local\Microsoft\Windows\WER\*",
            r"C:\Windows\Minidump\*",
            r"C:\Windows\LiveKernelReports\*",
            r"C:\Windows\memory.dmp",
        ],
        needs_admin=True,
    ))

    add(CleanItem(
        key="logs",
        name="系统日志文件",
        desc="CBS / DISM / WindowsUpdate / 安装日志，以及升级残留 Panther 目录",
        patterns=[
            r"C:\Windows\Logs\*",
            r"C:\Windows\Panther\*",
            r"C:\Windows\inf\setupapi.dev.log",
            r"C:\Windows\inf\setupapi.setup.log",
        ],
        needs_admin=True,
    ))

    # ---------------- 其他系统项 ----------------
    add(CleanItem(
        key="prefetch",
        name="预读取文件 (Prefetch)",
        desc="C:\\Windows\\Prefetch 下的 *.pf；删除后首次启动略慢，随后自动重建",
        patterns=[r"C:\Windows\Prefetch\*.pf"],
        needs_admin=True,
    ))

    add(CleanItem(
        key="downloaded_programs",
        name="已下载的程序文件",
        desc="C:\\Windows\\Downloaded Program Files 中的 ActiveX 缓存",
        patterns=[r"C:\Windows\Downloaded Program Files\*"],
        needs_admin=True,
    ))

    add(CleanItem(
        key="recycle_bin",
        name="回收站",
        desc="清空所有磁盘分区的回收站",
        handler="recyclebin",
    ))

    # ---------------- 浏览器缓存 ----------------
    browser_patterns = []
    for root in (
        r"%LOCALAPPDATA%\Google\Chrome\User Data",
        r"%LOCALAPPDATA%\Microsoft\Edge\User Data",
        r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data",
    ):
        browser_patterns += [
            root + r"\*\Cache\*",
            root + r"\*\Code Cache\*",
            root + r"\*\GPUCache\*",
            root + r"\*\Service Worker\CacheStorage\*",
            root + r"\*\Service Worker\ScriptCache\*",
            root + r"\ShaderCache\*",
            root + r"\GrShaderCache\*",
        ]
    browser_patterns += [
        r"%LOCALAPPDATA%\Mozilla\Firefox\Profiles\*\cache2\*",
        r"%LOCALAPPDATA%\Mozilla\Firefox\Profiles\*\startupCache\*",
        r"%LOCALAPPDATA%\Mozilla\Firefox\Profiles\*\shader-cache\*",
    ]

    add(CleanItem(
        key="browser_cache",
        name="浏览器缓存",
        desc="Chrome / Edge / Brave / Firefox 的网页与着色器缓存"
             "（不含书签、密码、Cookie、历史记录）",
        patterns=browser_patterns,
    ))

    # ---------------- 隐私痕迹（默认关闭） ----------------
    add(CleanItem(
        key="recent",
        name="最近使用记录 / 跳转列表",
        desc="快速访问、最近文档、任务栏跳转列表记录",
        patterns=[
            r"%APPDATA%\Microsoft\Windows\Recent\*",
            r"%APPDATA%\Microsoft\Windows\Recent\AutomaticDestinations\*",
            r"%APPDATA%\Microsoft\Windows\Recent\CustomDestinations\*",
        ],
        default_on=False,
    ))

    # ---------------- 高级项（默认关闭） ----------------
    add(CleanItem(
        key="installer_cache",
        name="MSI 补丁缓存 ($PatchCache$)",
        desc="删除后，修复/卸载部分程序时可能提示需要原始安装包",
        patterns=[r"C:\Windows\Installer\$PatchCache$\*"],
        needs_admin=True,
        default_on=False,
    ))

    add(CleanItem(
        key="windows_old",
        name="旧版 Windows 文件 (Windows.old)",
        desc="系统升级备份，删除后无法回退旧版本，通常可释放数 GB ~ 数十 GB",
        patterns=[
            r"C:\Windows.old\*",
            r"C:\$WINDOWS.~BT\*",
            r"C:\$WINDOWS.~WS\*",
            r"C:\$WinREAgent\*",
        ],
        needs_admin=True,
        default_on=False,
    ))

    add(CleanItem(
        key="winsxs",
        name="WinSxS 组件清理 (DISM)",
        desc="调用 DISM 清理被取代的系统组件，可释放数 GB，耗时几分钟",
        handler="dism",
        needs_admin=True,
        default_on=False,
    ))

    add(CleanItem(
        key="hibernation",
        name="休眠文件 (hiberfil.sys)",
        desc="关闭休眠并删除休眠文件，通常释放内存大小的 40%~100%"
             "（会同时关闭「快速启动」）",
        handler="hibernate",
        needs_admin=True,
        default_on=False,
    ))

    return items


# --------------------------------------------------------------------------- #
# 特殊处理器
# --------------------------------------------------------------------------- #

class SHQUERYRBINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("i64Size", ctypes.c_longlong),
        ("i64NumItems", ctypes.c_longlong),
    ]


def recycle_bin_size() -> int:
    info = SHQUERYRBINFO()
    info.cbSize = ctypes.sizeof(SHQUERYRBINFO)
    try:
        if ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info)) == 0:
            return int(info.i64Size)
    except Exception:
        pass
    return 0


def empty_recycle_bin(dry_run: bool = False) -> Stats:
    stats = Stats()
    size = recycle_bin_size()
    if dry_run:
        stats.size = size
        return stats
    SHERB_NOCONFIRMATION = 0x00000001
    SHERB_NOPROGRESSUI = 0x00000002
    SHERB_NOSOUND = 0x00000004
    try:
        ctypes.windll.shell32.SHEmptyRecycleBinW(
            None, None,
            SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND,
        )
        stats.size = size
    except Exception:
        stats.failed += 1
    return stats


def run_dism(dry_run: bool = False) -> Stats:
    stats = Stats()
    if dry_run:
        return stats
    try:
        proc = subprocess.run(
            ["dism.exe", "/Online", "/Cleanup-Image", "/StartComponentCleanup"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3600,
        )
        if proc.returncode != 0:
            stats.failed += 1
    except Exception:
        stats.failed += 1
    return stats


def run_hibernate(dry_run: bool = False) -> Stats:
    stats = Stats()
    size = 0
    try:
        size = os.path.getsize(r"C:\hiberfil.sys")
    except OSError:
        size = 0
    if dry_run:
        stats.size = size
        return stats
    try:
        subprocess.run(
            ["powercfg", "/hibernate", "off"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
        )
        stats.size = size
    except Exception:
        stats.failed += 1
    return stats


def run_item(item: CleanItem, dry_run: bool = False) -> Stats:
    if item.handler == "recyclebin":
        return empty_recycle_bin(dry_run)
    if item.handler == "dism":
        return run_dism(dry_run)
    if item.handler == "hibernate":
        return run_hibernate(dry_run)

    stats = Stats()
    seen = set()
    for pattern in item.patterns:
        for path in expand_pattern(pattern):
            try:
                norm = os.path.normcase(os.path.abspath(path))
            except Exception:
                continue
            if norm in seen:
                continue
            seen.add(norm)
            if is_protected(path):
                continue
            remove_tree(path, stats, dry_run)
    return stats


# --------------------------------------------------------------------------- #
# 展示
# --------------------------------------------------------------------------- #

def print_items(items: List[CleanItem]) -> None:
    print(f"\n{Color.BOLD}可用的清理项：{Color.RESET}\n")
    for it in items:
        flags = []
        if it.needs_admin:
            flags.append("需管理员")
        if not it.default_on:
            flags.append("默认关闭")
        flag = f" {Color.DIM}[{' / '.join(flags)}]{Color.RESET}" if flags else ""
        print(f"  {Color.CYAN}{it.key:<22}{Color.RESET}{it.name}{flag}")
        print(f"  {'':<22}{Color.DIM}{it.desc}{Color.RESET}")
    print()


def print_banner() -> None:
    print(f"{Color.CYAN}{Color.BOLD}")
    print("  ╔══════════════════════════════════════════════╗")
    print("  ║        C 盘深度清理工具  v1.0                ║")
    print("  ╚══════════════════════════════════════════════╝")
    print(f"{Color.RESET}")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def main() -> int:
    if os.name != "nt":
        print("此工具仅支持 Windows 系统。")
        return 1

    # 输出编码兜底
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser(
        description="Windows C 盘深度清理工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="预演模式：只扫描统计，不删除任何文件")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="跳过确认，直接执行")
    parser.add_argument("--all", action="store_true",
                        help="包含默认关闭的高级清理项")
    parser.add_argument("--only", metavar="KEYS",
                        help="只清理指定的项，逗号分隔，如 user_temp,browser_cache")
    parser.add_argument("--skip", metavar="KEYS",
                        help="跳过指定的项，逗号分隔")
    parser.add_argument("--list", action="store_true",
                        help="列出所有清理项后退出")
    parser.add_argument("--no-elevate", action="store_true",
                        help="不自动请求管理员权限")
    args = parser.parse_args()

    # 颜色支持
    if sys.stdout.isatty():
        if not enable_vt():
            for attr in list(vars(Color)):
                if attr.isupper():
                    setattr(Color, attr, "")
    else:
        for attr in list(vars(Color)):
            if attr.isupper():
                setattr(Color, attr, "")

    items = build_items()

    if args.list:
        print_items(items)
        return 0

    print_banner()

    admin = is_admin()

    # 非管理员时询问是否提权
    if not admin and not args.dry_run and not args.no_elevate:
        print(f"{Color.YELLOW}提示：当前不是管理员，系统级清理项将被跳过。{Color.RESET}")
        try:
            ans = input("是否以管理员身份重新运行？[Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        if ans in ("", "y", "yes"):
            if relaunch_as_admin():
                print("已请求提权，请在新窗口继续操作。")
                return 0
            print(f"{Color.RED}提权失败，继续以普通权限运行。{Color.RESET}\n")

    # 选择清理项
    if args.all:
        selected = list(items)
    else:
        selected = [it for it in items if it.default_on]

    if args.only:
        keys = {k.strip() for k in args.only.split(",") if k.strip()}
        unknown = keys - {it.key for it in items}
        if unknown:
            print(f"{Color.RED}未知的清理项：{', '.join(sorted(unknown))}{Color.RESET}")
            return 1
        selected = [it for it in items if it.key in keys]

    if args.skip:
        keys = {k.strip() for k in args.skip.split(",") if k.strip()}
        selected = [it for it in selected if it.key not in keys]

    if not selected:
        print("没有需要执行的清理项。")
        return 0

    # 打印计划
    print(f"{Color.BOLD}将要清理以下 {len(selected)} 个项目：{Color.RESET}\n")
    for it in selected:
        tag = ""
        if it.needs_admin and not admin:
            tag = f" {Color.YELLOW}[无管理员权限，可能跳过]{Color.RESET}"
        elif it.needs_admin:
            tag = f" {Color.DIM}[管理员]{Color.RESET}"
        print(f"  {Color.CYAN}·{Color.RESET} {Color.BOLD}{it.name}{Color.RESET}{tag}")
        print(f"      {Color.DIM}{it.desc}{Color.RESET}")

    if args.dry_run:
        print(f"\n{Color.YELLOW}== 预演模式：不会删除任何文件 =={Color.RESET}\n")
    elif not args.yes:
        try:
            ans = input(f"\n{Color.BOLD}确认开始清理？[y/N] {Color.RESET}").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans not in ("y", "yes"):
            print("已取消。")
            return 0
        print()

    # 记录初始磁盘状态
    try:
        free_before = __import__("shutil").disk_usage("C:\\").free
    except Exception:
        free_before = 0

    total = Stats()
    t0 = time.time()

    for idx, item in enumerate(selected, 1):
        prefix = f"[{idx:>2}/{len(selected)}] {item.name}"
        print(f"{prefix} ... ", end="", flush=True)

        try:
            st = run_item(item, dry_run=args.dry_run)
        except KeyboardInterrupt:
            print(f"\n{Color.YELLOW}用户中断。{Color.RESET}")
            break
        except Exception as exc:
            st = Stats(failed=1)
            print(f"{Color.RED}出错：{exc}{Color.RESET}")
            continue

        total.merge(st)

        if st.size or st.files:
            verb = "可释放" if args.dry_run else "已释放"
            print(f"{Color.GREEN}{verb} {fmt_size(st.size)}{Color.RESET} "
                  f"{Color.DIM}({st.files} 个文件){Color.RESET}")
        else:
            print(f"{Color.DIM}无内容可清理{Color.RESET}")

        if st.failed:
            print(f"          {Color.YELLOW}跳过 {st.failed} 项"
                  f"（被占用或无权限）{Color.RESET}")

    elapsed = time.time() - t0

    # 汇总
    print(f"\n{Color.BOLD}{'─' * 52}{Color.RESET}")
    if args.dry_run:
        print(f"{Color.BOLD}{Color.YELLOW}预演结束{Color.RESET}")
        print(f"  预计可释放：{Color.GREEN}{Color.BOLD}{fmt_size(total.size)}"
              f"{Color.RESET}")
        print(f"  涉及文件：  {total.files} 个")
    else:
        print(f"{Color.BOLD}{Color.GREEN}清理完成！{Color.RESET}")
        print(f"  删除文件：  {total.files} 个")
        print(f"  释放空间：  {Color.GREEN}{Color.BOLD}{fmt_size(total.size)}"
              f"{Color.RESET}")
        try:
            free_after = __import__("shutil").disk_usage("C:\\").free
            delta = free_after - free_before
            if delta > 0:
                print(f"  C 盘可用：  {fmt_size(free_before)} → "
                      f"{Color.GREEN}{fmt_size(free_after)}{Color.RESET} "
                      f"(+{fmt_size(delta)})")
        except Exception:
            pass

    if total.failed:
        print(f"  {Color.YELLOW}跳过项目：  {total.failed} 个"
              f"（正在被程序占用或无权限）{Color.RESET}")
    print(f"  耗时：      {elapsed:.1f} 秒")
    print(f"{Color.BOLD}{'─' * 52}{Color.RESET}")

    print(f"\n{Color.DIM}小贴士：")
    print("  · 被占用的文件属正常现象，重启系统后再运行一次可释放更多")
    print("  · 如需释放系统还原点空间，可运行：cleanmgr /sageset:1 后按需勾选")
    print(f"  · 本工具不会删除任何用户文档与已安装程序{Color.RESET}\n")

    return 0


if __name__ == "__main__":
    import ctypes

    # 弹出一个系统级确认框
    # 4 代表“是/否”按钮，0x40 代表信息图标
    result = ctypes.windll.user32.MessageBoxW(
        0,
        "是否开始清理C盘临时文件和缓存？\n\n点击“是”开始清理，点击“否”退出。",
        "C盘深度清理工具",
        4 | 0x40
    )

    # 如果用户点了“是”(IDYES=6)
    if result == 6:
        # 自动加上 -y 参数，跳过命令行里的 input 确认
        sys.argv.append('-y')
        try:
            exit_code = main()
        except KeyboardInterrupt:
            print("\n已中断。")
            exit_code = 130
        except Exception:
            import traceback

            # 将崩溃信息写入日志文件
            with open("cleaner_error.log", "w", encoding="utf-8") as f:
                f.write(traceback.format_exc())
            print("\n发生致命错误，详情请查看 cleaner_error.log")
            exit_code = 1
        finally:
            # 清理结束后暂停，防止黑框闪退，方便看结果
            input("\n清理结束，按回车键退出窗口...")
            sys.exit(exit_code)
    else:
        sys.exit(0)