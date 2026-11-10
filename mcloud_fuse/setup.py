"""``mcloud-fuse setup``：首次设置向导。

两种形态，共用同一套计划与执行逻辑：

- **TUI**（默认，交互终端）：全屏向导，方向键选择、Esc 返回、q 退出；
- **行模式**（``--no-tui`` / ``--yes`` / 非 TTY）：逐行提问，适合脚本与 CI。

向导只做四类事：导入 token、选挂载点/缓存/云盘范围、写配置、可选安装
``systemd --user`` 服务。不接触账号密码，也不把 token 打印到屏幕或日志。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from mcloud_fuse import service, token_status, tokens, tui, userconfig
from mcloud_fuse.client.web139 import (
    Web139Auth,
    Web139AuthError,
    Web139Client,
    Web139Error,
    Web139Token,
)
from mcloud_fuse.userconfig import UserConfig

STEP_TOTAL = 7

TOKEN_INSTRUCTIONS = (
    "请先在浏览器里拿到 token：",
    "  1. 打开 https://yun.139.com/ 并确认已登录",
    "  2. 按 F12 → Network → 过滤 file/list → 点任意一条请求",
    "  3. 右侧 Request Headers → 复制 `Authorization: Basic ...` 整行",
    "     （或右键该请求 → Copy → Copy as cURL）",
    "",
    "粘贴到下面，按 Ctrl-D 结束；直接 Ctrl-D 表示跳过。",
)


class StepWarning(Exception):
    """非致命失败：配置已保存，但这一步没成功。"""


@dataclass
class SetupPlan:
    config: UserConfig
    existing_token: Optional[Web139Token] = None
    new_token_basic: Optional[str] = None
    verify_api: bool = True


@dataclass
class StepResult:
    name: str
    ok: bool
    detail: str
    fatal: bool = False


# --------------------------------------------------------------------- 校验


def _is_within(child: str, parent: str) -> bool:
    try:
        Path(child).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


def validate_mountpoint(value: str) -> Optional[str]:
    if not value.strip():
        return "挂载点不能为空"
    path = Path(value).expanduser()
    if not path.is_absolute():
        return "请填绝对路径，例如 ~/mnt/mcloud"
    if str(path) == os.path.sep:
        return "不能把云盘挂载到 /"
    if os.path.ismount(path):
        return f"{path} 已经是一个挂载点；请先卸载或换一个目录"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"无法创建挂载点：{exc}"
    if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
        return f"{path} 当前用户不可读写"
    return None


def validate_cache(value: str, mountpoint: str = "") -> Optional[str]:
    if not value.strip():
        return "缓存目录不能为空"
    path = Path(value).expanduser()
    if not path.is_absolute():
        return "请填绝对路径，例如 ~/.cache/mcloud-fuse"
    if mountpoint and (
        _is_within(str(path), mountpoint) or _is_within(mountpoint, str(path))
    ):
        return "缓存目录不能和挂载点互相包含（会导致递归与卸载时误删）"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"无法创建缓存目录：{exc}"
    if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
        return f"{path} 当前用户不可读写"
    return None


def validate_root(value: str) -> Optional[str]:
    text = value.strip() or "/"
    if text == "/":
        return None
    if "\\" in text:
        return "云盘路径请用 / 分隔，例如 /Photos/2026"
    if not text.startswith("/"):
        if "/" in text:
            return "云盘路径必须以 / 开头，例如 /Photos/2026"
        # 兼容旧版直接填 fileId
        return None
    trimmed = text.rstrip("/")
    parts = trimmed.split("/")[1:]
    if not trimmed or not parts or any(part in ("", ".", "..") for part in parts):
        return "云盘路径不能包含空段、. 或 .."
    return None


def _absolute(value: str) -> str:
    return os.path.abspath(os.path.expanduser(value.strip()))


# --------------------------------------------------------------- 计划与执行


def load_existing_token() -> Optional[Web139Token]:
    try:
        return Web139Auth.load(path=tokens.default_token_path()).token
    except (Web139AuthError, OSError):
        return None


def parse_token(raw: str) -> Web139Token:
    basic = tokens.extract_basic(raw)
    token = Web139Token.parse(basic)
    if token.is_expired():
        raise Web139AuthError("这个 token 已经过期，请在浏览器里刷新云盘页面后重新复制。")
    return token


def refresh_existing_token(token: Web139Token) -> Web139Token:
    """尝试用续期接口延长已有 token；成功后原子写回 0600 文件。

    只对已有 token 生效：没有 token 时没有任何东西可以续期，必须粘贴。
    续期失败通常意味着接口要求设备指纹，调用方应退回「重新粘贴」。
    """
    auth = Web139Auth(token, path=None)
    refreshed = Web139Client(auth).refresh_token(force=True)
    tokens.save_basic(refreshed.to_basic(), force=False)
    return refreshed


def _save_token_step(plan: SetupPlan) -> str:
    assert plan.new_token_basic is not None
    result = tokens.save_basic(plan.new_token_basic, path=tokens.default_token_path())
    return result.message


def _save_config_step(plan: SetupPlan) -> str:
    path = userconfig.save(plan.config)
    return f"配置已写入 {path}"


def _verify_step(plan: SetupPlan) -> str:
    try:
        client = Web139Client(Web139Auth.load(path=tokens.default_token_path()))
        item = next(iter(client.iter_files("/")), None)
    except (Web139AuthError, Web139Error, OSError) as exc:
        raise StepWarning(f"账号验证失败：{exc}（配置已保存，可稍后运行 mcloud-fuse doctor）") from exc
    suffix = f"（根目录示例：{item.name}）" if item is not None else "（根目录为空）"
    return "根目录可读" + suffix


def _install_service_step(plan: SetupPlan) -> str:
    result = service.install(plan.config)
    if not result.ok:
        raise StepWarning(result.detail)
    return result.detail


def execute(
    plan: SetupPlan,
    on_step: Callable[[str, int, int], None],
) -> List[StepResult]:
    steps: List[Tuple[str, Callable[[SetupPlan], str]]] = []
    if plan.new_token_basic:
        steps.append(("保存 token", _save_token_step))
    steps.append(("写入配置", _save_config_step))
    if plan.verify_api:
        steps.append(("验证账号", _verify_step))
    if plan.config.install_service:
        steps.append(("安装 systemd 用户服务", _install_service_step))

    results: List[StepResult] = []
    for index, (name, function) in enumerate(steps):
        on_step(name, index, len(steps))
        try:
            detail = function(plan)
        except StepWarning as exc:
            results.append(StepResult(name, False, str(exc)))
            continue
        except Exception as exc:  # noqa: BLE001 - 向导必须把异常转成可读结果
            results.append(StepResult(name, False, str(exc), fatal=True))
            break
        results.append(StepResult(name, True, detail))
    return results


# --------------------------------------------------------------------- 行模式


def _ask(prompt: str, default: str, assume_yes: bool) -> str:
    if assume_yes or not sys.stdin.isatty():
        return default
    try:
        answer = input(f"{prompt} [{default}]: ").strip()
    except EOFError:
        return default
    return answer or default


def _confirm(prompt: str, default: bool, assume_yes: bool) -> bool:
    if assume_yes or not sys.stdin.isatty():
        return default
    suffix = "Y/n" if default else "y/N"
    try:
        answer = input(f"{prompt} [{suffix}]: ").strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer in ("y", "yes", "是")


def _print_token_help() -> None:
    for line in TOKEN_INSTRUCTIONS:
        print(line)


def _read_token_from_stdin() -> str:
    if sys.stdin.isatty():
        _print_token_help()
    return sys.stdin.read()


def _prompt_value(
    label: str,
    default: str,
    validator: Callable[[str], Optional[str]],
    assume_yes: bool,
) -> str:
    while True:
        value = _ask(label, default, assume_yes)
        message = validator(value)
        if message is None:
            return value
        print(f"❌ {message}", file=sys.stderr)
        if assume_yes or not sys.stdin.isatty():
            raise SystemExit(message)


def run_line(plan: SetupPlan, args: argparse.Namespace) -> int:
    cfg = plan.config
    print("\nmcloud-fuse 首次设置（行模式）\n" + "=" * 40)

    if plan.existing_token is not None:
        print(f"token：{tokens.describe(plan.existing_token)}")
    else:
        print("token：未导入")

    if args.refresh_token:
        if plan.existing_token is None:
            print("⚠️  没有已保存的 token，无法自动续期；请先粘贴。", file=sys.stderr)
        else:
            try:
                refreshed = refresh_existing_token(plan.existing_token)
            except (Web139Error, OSError) as exc:
                print(f"⚠️  自动续期失败：{exc}", file=sys.stderr)
            else:
                plan.new_token_basic = refreshed.to_basic()
                plan.existing_token = refreshed
                print(f"✅ 已自动续期：{tokens.describe(refreshed)}")

    if args.force_token or plan.existing_token is None or plan.existing_token.is_expired():
        if args.token_stdin or sys.stdin.isatty():
            raw = _read_token_from_stdin()
            if raw.strip():
                try:
                    token = parse_token(raw)
                except (ValueError, Web139AuthError) as exc:
                    print(f"❌ {exc}", file=sys.stderr)
                    return 1
                plan.new_token_basic = token.to_basic()
                plan.existing_token = token
                print(f"✅ 已读取 token：{tokens.describe(token)}")
            elif plan.existing_token is None or plan.existing_token.is_expired():
                print("❌ 没有可用的 token。", file=sys.stderr)
                return 1
        else:
            print(
                "❌ 没有可用的 token，且当前不是交互终端。\n"
                "   请先运行 mcloud-fuse setup，或通过 --token-stdin 从标准输入导入。",
                file=sys.stderr,
            )
            return 1

    if not args.yes:
        mountpoint = _prompt_value("挂载点", cfg.mountpoint, validate_mountpoint, args.yes)
        root = _prompt_value(
            "云盘路径（/ 表示整个云盘，如 /Photos/2026）", cfg.root, validate_root, args.yes
        )
        cache_dir = _prompt_value(
            "缓存目录",
            cfg.cache_dir,
            lambda value: validate_cache(value, mountpoint),
            args.yes,
        )
        rm_mode = _prompt_value(
            "删除模式 delete/trash",
            cfg.rm_mode,
            lambda value: None if value in ("delete", "trash") else "只能是 delete 或 trash",
            args.yes,
        )
        conflict = _prompt_value(
            "冲突策略 copy/error",
            cfg.conflict,
            lambda value: None if value in ("copy", "error") else "只能是 copy 或 error",
            args.yes,
        )
        cfg = cfg.with_changes(
            mountpoint=_absolute(mountpoint),
            root=root,
            cache_dir=_absolute(cache_dir),
            rm_mode=rm_mode,
            conflict=conflict,
        )
        if service.available() and not args.no_service:
            cfg = cfg.with_changes(
                install_service=_confirm("安装为 systemd --user 服务", cfg.install_service, args.yes)
            )
        elif args.no_service:
            cfg = cfg.with_changes(install_service=False)
    if not service.available():
        cfg = cfg.with_changes(install_service=False)
    plan.config = cfg

    results = execute(plan, lambda name, index, total: print(f"→ [{index + 1}/{total}] {name} ..."))
    print()
    for result in results:
        marker = "✅" if result.ok else ("❌" if result.fatal else "⚠️ ")
        print(f"{marker} {result.name}：{result.detail}")
    print()
    return 0 if not any(result.fatal for result in results) else 1


# ---------------------------------------------------------------------- TUI


class _Wizard:
    def __init__(self, screen: tui.Screen, plan: SetupPlan, force_token: bool):
        self.screen = screen
        self.plan = plan
        self.force_token = force_token

    def run(self) -> SetupPlan:
        if not self.welcome():
            raise tui.Cancelled()
        steps = [
            self.step_token,
            self.step_mountpoint,
            self.step_root,
            self.step_cache,
            self.step_rm_mode,
            self.step_conflict,
            self.step_service,
        ]
        index = 0
        while index < len(steps):
            try:
                steps[index]()
                index += 1
            except tui.Back:
                if index == 0:
                    if not self.welcome():
                        raise tui.Cancelled()
                else:
                    index -= 1
        while True:
            action = self.review()
            if action == "go":
                return self.plan
            if action == "edit":
                index = 0
                while index < len(steps):
                    try:
                        steps[index]()
                        index += 1
                    except tui.Back:
                        index = max(0, index - 1)
                continue
            raise tui.Cancelled()

    # ------------------------------------------------------------------ 页面

    def welcome(self) -> bool:
        lines = [
            "向导会导入 token、选挂载点/缓存/云盘范围、写配置，",
            "并可选安装 systemd --user 服务（登录后自动挂载）。",
            "",
            "注意：Web 接口是第三方逆向接口，不在厂商授权范围内；",
            "账号风控与接口漂移风险由使用者承担（docs/adr/0002）。",
        ]
        choice = self.screen.menu(
            step=0,
            total=0,
            title="mcloud-fuse 首次设置",
            subtitle="按两次 Ctrl-C 可随时退出；Esc 返回上一步。",
            preamble=lines,
            items=[("开始设置", "大约 1 分钟"), ("退出", "不做任何修改")],
            hint="↑↓ 选择   Enter 确认   q 退出",
            allow_back=False,
        )
        return choice == 0

    def step_token(self) -> None:
        existing = self.plan.existing_token
        valid = existing is not None and not existing.is_expired()
        if existing is not None and not self.force_token:
            actions = []
            items = []
            if valid:
                items.append(("使用已保存的令牌", tokens.describe(existing)))
                actions.append("keep")
            items.append(
                (
                    "尝试自动续期（无需粘贴）",
                    "调用续期接口延长有效期；失败时可以再粘贴新 token",
                )
            )
            actions.append("refresh")
            items.append(("重新粘贴令牌", "从浏览器重新复制 Authorization，或整段 cURL"))
            actions.append("paste")
            choice = self.screen.menu(
                step=1,
                total=STEP_TOTAL,
                title="访问令牌",
                subtitle="令牌等同于账号访问权，只保存在本机 0600 文件里；向导不会显示明文。",
                items=items,
                # 有效 token 默认「使用已保存」；已过期时第一项是「尝试自动续期」，
                # 默认选中它，尽量免去重新进浏览器复制。
                selected=0,
            )
            action = actions[choice]
            if action == "keep":
                return
            if action == "refresh":
                self.screen.progress(
                    step=1,
                    total=STEP_TOTAL,
                    title="访问令牌",
                    label="正在请求续期 ...",
                    done=0,
                )
                try:
                    refreshed = refresh_existing_token(existing)
                except (Web139Error, OSError) as exc:
                    self.screen.message(
                        step=1,
                        total=STEP_TOTAL,
                        title="自动续期失败",
                        lines=[
                            str(exc),
                            "",
                            "续期接口可能要求设备指纹；可以继续粘贴新的 token。",
                        ],
                        kind="err",
                    )
                else:
                    self.plan.new_token_basic = refreshed.to_basic()
                    self.plan.existing_token = refreshed
                    self.screen.message(
                        step=1,
                        total=STEP_TOTAL,
                        title="续期成功",
                        lines=[tokens.describe(refreshed), "", "点击 Enter 继续。"],
                        kind="ok",
                    )
                    return

        while True:
            raw = self.screen.read_pasted(TOKEN_INSTRUCTIONS)
            if not raw.strip():
                if valid:
                    return
                self.screen.message(
                    step=1,
                    total=STEP_TOTAL,
                    title="需要令牌",
                    lines=["没有令牌无法访问云盘。请重新粘贴，或按 q 退出后去浏览器复制。"],
                )
                continue
            try:
                token = parse_token(raw)
            except (ValueError, Web139AuthError) as exc:
                self.screen.message(
                    step=1,
                    total=STEP_TOTAL,
                    title="令牌不可用",
                    lines=[str(exc), "", "请重新复制：Network → file/list → Request Headers → Authorization"],
                    kind="err",
                )
                continue
            self.plan.new_token_basic = token.to_basic()
            self.plan.existing_token = token
            self.screen.message(
                step=1,
                total=STEP_TOTAL,
                title="令牌已读取",
                lines=[tokens.describe(token), "", "点击 Enter 继续。"],
                kind="ok",
            )
            return

    def step_mountpoint(self) -> None:
        value = self.screen.input(
            step=2,
            total=STEP_TOTAL,
            title="挂载点",
            subtitle="云盘内容会出现在这个目录；目录不存在时会自动创建。",
            default=self.plan.config.mountpoint,
            note="提示：如果目录里已有文件，挂载后会被暂时遮住，原文件不会被删除。",
            validator=validate_mountpoint,
        )
        self.plan.config = self.plan.config.with_changes(mountpoint=_absolute(value))

    def step_root(self) -> None:
        choice = self.screen.menu(
            step=3,
            total=STEP_TOTAL,
            title="云盘范围",
            subtitle="默认挂载整个云盘。只挂某个目录时，直接填云盘里的路径即可。",
            items=[
                ("整个云盘（推荐）", "根目录显示云盘里的全部文件"),
                ("只挂载某个目录", "例如 /Photos/2026，不用再找 fileId"),
            ],
        )
        if choice == 0:
            self.plan.config = self.plan.config.with_changes(root="/")
            return
        value = self.screen.input(
            step=3,
            total=STEP_TOTAL,
            title="目录路径",
            subtitle="从云盘根开始，以 / 开头；旧版填过的 fileId 也可以继续用。",
            default=self.plan.config.root,
            validator=validate_root,
        )
        self.plan.config = self.plan.config.with_changes(root=value)

    def step_cache(self) -> None:
        note = "读缓存与待上传文件都放这里；写回日志在 ~/.local/state，不会被 tmpfs 清空。"
        try:
            usage = shutil.disk_usage(self.plan.config.cache_dir)
            note += f" 当前可用 {usage.free / (1024 ** 3):.1f} GiB。"
        except OSError:
            pass
        value = self.screen.input(
            step=4,
            total=STEP_TOTAL,
            title="缓存目录",
            subtitle="大文件按需读取与写回上传都依赖它；不要放在挂载点里面。",
            default=self.plan.config.cache_dir,
            note=note,
            validator=lambda text: validate_cache(text, self.plan.config.mountpoint),
        )
        self.plan.config = self.plan.config.with_changes(cache_dir=_absolute(value))

    def step_rm_mode(self) -> None:
        current = self.plan.config.rm_mode
        choice = self.screen.menu(
            step=5,
            total=STEP_TOTAL,
            title="删除方式",
            subtitle="决定 rm / rmdir 的行为。之后可随时修改配置或设 MCLOUD_RM_MODE 覆盖。",
            items=[
                ("永久删除（默认）", "调用 /file/batchDelete，立即释放容量，不能恢复"),
                ("移入回收站", "调用 /recyclebin/batchTrash，可用 mcloud-fuse trash 恢复"),
            ],
            selected=0 if current == "delete" else 1,
        )
        self.plan.config = self.plan.config.with_changes(
            rm_mode="delete" if choice == 0 else "trash"
        )

    def step_conflict(self) -> None:
        current = self.plan.config.conflict
        choice = self.screen.menu(
            step=6,
            total=STEP_TOTAL,
            title="覆盖写冲突",
            subtitle="打开写句柄后会记录远端 updatedAt；提交前发现别人改过时怎么办。",
            items=[
                ("保留冲突副本（默认）", "本地版本另存为 name.conflict-<host>-<ts>，远端原样保留"),
                ("报错并保留写回", "返回 ESTALE，由应用决定是否重试"),
            ],
            selected=0 if current == "copy" else 1,
        )
        self.plan.config = self.plan.config.with_changes(
            conflict="copy" if choice == 0 else "error"
        )

    def step_service(self) -> None:
        if not service.available():
            self.plan.config = self.plan.config.with_changes(install_service=False)
            self.screen.message(
                step=7,
                total=STEP_TOTAL,
                title="启动方式",
                lines=["当前环境没有 systemd（或 systemctl），将只保存配置。", "", "之后可手动运行 `mcloud-fuse` 挂载。"],
            )
            return
        choice = self.screen.menu(
            step=7,
            total=STEP_TOTAL,
            title="启动方式",
            subtitle="systemd --user 服务会在登录后自动挂载，不需要 root，也不开 allow_other。",
            items=[
                ("安装为 systemd 用户服务（推荐）", "开机/登录后自动挂载；用 mcloud-fuse status 查看"),
                ("只保存配置", "之后手动运行 mcloud-fuse（前台，Ctrl-C 卸载）"),
            ],
            selected=0 if self.plan.config.install_service else 1,
        )
        self.plan.config = self.plan.config.with_changes(install_service=choice == 0)

    def review(self) -> str:
        cfg = self.plan.config
        token_text = (
            tokens.describe(self.plan.existing_token)
            if self.plan.existing_token is not None
            else "未导入"
        )
        auto = "开启" if token_status.auto_refresh_enabled() else "关闭"
        auto_line = f"{auto}；{token_status.cadence_text()}"
        lines = [
            f"挂载点    {cfg.mountpoint}",
            f"云盘范围  {'整个云盘' if cfg.root == '/' else cfg.root}",
            f"缓存目录  {cfg.cache_dir}",
            f"删除方式  {'永久删除' if cfg.rm_mode == 'delete' else '移入回收站'}",
            f"冲突策略  {'保留冲突副本' if cfg.conflict == 'copy' else '报错（ESTALE）'}",
            f"令牌      {token_text}",
            f"自动续期  {auto_line}",
            f"启动方式  {'systemd 用户服务' if cfg.install_service else '仅保存配置，手动挂载'}",
        ]
        self.screen.message(
            step=STEP_TOTAL,
            total=STEP_TOTAL,
            title="确认设置",
            lines=lines,
            hint="确认无误按 Enter 继续",
        )
        try:
            choice = self.screen.menu(
                step=STEP_TOTAL,
                total=STEP_TOTAL,
                title="确认设置",
                subtitle="选“重新配置”回到第一步；Esc 也返回修改。",
                items=[
                    ("开始执行", "保存配置并完成安装"),
                    ("重新配置", "保留当前输入，回到第一步"),
                    ("退出", "不写入任何配置"),
                ],
            )
        except tui.Back:
            return "edit"
        return ("go", "edit", "quit")[choice]


def _run_tui(screen: tui.Screen, plan: SetupPlan, force_token: bool) -> Tuple[SetupPlan, List[StepResult]]:
    wizard = _Wizard(screen, plan, force_token)
    plan = wizard.run()
    def on_step(label: str, index: int, total: int) -> None:
        screen.progress(
            step=STEP_TOTAL,
            total=STEP_TOTAL,
            title="正在完成设置",
            label=label,
            done=index,
        )

    results = execute(plan, on_step)
    fatal = any(result.fatal for result in results)
    lines = []
    for result in results:
        marker = "[OK] " if result.ok else ("[!!] " if result.fatal else "[!]  ")
        lines.append(marker + result.name + "：" + result.detail)
    lines.append("")
    if fatal:
        lines.append("配置未完成，请根据上面的错误重试。")
    elif plan.config.install_service:
        lines.append(f"之后可以： ls {plan.config.mountpoint}")
        lines.append("查看状态：mcloud-fuse status    卸载：mcloud-fuse unmount")
    else:
        lines.append("手动挂载：mcloud-fuse")
        lines.append(f"挂载后内容在：{plan.config.mountpoint}")
    screen.message(
        step=STEP_TOTAL,
        total=STEP_TOTAL,
        title="设置完成" if not fatal else "设置未完成",
        lines=lines,
        hint="Enter 退出",
        kind="ok" if not fatal else "err",
        allow_quit=False,
    )
    return plan, results


# --------------------------------------------------------------------- 入口


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mcloud-fuse setup",
        description="首次设置向导：导入 token、写配置、可选安装 systemd 用户服务",
    )
    parser.add_argument("--mountpoint", help="挂载点（默认 ~/mnt/mcloud）")
    parser.add_argument("--root", help="云盘路径（如 /Photos/2026）或旧版 fileId；默认 /")
    parser.add_argument("--cache-dir", help="缓存目录（默认 /tmp/cmcloud-fuse）")
    parser.add_argument("--rm-mode", choices=("delete", "trash"), help="删除语义")
    parser.add_argument("--conflict", choices=("copy", "error"), help="覆盖写冲突策略")
    parser.add_argument("--no-service", action="store_true", help="不安装 systemd 用户服务")
    parser.add_argument("--no-check-api", action="store_true", help="跳过账号连通性检查")
    parser.add_argument("--no-tui", action="store_true", help="使用逐行提问，不进入全屏界面")
    parser.add_argument("--yes", "-y", action="store_true", help="全部使用默认值，不提问")
    parser.add_argument(
        "--token-stdin",
        action="store_true",
        help="从标准输入读取 token（cURL / Authorization 行 / 裸 base64）",
    )
    parser.add_argument(
        "--refresh-token",
        action="store_true",
        help="先尝试用续期接口延长已保存的 token，失败再粘贴",
    )
    parser.add_argument("--force-token", action="store_true", help="即使已有有效 token 也重新导入")
    return parser.parse_args(argv)


def _apply_args(base: UserConfig, args: argparse.Namespace) -> UserConfig:
    changes = {}
    if args.mountpoint:
        changes["mountpoint"] = args.mountpoint
    if args.cache_dir:
        changes["cache_dir"] = args.cache_dir
    if args.root:
        changes["root"] = args.root
    if args.rm_mode:
        changes["rm_mode"] = args.rm_mode
    if args.conflict:
        changes["conflict"] = args.conflict
    if args.no_service:
        changes["install_service"] = False
    return base.with_changes(**changes) if changes else base


def _print_summary(plan: SetupPlan, results: Sequence[StepResult]) -> None:
    print()
    print("=" * 40)
    for result in results:
        marker = "OK " if result.ok else ("ERR" if result.fatal else "WARN")
        print(f"[{marker}] {result.name}：{result.detail}")
    print(f"配置：{userconfig.config_path()}")
    print(f"挂载点：{plan.config.mountpoint}")
    if plan.config.install_service:
        print("查看状态：mcloud-fuse status")
    else:
        print("手动挂载：mcloud-fuse")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    try:
        base = userconfig.load() or UserConfig()
    except userconfig.ConfigError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        print("   可以删掉配置文件后重新运行 mcloud-fuse setup。", file=sys.stderr)
        return 1

    try:
        base = _apply_args(base, args)
    except userconfig.ConfigError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1

    plan = SetupPlan(
        config=base,
        existing_token=load_existing_token(),
        verify_api=not args.no_check_api,
    )

    if args.token_stdin:
        raw = sys.stdin.read()
        if raw.strip():
            try:
                token = parse_token(raw)
            except (ValueError, Web139AuthError) as exc:
                print(f"❌ {exc}", file=sys.stderr)
                return 1
            plan.new_token_basic = token.to_basic()
            plan.existing_token = token

    use_tui = (
        not args.no_tui
        and not args.yes
        and sys.stdin.isatty()
        and sys.stdout.isatty()
    )
    if not use_tui:
        return run_line(plan, args)

    try:
        plan, results = tui.run(lambda screen: _run_tui(screen, plan, args.force_token))
    except tui.TuiUnavailable as exc:
        print(f"⚠️  {exc}，改用行模式。", file=sys.stderr)
        return run_line(plan, args)
    except tui.Cancelled:
        print("已取消，未做任何修改。")
        return 130

    _print_summary(plan, results)
    return 0 if not any(result.fatal for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
