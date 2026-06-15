import os
import sys

# 让 tests/ 能 import src/ 下的 local_proxy / common / keep_wsl（模块已从仓库根迁入 src/）
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
