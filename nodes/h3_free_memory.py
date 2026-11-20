# -*- coding: utf-8 -*-
"""H3 释放内存：串在流程里的「透传」节点。输入什么原样输出什么，经过时按开关释放内存。

每个功能都是独立开关：
- 卸载所有模型：把 ComfyUI 已加载的模型全部卸载（显存和为模型占的内存）。
- 清空显存缓存：torch.cuda.empty_cache()。
- 回收 Python 内存：gc.collect()。
- 还给操作系统：Linux 上调用 malloc_trim，把 Python 已经释放、但还被进程占着的内存交还给系统。
  （系统看到的「已用内存」降下来，才不会因为内存不够被系统直接杀掉进程。Windows / macOS 上这项没有效果。）
它只能释放「没人再用」的内存；还在被后面节点使用的数据（比如潜空间）不会被动。
"""
from __future__ import annotations

import ctypes
import gc
import logging
import sys

_LOG = logging.getLogger("h3_free_memory")


class _AnyType(str):
    """任意类型：和任何类型都能连。"""

    def __ne__(self, other):
        return False

    def __eq__(self, other):
        return True

    def __hash__(self):
        return hash("*")


ANY = _AnyType("*")


def _rss_mb():
    try:
        with open("/proc/self/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        pass
    return None


def free_memory(unload_models=True, empty_cuda=True, collect_python=True, trim_os=True):
    """按开关释放内存，返回每一项做了什么的说明列表。各项互相独立，一项失败不影响别的。"""
    notes = []
    before = _rss_mb()
    if unload_models:
        try:
            import comfy.model_management as mm

            mm.unload_all_models()
            notes.append("已卸载所有模型")
        except Exception as exc:  # 不在 ComfyUI 里（比如测试）或接口变了
            notes.append(f"卸载模型失败：{exc}")
    if collect_python:
        try:
            n = gc.collect()
            notes.append(f"已回收 Python 内存（{n} 个对象）")
        except Exception as exc:
            notes.append(f"回收 Python 内存失败：{exc}")
    if empty_cuda:
        try:
            try:
                import comfy.model_management as mm

                mm.soft_empty_cache()
            except Exception:
                pass
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                try:
                    torch.cuda.ipc_collect()
                except Exception:
                    pass
            notes.append("已清空显存缓存")
        except Exception as exc:
            notes.append(f"清空显存缓存失败：{exc}")
    if trim_os:
        if sys.platform.startswith("linux"):
            try:
                ctypes.CDLL("libc.so.6").malloc_trim(0)
                notes.append("已把空闲内存还给操作系统")
            except Exception as exc:
                notes.append(f"还给操作系统失败：{exc}")
        else:
            notes.append("还给操作系统：只在 Linux 上有效，已跳过")
    after = _rss_mb()
    if before is not None and after is not None:
        notes.append(f"进程占用内存 {before:.0f} MB → {after:.0f} MB")
    return notes


class H3FreeMemory:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "卸载所有模型": ("BOOLEAN", {"default": True, "tooltip": "卸载 ComfyUI 已加载的全部模型。后面的节点再用到模型时会重新加载。"}),
                "清空显存缓存": ("BOOLEAN", {"default": True, "tooltip": "清空 CUDA 显存缓存。"}),
                "回收Python内存": ("BOOLEAN", {"default": True, "tooltip": "回收 Python 里没人再用的对象。"}),
                "还给操作系统": ("BOOLEAN", {"default": True, "tooltip": "Linux：把已经释放的内存真正还给系统，系统看到的已用内存才会下降。"}),
            },
            "optional": {
                "输入": (ANY, {"tooltip": "任意类型，原样从「输出」送出。串在流程里，经过这个节点时释放内存。不接也能单独运行。"}),
            },
        }

    RETURN_TYPES = (ANY,)
    RETURN_NAMES = ("输出",)
    FUNCTION = "run"
    CATEGORY = "自定义/H3视频"

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")  # 每次运行都执行

    def run(self, **kwargs):
        import os as _os, time as _tm
        def _wlog(msg):
            _LOG.info("[H3 释放内存] %s", msg)
            try:
                import folder_paths
                d = _os.path.join(folder_paths.get_output_directory(), "h3_logs")
                _os.makedirs(d, exist_ok=True)
                with open(_os.path.join(d, "手动运行_" + _tm.strftime("%Y%m%d") + ".log"), "a", encoding="utf-8") as f:
                    f.write(_tm.strftime("%m-%d %H:%M:%S ") + "释放内存｜" + msg + "\n")
            except Exception:
                pass
        def _vram():
            try:
                import torch
                if torch.cuda.is_available():
                    return f"已分配 {torch.cuda.memory_allocated() / 1e9:.2f}GB，已保留 {torch.cuda.memory_reserved() / 1e9:.2f}GB"
            except Exception:
                pass
            return "（读不到显存）"
        _t0 = _tm.time()
        _wlog(f"▶ 开始｜开关：卸载模型={kwargs.get('卸载所有模型', True)} 清显存={kwargs.get('清空显存缓存', True)} 回收Python={kwargs.get('回收Python内存', True)} 还给系统={kwargs.get('还给操作系统', True)}｜释放前显存：{_vram()}")
        notes = free_memory(
            unload_models=bool(kwargs.get("卸载所有模型", True)),
            empty_cuda=bool(kwargs.get("清空显存缓存", True)),
            collect_python=bool(kwargs.get("回收Python内存", True)),
            trim_os=bool(kwargs.get("还给操作系统", True)),
        )
        _wlog("做了：" + ("；".join(notes) if notes else "没有打开任何开关，什么也没做") + f"｜释放后显存：{_vram()}｜用时 {_tm.time() - _t0:.2f} 秒")
        return (kwargs.get("输入"),)


NODE_CLASS_MAPPINGS = {"H3FreeMemory": H3FreeMemory}
NODE_DISPLAY_NAME_MAPPINGS = {"H3FreeMemory": "H3 释放内存"}
