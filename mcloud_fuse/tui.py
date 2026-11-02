"""极简 curses TUI 工具箱：边框、菜单、单行输入、消息页。

只用标准库，不引入 ``rich`` / ``textual``。设计上刻意保持小：

- 中文按 ``unicodedata.east_asian_width`` 计算显示宽度，不会被截成半个字；
- 没有颜色或 ``TERM`` 不支持时自动退化为 ``A_REVERSE``，不依赖 256 色；
- 非 TTY 不进入 curses，由 :class:`TuiUnavailable` 让调用方退回行模式；
- 纯逻辑（宽度计算、行编辑）与渲染分离，便于离线测试。
"""

from __future__ import annotations

import curses
import sys
import unicodedata
from dataclasses import dataclass
from typing import Callable, Optional, Sequence, Tuple

MIN_HEIGHT = 16
MIN_WIDTH = 60


class TuiUnavailable(Exception):
    """当前终端不适合 curses。"""


class Cancelled(Exception):
    """用户按 q / Ctrl-C 主动退出。"""


class Back(Exception):
    """用户按 Esc 返回上一步。"""


# --------------------------------------------------------------------- 宽度


def char_width(char: str) -> int:
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def display_width(text: str) -> int:
    return sum(char_width(char) for char in text)


def clip(text: str, width: int) -> str:
    """按显示宽度截断，超宽时以省略号结尾。"""
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text
    out = []
    used = 0
    for char in text:
        cw = char_width(char)
        if used + cw > width - 1:
            break
        out.append(char)
        used += cw
    return "".join(out) + "…"


def wrap_text(text: str, width: int) -> list[str]:
    """按显示宽度折行；优先在空格处断行，中文则逐字断。"""
    if width <= 0:
        return []
    lines: list[str] = []
    for paragraph in str(text).split("\n"):
        if not paragraph:
            lines.append("")
            continue
        line = ""
        used = 0
        last_space = -1
        for char in paragraph:
            cw = char_width(char)
            if used + cw > width:
                if char == " ":
                    # 当前字符是空格：在它前面断开，空格不带到下一行
                    lines.append(line.rstrip())
                    line = ""
                    used = 0
                    last_space = -1
                    continue
                if last_space > 0:
                    head, tail = line[:last_space], line[last_space + 1 :]
                    lines.append(head.rstrip())
                    line = tail + char
                    used = display_width(line)
                else:
                    lines.append(line)
                    line = char
                    used = cw
                last_space = line.rfind(" ")
            else:
                line += char
                used += cw
                if char == " ":
                    last_space = len(line) - 1
        lines.append(line.rstrip())
    return lines


# ------------------------------------------------------------------ 行编辑


@dataclass
class LineEditor:
    """单行输入的纯逻辑部分（不依赖 curses），方便单测。"""

    value: str = ""
    cursor: int = 0

    def __post_init__(self) -> None:
        self.cursor = max(0, min(self.cursor, len(self.value)))

    def insert(self, text: str) -> None:
        self.value = self.value[: self.cursor] + text + self.value[self.cursor :]
        self.cursor += len(text)

    def backspace(self) -> None:
        if self.cursor > 0:
            self.value = self.value[: self.cursor - 1] + self.value[self.cursor :]
            self.cursor -= 1

    def delete(self) -> None:
        if self.cursor < len(self.value):
            self.value = self.value[: self.cursor] + self.value[self.cursor + 1 :]

    def left(self) -> None:
        self.cursor = max(0, self.cursor - 1)

    def right(self) -> None:
        self.cursor = min(len(self.value), self.cursor + 1)

    def home(self) -> None:
        self.cursor = 0

    def end(self) -> None:
        self.cursor = len(self.value)

    def kill_to_start(self) -> None:
        self.value = self.value[self.cursor :]
        self.cursor = 0

    def kill_word(self) -> None:
        if self.cursor == 0:
            return
        index = self.cursor
        while index > 0 and self.value[index - 1] == " ":
            index -= 1
        while index > 0 and self.value[index - 1] != " ":
            index -= 1
        self.value = self.value[:index] + self.value[self.cursor :]
        self.cursor = index


