import os
import sys
import zipfile
import shutil
from pathlib import Path
import urllib.request

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent
BASEMENT_DIR = PROJECT_ROOT / "basement"
TARGET_SCRIPTS_DIR = BASEMENT_DIR / "scripts"
ZIP_PATH = BASEMENT_DIR / "skill_temp.zip"
EXTRACT_PATH = BASEMENT_DIR / "temp_extract"

SKILL_URL = "https://api.skillhub.cn/api/v1/download?slug=@org-vt43r0t0/cm-cloud-manage"


def download_and_setup_basement():
    # 1. 检查是否已经存在
    if (TARGET_SCRIPTS_DIR / "cm_cloud_client.py").exists():
        print("✅ basement/scripts/cm_cloud_client.py 已存在，无需重复下载！")
        return

    print("🚀 正在从 SkillHub 自动拉取中国移动云盘 Agent Skill 官方依赖...")
    BASEMENT_DIR.mkdir(parents=True, exist_ok=True)

    # 2. 下载 ZIP 包
    try:
        req = urllib.request.Request(
            SKILL_URL,
            headers={"User-Agent": "Mozilla/5.0 (compatible; mcloud_FUSE/1.0)"}
        )
        print(f"📥 正在下载: {SKILL_URL}")
        with urllib.request.urlopen(req) as response, open(ZIP_PATH, "wb") as out_file:
            shutil.copyfileobj(response, out_file)
        print("✅ 下载完成！")
    except Exception as e:
        print(f"❌ 下载失败: {e}")
        print("💡 请手动下载 ZIP 包并解压至 basement/scripts/ 目录下。")
        sys.exit(1)

    # 3. 解压并整理文件路径
    print("📦 正在解压并整理代码文件...")
    if EXTRACT_PATH.exists():
        shutil.rmtree(EXTRACT_PATH)

    try:
        with zipfile.ZipFile(ZIP_PATH, "r") as zip_ref:
            zip_ref.extractall(EXTRACT_PATH)

        # 查找解压目录下的 scripts/ 文件夹（或者直接搜索 cm_cloud_client.py 所在目录）
        found_scripts = list(EXTRACT_PATH.rglob("cm_cloud_client.py"))

        if not found_scripts:
            # 兼容：如果解压出来直接是 python 文件
            source_dir = EXTRACT_PATH
        else:
            source_dir = found_scripts[0].parent

        TARGET_SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)

        # 将解压出来的 python 脚本移动/复制到 basement/scripts/
        for item in source_dir.iterdir():
            if item.is_file():
                shutil.copy2(item, TARGET_SCRIPTS_DIR / item.name)

        print(f"🎉 成功配置！官方 SDK 模块已部署至: {TARGET_SCRIPTS_DIR.resolve()}")

    except Exception as e:
        print(f"❌ 解压或路径整理失败: {e}")
        sys.exit(1)
    finally:
        # 清理临时文件
        if ZIP_PATH.exists():
            ZIP_PATH.unlink()
        if EXTRACT_PATH.exists():
            shutil.rmtree(EXTRACT_PATH)


if __name__ == "__main__":
    download_and_setup_basement()