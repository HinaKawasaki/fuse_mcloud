"""TUI 的纯逻辑部分：中文宽度、折行、行编辑、输入窗口。"""

from __future__ import annotations

from mcloud_fuse.tui import (
    Cancelled,
    LineEditor,
    Screen,
    TuiUnavailable,
    clip,
    display_width,
    run,
    visible_field,
    wrap_text,
)


def test_cjk_width_counts_double():
    assert display_width("abc") == 3
    assert display_width("中文") == 4
    assert display_width("a中b") == 4


def test_clip_respects_display_width():
    assert clip("abcdef", 3) == "ab…"
    assert clip("你好世界", 4) == "你…"
    assert clip("短", 10) == "短"


def test_wrap_text_breaks_chinese_by_width():
    assert wrap_text("你好世界", 4) == ["你好", "世界"]
    assert wrap_text("a b c", 3) == ["a b", "c"]


def test_line_editor_insert_and_backspace():
    editor = LineEditor("ac", 1)
    editor.insert("b")
    assert editor.value == "abc"
    assert editor.cursor == 2
    editor.backspace()
    assert editor.value == "ac"
    assert editor.cursor == 1


def test_line_editor_delete_left_right_home_end():
    editor = LineEditor("abcd", 2)
    editor.delete()
    assert editor.value == "abd"
    editor.home()
    assert editor.cursor == 0
    editor.right()
    assert editor.cursor == 1
    editor.end()
    assert editor.cursor == 3


def test_line_editor_kill_word_and_line():
    editor = LineEditor("foo bar baz", len("foo bar baz"))
    editor.kill_word()
    assert editor.value == "foo bar "
    editor.kill_to_start()
    assert editor.value == ""
    assert editor.cursor == 0


def test_visible_field_keeps_cursor_visible():
    value = "/home/user/mnt/a-very-long-directory-name"
    shown, column = visible_field(value, len(value), 12)
    assert display_width(shown) <= 12
    assert 0 <= column < 12
    assert shown.endswith("name")


def test_visible_field_empty():
    assert visible_field("", 0, 10) == ("", 0)
    assert visible_field("abc", 1, 10) == ("abc", 1)


# --------------------------------------------------------------- 渲染逻辑


class FakeStdscr:
    """最小的 curses window 替身，只实现 Screen 用到的方法。"""

    def __init__(self, keys=(), height=24, width=80):
        self.keys = list(keys)
        self.height = height
        self.width = width
        self.writes = []

    def keypad(self, flag):
        return None

    def getmaxyx(self):
        return self.height, self.width

    def erase(self):
        return None

    def refresh(self):
        return None

    def move(self, y, x):
        return None

    def addstr(self, y, x, text, attr=0):
        self.writes.append((y, x, text, attr))

    def get_wch(self):
        if not self.keys:
            raise AssertionError("界面等待了预期之外的按键")
        return self.keys.pop(0)


def test_screen_menu_moves_and_returns_index():
    window = FakeStdscr(keys=["j", "\n"])
    screen = Screen(window)
    index = screen.menu(
        step=1,
        total=7,
        title="测试菜单",
        preamble=["第一行说明"],
        items=[("选项一", "描述"), ("选项二", "描述")],
    )
    assert index == 1
    assert any("测试菜单" in write[2] for write in window.writes)


def test_screen_input_accepts_text_and_validates():
    window = FakeStdscr(keys=["a", "b", "\n"])
    screen = Screen(window)
    value = screen.input(
        step=2,
        total=7,
        title="输入",
        default="",
        validator=lambda text: None if text == "ab" else "必须是 ab",
    )
    assert value == "ab"


def test_screen_input_reports_validation_error_then_accepts():
    window = FakeStdscr(keys=["\n", "o", "k", "\n"])
    screen = Screen(window)
    value = screen.input(
        step=2,
        total=7,
        title="输入",
        default="",
        validator=lambda text: None if text == "ok" else "无效",
    )
    assert value == "ok"


def test_screen_message_waits_for_enter():
    window = FakeStdscr(keys=["\n"])
    screen = Screen(window)
    screen.message(step=7, total=7, title="完成", lines=["一行结果"])
    assert any("一行结果" in write[2] for write in window.writes)


def test_screen_progress_does_not_block():
    window = FakeStdscr()
    screen = Screen(window)
    screen.progress(step=7, total=7, title="进度", label="正在写入", done=1)
    assert any("正在写入" in write[2] for write in window.writes)


def test_screen_small_terminal_can_be_cancelled():
    window = FakeStdscr(keys=["q"], height=8, width=30)
    screen = Screen(window)
    import pytest

    with pytest.raises(Cancelled):
        screen._ensure_size()


def test_run_requires_tty(monkeypatch):
    import pytest

    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    with pytest.raises(TuiUnavailable):
        run(lambda screen: None)