def visible_field(value: str, cursor: int, width: int) -> Tuple[str, int]:
    """返回 (窗口内文本, 光标相对列)，保证光标始终可见。"""
    if width <= 0:
        return "", 0
    start = 0
    while start < cursor and display_width(value[start:cursor]) >= width:
        start += 1
    shown = clip(value[start:], width)
    column = min(display_width(value[start:cursor]), max(0, width - 1))
    return shown, column


# ------------------------------------------------------------------- 渲染


# (颜色对编号, 颜色启用时的附加属性, 无颜色时的退化属性)
# 不能在这里调用 curses.color_pair()：模块导入时还没有 initscr()。
_ATTRS = {
    "title": (1, curses.A_BOLD, curses.A_BOLD),
    "dim": (2, curses.A_DIM, curses.A_DIM),
    "ok": (3, curses.A_BOLD, curses.A_BOLD),
    "err": (4, curses.A_BOLD, curses.A_BOLD),
    "warn": (6, curses.A_BOLD, curses.A_BOLD),
    "selected": (5, curses.A_BOLD | curses.A_REVERSE, curses.A_REVERSE | curses.A_BOLD),
    "normal": (0, 0, 0),
    "accent": (1, curses.A_BOLD, curses.A_BOLD),
}

_BOX = {
    "tl": "┌", "tr": "┐", "bl": "└", "br": "┘", "h": "─", "v": "│",
}


