"""pytest 公共配置：统一注入仓库根 / basement/scripts / tests 到 sys.path。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _path in (ROOT, ROOT / "basement" / "scripts", ROOT / "tests"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
