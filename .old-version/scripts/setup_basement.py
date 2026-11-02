#!/usr/bin/env python3
"""拉取官方「中国移动云盘」Agent Skill 到 ``basement/``。

官方 Skill 不在本仓库内（``basement/`` 已被 ``.gitignore`` 排除，其中含 ``.env``
凭据），但源码运行、editable 安装与 CI 都需要它先落到 ``<repo>/basement/``。

两个必须守住的点：

1. **版本要锁死**。SkillHub 上同一个 slug 后来发过 2.x：模块布局从
   ``scripts/cm_cloud_client.py`` 换成了 ``cmcloud_core`` / ``core_*`` /
   ``workflow_*``，不再提供 ``cm_cloud_client`` / ``cm_cloud_sandbox`` /
   ``cm_cloud_transport`` / ``cm_cloud_errors`` / ``cm_cloud_format`` /
   ``cm_cloud_auth`` / ``cm_cloud_config``。本层是按 1.x 写的，所以默认锁
   ``1.0.4``，需要升级时用 ``MCLOUD_SKILL_VERSION`` 显式覆盖。
2. **要整包解压**。``cm_cloud_config`` 读的是 ``basement/config.json`` 与
   ``basement/.env``（``SKILL_DIR = scripts/ 的上一级``）；只把 ``scripts/*.py``
   拷进去，会导致所有 import 在 ``ConfigError: 缺少配置文件`` 上炸掉。
"""

import os
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent
BASEMENT_DIR = PROJECT_ROOT / "basement"
TARGET_SCRIPTS_DIR = BASEMENT_DIR / "scripts"

SKILL_SLUG = "@org-vt43r0t0/cm-cloud-manage"
SKILL_URL = f"https://api.skillhub.cn/api/v1/download?slug={SKILL_SLUG}"

# 本层依赖的 1.x 布局；见模块 docstring
DEFAULT_SKILL_VERSION = "1.0.4"
DOWNLOAD_URL_HINT = f"{SKILL_URL}（默认锁定 {DEFAULT_SKILL_VERSION}）"
# 判定 Skill 是否可用的标志文件（1.x 才有）
SKILL_MARKER = "cm_cloud_client.py"
# 与 marker 同属 1.x 包的运行期配置，缺任何一个都说明上一次是「半拉子」状态
CONFIG_FILES = ("config.json", ".env")

DOWNLOAD_ATTEMPTS = 3


def skill_version() -> str:
    return (os.getenv("MCLOUD_SKILL_VERSION") or "").strip() or DEFAULT_SKILL_VERSION


def download_url(version: str) -> str:
    return f"{SKILL_URL}&version={version}"


def is_ready() -> bool:
    return (TARGET_SCRIPTS_DIR / SKILL_MARKER).is_file() and all(
        (BASEMENT_DIR / name).is_file() for name in CONFIG_FILES
    )


def download_zip(version: str, dest: Path) -> None:
    url = download_url(version)
    print(f"📥 正在下载: {url}")
    last_error: Exception | None = None
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0 (compatible; mcloud_FUSE/1.0)"}
            )
            with urllib.request.urlopen(req, timeout=60) as response, dest.open("wb") as out_file:
                shutil.copyfileobj(response, out_file)
            if dest.stat().st_size == 0:
                raise RuntimeError("下载到的文件为空")
            print("✅ 下载完成！")
            return
        except Exception as exc:  # noqa: BLE001 - 统一重试并汇报最后一次错误
            last_error = exc
            print(f"⚠️ 第 {attempt}/{DOWNLOAD_ATTEMPTS} 次下载失败: {exc}")
    raise RuntimeError(f"下载失败（已重试 {DOWNLOAD_ATTEMPTS} 次）: {last_error}")


def locate_skill_root(extract_path: Path) -> Path:
    """在解压结果里定位 Skill 根目录（``scripts/`` 的上一级）。"""
    found = sorted(extract_path.rglob(SKILL_MARKER))
    if found:
        scripts_dir = found[0].parent
        return scripts_dir.parent if scripts_dir.name == "scripts" else scripts_dir
    # 兜底：解压出来直接是扁平的一堆 py 文件
    if list(extract_path.glob("*.py")):
        return extract_path
    raise RuntimeError(
        f"下载到的包里没有 {SKILL_MARKER}；SkillHub 上的布局可能又变了，"
        f"请检查 {DOWNLOAD_URL_HINT}"
    )


def setup_basement(force: bool = False) -> None:
    # 1. 检查是否已经就绪
    if not force and is_ready():
        print(f"✅ basement/ 已就绪（含 {SKILL_MARKER} 与 {', '.join(CONFIG_FILES)}），跳过下载。")
        return

    version = skill_version()
    print(f"🚀 正在从 SkillHub 拉取中国移动云盘 Agent Skill（version={version}）...")
    BASEMENT_DIR.mkdir(parents=True, exist_ok=True)

    workdir = Path(tempfile.mkdtemp(prefix="mcloud-skill-"))
    zip_path = workdir / "skill.zip"
    extract_path = workdir / "extract"
    try:
        # 2. 下载
        download_zip(version, zip_path)

        # 3. 解压并整包落到 basement/
        print("📦 正在解压并部署...")
        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            zip_ref.extractall(extract_path)
        skill_root = locate_skill_root(extract_path)
        shutil.copytree(skill_root, BASEMENT_DIR, dirs_exist_ok=True)

        if not is_ready():
            raise RuntimeError(
                "部署后仍缺少 "
                f"{SKILL_MARKER} 或 {', '.join(CONFIG_FILES)}；"
                "请确认下载到的版本是 1.x"
            )
        print(f"🎉 成功配置！官方 Skill 已部署至: {BASEMENT_DIR.resolve()}")
    except Exception as exc:  # noqa: BLE001 - 统一给出可操作提示
        print(f"❌ 配置 basement 失败: {exc}")
        print(
            "💡 可手动下载 ZIP 并整包解压到 basement/："
            f"{DOWNLOAD_URL_HINT}；"
            "或设置 MCLOUD_BASEMENT_DIR 指向已有的 Skill scripts 目录。"
        )
        sys.exit(1)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    setup_basement(force="--force" in sys.argv[1:])
