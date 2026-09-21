"""
ComfyUI-CustomNodes — 自动加载框架
================================================
新增节点：把 .py 文件放入 nodes/ 目录，重启 ComfyUI 即可，不需要修改任何其他文件。

节点文件规范：
  · 文件名不以下划线开头（下划线开头的是公共模块，如 _shared.py、_template.py，不会被当成节点）
  · 文件内包含 NODE_CLASS_MAPPINGS 和 NODE_DISPLAY_NAME_MAPPINGS 字典
  · CATEGORY 格式示例：
      "自定义/高级提示词/提示词翻译"  ← 提示词翻译两个节点
      "自定义/高级提示词/多行提示词"  ← 多行提示词节点
      "自定义/高级提示词/xxx"         ← 新加的节点放这里
"""
import os
import importlib.util
import traceback

WEB_DIRECTORY = "./web"
NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}

_HERE = os.path.dirname(os.path.abspath(__file__))
_NODES_DIR = os.path.join(_HERE, "nodes")
os.makedirs(_NODES_DIR, exist_ok=True)

# 让节点文件能 import _shared 等公共模块
import sys
if _NODES_DIR not in sys.path:
    sys.path.insert(0, _NODES_DIR)

for _fname in sorted(os.listdir(_NODES_DIR)):
    if not _fname.endswith(".py") or _fname.startswith("_"):
        continue
    _path = os.path.join(_NODES_DIR, _fname)
    try:
        _spec = importlib.util.spec_from_file_location(
            f"comfy_custom_nodes.{_fname[:-3]}", _path,
            submodule_search_locations=[_NODES_DIR],
        )
        _mod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        NODE_CLASS_MAPPINGS.update(getattr(_mod, "NODE_CLASS_MAPPINGS", {}))
        NODE_DISPLAY_NAME_MAPPINGS.update(getattr(_mod, "NODE_DISPLAY_NAME_MAPPINGS", {}))
        print(f"[CustomNodes] ✓ {_fname}")
    except Exception:
        print(f"[CustomNodes] ✗ {_fname}")
        traceback.print_exc()
