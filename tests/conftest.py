"""pytest 路径自举配置：确保在任意工作目录下直接运行 pytest 都能解析 irmia 包。"""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_ROOT = str(Path(__file__).resolve().parent.parent)
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)