class Screen:
    """一个全屏向导页面。所有方法都自己处理重绘与按键。"""

    def __init__(self, stdscr) -> None:
        self.stdscr = stdscr
        self._colors = False
        try:
            stdscr.keypad(True)
        except curses.error:
            pass
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self._init_colors()

    # ------------------------------------------------------------- 基础

    def _init_colors(self) -> None:
        try:
            if not curses.has_colors():
                return
            curses.start_color()
            try:
                curses.use_default_colors()
                background = -1
            except curses.error:
                background = curses.COLOR_BLACK
            pairs = {
                1: curses.COLOR_CYAN,
                2: curses.COLOR_WHITE,
                3: curses.COLOR_GREEN,
                4: curses.COLOR_RED,
                5: curses.COLOR_CYAN,
                6: curses.COLOR_YELLOW,
            }
            for index, foreground in pairs.items():
                curses.init_pair(index, foreground, background)
            self._colors = True
        except curses.error:
            self._colors = False

    def attr(self, name: str):
        pair, colored_extra, fallback = _ATTRS.get(name, _ATTRS["normal"])
        if self._colors and pair:
            return curses.color_pair(pair) | colored_extra
        return fallback

    def size(self) -> Tuple[int, int]:
        height, width = self.stdscr.getmaxyx()
        return height, width

    def put(self, y: int, x: int, text: str, attr=0) -> None:
        height, width = self.size()
        if y < 0 or y >= height or x >= width or not text:
            return
        text = clip(text, width - x)
        try:
            self.stdscr.addstr(y, x, text, attr)
        except curses.error:
            pass

    def _hline(self, y: int, x: int, width: int) -> None:
        self.put(y, x, _BOX["h"] * max(0, width), self.attr("dim"))

    def _border(self) -> None:
        height, width = self.size()
        line = _BOX["h"] * (width - 2)
        self.put(0, 0, _BOX["tl"] + line + _BOX["tr"], self.attr("dim"))
        for row in range(1, height - 1):
            self.put(row, 0, _BOX["v"], self.attr("dim"))
            self.put(row, width - 1, _BOX["v"], self.attr("dim"))
        self.put(height - 1, 0, _BOX["bl"] + line + _BOX["br"], self.attr("dim"))

    def _ensure_size(self) -> Tuple[int, int]:
        while True:
            height, width = self.size()
            if height >= MIN_HEIGHT and width >= MIN_WIDTH:
                return height, width
            self.stdscr.erase()
            message = f"终端太小（{width}x{height}），请调整到至少 {MIN_WIDTH}x{MIN_HEIGHT}"
            try:
                self.stdscr.addstr(0, 0, clip(message, width - 1))
            except curses.error:
                pass
            self.stdscr.refresh()
            key = self.stdscr.get_wch()
            if key in ("q", "\x03"):
                raise Cancelled()

    def _chrome(self, step: int, total: int, title: str, subtitle: str = "") -> Tuple[int, int, int]:
        """画边框/标题/页脚，返回正文区域的起点与宽度。"""
        height, width = self._ensure_size()
        self.stdscr.erase()
        self._border()
        self.put(1, 2, title, self.attr("title"))
        if total > 0:
            marker = f"步骤 {step}/{total}"
            self.put(1, width - 2 - display_width(marker), marker, self.attr("dim"))
        body_y = 3
        body_width = width - 6
        if subtitle:
            for line in wrap_text(subtitle, body_width)[:3]:
                self.put(body_y, 3, line, self.attr("dim"))
                body_y += 1
            body_y += 1
        return body_y, 3, body_width

    def _footer(self, hint: str) -> None:
        height, width = self.size()
        self.put(height - 2, 3, clip(hint, width - 6), self.attr("dim"))

    def _finish(self) -> None:
        self.stdscr.refresh()

    # ------------------------------------------------------------- 菜单

    def menu(
        self,
        *,
        step: int,
        total: int,
        title: str,
        subtitle: str = "",
        items: Sequence[Tuple[str, str]],
        preamble: Sequence[str] = (),
        selected: int = 0,
        hint: str = "↑↓ 选择   Enter 确认   Esc 上一步   q 退出",
        allow_back: bool = True,
        allow_quit: bool = True,
    ) -> int:
        index = max(0, min(selected, len(items) - 1))
        while True:
            body_y, x, body_width = self._chrome(step, total, title, subtitle)
            for text in preamble:
                for line in wrap_text(text, body_width):
                    self.put(body_y, x, line, self.attr("normal"))
                    body_y += 1
            if preamble:
                body_y += 1
            height, _ = self.size()
            available = height - 3 - body_y
            # 小屏滚动：保证选中项可见
            rows = []
            for item_index, (label, description) in enumerate(items):
                rows.append((item_index, label, wrap_text(description, body_width - 4)))
            offset = 0
            while True:
                used = sum(1 + len(desc) for _, _, desc in rows[offset : index + 1])
                if used <= available or offset >= index:
                    break
                offset += 1
            row = body_y
            for item_index, label, description in rows[offset:]:
                if row >= height - 3:
                    break
                selected_row = item_index == index
                marker = "›" if selected_row else " "
                self.put(
                    row,
                    x,
                    marker + " " + clip(label, body_width - 2),
                    self.attr("selected") if selected_row else self.attr("normal"),
                )
                row += 1
                for line in description[:2]:
                    self.put(row, x + 4, clip(line, body_width - 4), self.attr("dim"))
                    row += 1
            self._footer(hint)
            self._finish()
            key = self.stdscr.get_wch()
            if key in (curses.KEY_UP, "k"):
                index = (index - 1) % len(items)
            elif key in (curses.KEY_DOWN, "j"):
                index = (index + 1) % len(items)
            elif key in ("\n", "\r", curses.KEY_ENTER, " "):
                return index
            elif key in ("\x1b",):
                if allow_back:
                    raise Back()
                raise Cancelled()
            elif key == "\x03":
                raise Cancelled()
            elif key == "q" and allow_quit:
                raise Cancelled()

    # ------------------------------------------------------------- 输入

    def input(
        self,
        *,
        step: int,
        total: int,
        title: str,
        subtitle: str = "",
        default: str = "",
        note: str = "",
        validator: Optional[Callable[[str], Optional[str]]] = None,
        hint: str = "Enter 下一步   Ctrl-U 清空   Esc 上一步",
    ) -> str:
        editor = LineEditor(default.strip(), len(default.strip()))
        error = ""
        while True:
            body_y, x, body_width = self._chrome(step, total, title, subtitle)
            field_width = body_width - 4
            self.put(body_y + 1, x, ">", self.attr("accent"))
            shown, column = visible_field(editor.value, editor.cursor, field_width)
            self.put(body_y + 1, x + 2, shown, self.attr("normal"))
            self.put(body_y + 2, x + 2, _BOX["h"] * field_width, self.attr("dim"))
            if note:
                self.put(body_y + 3, x, clip(note, body_width), self.attr("dim"))
            if error:
                for offset, line in enumerate(wrap_text(error, body_width)[:2]):
                    self.put(body_y + 4 + offset, x, line, self.attr("err"))
            self._footer(hint)
            try:
                curses.curs_set(1)
            except curses.error:
                pass
            try:
                self.stdscr.move(body_y + 1, x + 2 + column)
            except curses.error:
                pass
            self._finish()
            key = self.stdscr.get_wch()
            error = ""
            if key in ("\n", "\r", curses.KEY_ENTER):
                value = editor.value.strip()
                message = validator(value) if validator else None
                if message:
                    error = message
                    continue
                try:
                    curses.curs_set(0)
                except curses.error:
                    pass
                return value
            if key == "\x1b":
                try:
                    curses.curs_set(0)
                except curses.error:
                    pass
                raise Back()
            if key == "\x03":
                raise Cancelled()
            if key in (curses.KEY_BACKSPACE, 127, 8):
                editor.backspace()
            elif key == curses.KEY_DC:
                editor.delete()
            elif key == curses.KEY_LEFT:
                editor.left()
            elif key == curses.KEY_RIGHT:
                editor.right()
            elif key == curses.KEY_HOME:
                editor.home()
            elif key == curses.KEY_END:
                editor.end()
            elif key == "\x15":  # Ctrl-U
                editor.kill_to_start()
            elif key == "\x17":  # Ctrl-W
                editor.kill_word()
            elif isinstance(key, str) and key.isprintable():
                editor.insert(key)

    # ------------------------------------------------------------- 消息

    def message(
        self,
        *,
        step: int,
        total: int,
        title: str,
        lines: Sequence[str],
        hint: str = "Enter 继续   q 退出",
        allow_quit: bool = True,
        kind: str = "normal",
    ) -> None:
        while True:
            body_y, x, body_width = self._chrome(step, total, title)
            height, _ = self.size()
            row = body_y
            for text in lines:
                for line in wrap_text(text, body_width):
                    if row >= height - 3:
                        break
                    self.put(row, x, line, self.attr(kind) if kind != "normal" else 0)
                    row += 1
                row += 0
            self._footer(hint)
            self._finish()
            key = self.stdscr.get_wch()
            if key in ("\n", "\r", curses.KEY_ENTER, " "):
                return
            if key == "\x03" or (key == "q" and allow_quit):
                raise Cancelled()

    def progress(self, *, step: int, total: int, title: str, label: str, done: int) -> None:
        body_y, x, body_width = self._chrome(step, total, title)
        self.put(body_y + 1, x, label, self.attr("normal"))
        bar_width = min(body_width, 40)
        filled = int(bar_width * (done / max(total, 1)))
        self.put(body_y + 3, x, "█" * filled + "░" * (bar_width - filled), self.attr("accent"))
        self._finish()

    # ------------------------------------------------------- 暂时离开 curses

    def suspend(self) -> None:
        try:
            curses.def_prog_mode()
            curses.endwin()
        except curses.error:
            pass

    def resume(self) -> None:
        try:
            curses.reset_prog_mode()
        except curses.error:
            pass
        self.stdscr.refresh()

    def read_pasted(self, instructions: Sequence[str]) -> str:
        """退出 curses，让用户粘贴多行内容（Ctrl-D 结束），再恢复界面。"""
        self.suspend()
        try:
            print()
            for line in instructions:
                print(line)
            print()
            return sys.stdin.read()
        finally:
            self.resume()


def run(callback: Callable[[Screen], object]) -> object:
    """在 curses 里运行 ``callback``；终端不适合时抛 :class:`TuiUnavailable`。"""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise TuiUnavailable("当前不是交互终端")
    try:
        return curses.wrapper(lambda stdscr: callback(Screen(stdscr)))
    except curses.error as exc:
        raise TuiUnavailable(f"无法初始化终端界面：{exc}") from exc
