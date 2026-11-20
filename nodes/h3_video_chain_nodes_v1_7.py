# -*- coding: utf-8 -*-
# H3 视频续接工具 v1.7
# 本文件适配 ComfyUI-Prompt 的自动加载框架。
# 音频时间锚定算法参考并改编自 NikoDemon80/ComfyUI-H3-Motion-Context，
# Copyright (C) 2026 NikoDemon80，原项目采用 GNU GPL v3.0。
# 本文件是经过修改的独立实现，修改日期：2026-11-06。
# 「引导」部分改编自 ComfyUI_MiniMaxH3_Director（Apache License 2.0）。
# 本文件整体按 GNU GPL v3.0 发布，见同目录 LICENSE-GPL-3.0.txt。

"""MiniMax H3 AV latent continuation nodes for the ComfyUI-Prompt auto-loader.

Nodes:
- H3 潜空间前缀复制: copy the previous video+audio latent tail into the next
  segment's prefix and lock it with a noise mask (复制 = whole-prefix lock,
  引导 = progressive remask, 硬切 = no join).
- H3 潜空间保存 / 读取: save/load paired H3 video+audio tensors.

This is intentionally an H3-specific node set, not a generic video-latent tool.
"""

import gc
import logging
import math
import os
import torch
import comfy.nested_tensor
import folder_paths

try:
    from safetensors.torch import load_file as _st_load, save_file as _st_save
except Exception:  # ComfyUI normally ships safetensors; fail clearly at runtime.
    _st_load = None
    _st_save = None

# ==== H3 TASK COMMON BEGIN（由脚本嵌入，三个文件里这一段必须相同，不要手改） ====
import hashlib as _hl
import json as _js
import logging as _lg
import os as _os
import re as _re
import threading as _th
import time as _tm

# 任务登记表 + 日志。每个任务（一次「输出第几段=0」的运行）一个任务编号（时间戳），
# 登记表只是「记忆」：记下每一段的潜空间文件、视频片段文件，读取/合并只按它来，查不到就报错，绝不去猜「最新的」。
# 成功合并后自动删除；没跑完的超过 H3T_STALE_DAYS 天自动清理。日志单独保留最近 H3T_LOG_KEEP 个。
_H3T_LOG = _lg.getLogger("h3_task")
_H3T_LOCK = _th.RLock()
H3T_LOG_KEEP = 20
H3T_STALE_DAYS = 7
H3T_MS_TYPE = "H3MultiSegmentInput"
_H3T_ID = _re.compile(r"^[0-9A-Za-z_\-]{1,64}$")


def h3t_root():
    import folder_paths

    return _os.path.realpath(folder_paths.get_output_directory())


def h3t_dir(name):
    d = _os.path.join(h3t_root(), name)
    _os.makedirs(d, exist_ok=True)
    return d


def h3t_clean_id(v):
    v = v.strip() if isinstance(v, str) else ""
    return v if _H3T_ID.match(v) else ""


# ------------------------------------------------------------------ 日志（终端 + 文件）
def h3t_log_path(task):
    return _os.path.join(h3t_dir("h3_logs"), task + ".log")


def h3t_log(task, msg, level=_lg.INFO):
    """终端 + 文件。有任务编号写进 h3_logs/<任务编号>.log；没有任务编号（手动单段运行）写进 h3_logs/手动运行_<日期>.log，同一天的手动运行都在这一个文件里。"""
    text = f"[H3 任务 {task}] {msg}" if task else f"[H3] {msg}"
    _H3T_LOG.log(level, text)
    try:
        tag = {_lg.WARNING: "WARN ", _lg.ERROR: "ERROR "}.get(level, "")
        path = h3t_log_path(task) if task else h3t_log_path("手动运行_" + _tm.strftime("%Y%m%d"))
        with _H3T_LOCK, open(path, "a", encoding="utf-8") as f:
            f.write(_tm.strftime("%m-%d %H:%M:%S ") + tag + str(msg) + "\n")
    except Exception:
        pass


# ------------------------------------------------------------------ 详细日志的公共工具：输入输出摘要 + 给节点函数包一层（进入 / 退出 / 出错都记）
def h3t_anchors(cond):
    """条件里所有引导/参考/音频锚点的帧位置，例如「3(图) 22(音频)」；没有锚点返回「无」。"""
    try:
        out = []
        for item in cond or []:
            meta = item[1] if isinstance(item, (list, tuple)) and len(item) > 1 and isinstance(item[1], dict) else {}
            for k in meta.get("minimax_keyframes") or []:
                if not isinstance(k, dict):
                    continue
                kind = "音频" if "audio_latent" in k else "图" if "latent" in k else "?"
                out.append(f"{k.get('resolved_frame_index', '?')}({kind})")
        return " ".join(out) if out else "无"
    except Exception:
        return "（读取失败）"


def h3t_shape(x):
    try:
        return tuple(x.shape)
    except Exception:
        return "?"


def h3t_brief(v, _d=0):
    """把一个输入/输出压成一小段文字：张量只写形状和类型，条件只写锚点位置，字符串截断。不写数据本身，所以日志再长也不会很大。"""
    try:
        if v is None:
            return "无"
        if isinstance(v, (bool, int, float)):
            return str(v)
        if isinstance(v, str):
            t = v.replace("\n", "↵")
            return repr(t if len(t) <= 80 else t[:80] + f"…(共{len(v)}字)")
        if hasattr(v, "tensors") and not hasattr(v, "shape"):
            return "嵌套张量[" + "; ".join(h3t_brief(t, _d + 1) for t in list(v.tensors)[:4]) + "]"
        if hasattr(v, "shape") and hasattr(v, "dtype"):
            dev = getattr(v, "device", None)
            return f"张量{tuple(v.shape)} {v.dtype}" + (f" {dev}" if dev is not None else "")
        if isinstance(v, dict):
            if "samples" in v:
                parts = ["samples=" + h3t_brief(v["samples"], _d + 1)]
                if v.get("noise_mask") is not None:
                    parts.append("noise_mask=" + h3t_brief(v["noise_mask"], _d + 1))
                extra = [k for k in v if k not in ("samples", "noise_mask")]
                if extra:
                    parts.append("其他键=" + ",".join(map(str, extra[:6])))
                return "潜空间{" + "，".join(parts) + "}"
            if "waveform" in v:
                return f"音频{{{h3t_brief(v['waveform'], _d + 1)}，采样率={v.get('sample_rate')}}}"
            if isinstance(v.get("items"), list):
                its = v["items"]
                return f"引导{{{len(its)} 个：" + "，".join(f"第{i.get('frame')}帧 {h3t_brief(i.get('image'), _d + 1)}" for i in its[:8] if isinstance(i, dict)) + "}"
            return "字典{" + ",".join(map(str, list(v)[:8])) + ("…" if len(v) > 8 else "") + "}"
        if isinstance(v, (list, tuple)):
            if v and isinstance(v[0], (list, tuple)) and len(v[0]) == 2 and isinstance(v[0][1], dict) and ("minimax_keyframes" in v[0][1] or "pooled_output" in v[0][1] or len(v[0][1]) >= 0 and hasattr(v[0][0], "shape")):
                return f"条件×{len(v)}，锚点位置={h3t_anchors(v)}"
            if _d >= 2:
                return f"{type(v).__name__}×{len(v)}"
            return "[" + "，".join(h3t_brief(x, _d + 1) for x in list(v)[:6]) + ("…" if len(v) > 6 else "") + "]"
        return type(v).__name__
    except Exception:
        return "（摘要失败）"


def _h3t_kw_brief(kw):
    skip = {"prompt", "extra_pnginfo"}
    return "，".join(f"{k}={h3t_brief(v)}" for k, v in kw.items() if k not in skip)


def h3t_logged(label, task_of=None, log_error=True):
    """给节点的运行函数包一层：进入时记「收到什么」，退出时记「输出什么、用了多久」，出错时记「收到的输入 + 完整报错 + 出错位置」再原样抛出。
    记日志出任何问题都不影响节点本身。task_of(kw) 返回任务编号；不给就从提交内容里找。"""
    import functools as _ft
    import inspect as _ins
    import traceback as _tb

    def _task(kw):
        try:
            if task_of:
                return task_of(kw) or ""
            return (h3t_context(kw.get("prompt"), kw.get("unique_id")) or {}).get("task") or ""
        except Exception:
            return ""

    def _begin(kw):
        t = _task(kw)
        try:
            h3t_log(t, f"▶ 「{label}」开始｜收到：{_h3t_kw_brief(kw)}")
        except Exception:
            pass
        return t, _tm.time()

    def _end(t, t0, out):
        try:
            h3t_log(t, f"◀ 「{label}」完成｜输出：{h3t_brief(out)}｜用时 {_tm.time() - t0:.2f} 秒")
        except Exception:
            pass

    def _fail(t, t0, kw, exc):
        if not log_error:
            return
        try:
            frames = _tb.extract_tb(exc.__traceback__)[-3:]
            where = " ← ".join(f"{_os.path.basename(f.filename)}:{f.lineno} {f.name}" for f in reversed(frames))
            h3t_log(t, f"✖ 「{label}」出错（用时 {_tm.time() - t0:.2f} 秒）：{type(exc).__name__}: {exc}｜出错位置：{where}｜"
                       f"出错时收到的输入：{_h3t_kw_brief(kw)}｜如果报错说尺寸、帧数、通道数对不上，对照上面「收到」里各个输入的形状，看是哪一个口接错了。", _lg.ERROR)
        except Exception:
            pass

    def deco(fn):
        if _ins.iscoroutinefunction(fn):
            @_ft.wraps(fn)
            async def aw(self, *a, **kw):
                t, t0 = _begin(kw)
                try:
                    out = await fn(self, *a, **kw)
                except Exception as exc:
                    _fail(t, t0, kw, exc)
                    raise
                _end(t, t0, out)
                return out
            return aw

        @_ft.wraps(fn)
        def sw(self, *a, **kw):
            t, t0 = _begin(kw)
            try:
                out = fn(self, *a, **kw)
            except Exception as exc:
                _fail(t, t0, kw, exc)
                raise
            _end(t, t0, out)
            return out
        return sw
    return deco


def h3t_prune_logs(keep=None, protect=""):
    """只保留最近 keep 个日志；先删没出过错的旧日志，出过错的尽量留着。"""
    keep = H3T_LOG_KEEP if keep is None else keep
    try:
        d = h3t_dir("h3_logs")
        items = []
        for n in _os.listdir(d):
            if not n.endswith(".log") or n[:-4] == protect:
                continue
            p = _os.path.join(d, n)
            try:
                with open(p, encoding="utf-8", errors="ignore") as f:
                    bad = "ERROR " in f.read()
                items.append((bad, _os.path.getmtime(p), p))
            except OSError:
                continue
        extra = len(items) + (1 if protect else 0) - keep
        for _bad, _m, p in sorted(items)[: max(0, extra)]:
            try:
                _os.remove(p)
            except OSError:
                pass
    except Exception:
        pass


# ------------------------------------------------------------------ 路径（一律限制在 output 目录内）
def h3t_rel(path):
    return _os.path.relpath(_os.path.realpath(path), h3t_root()).replace(_os.sep, "/")


def h3t_abs(rel):
    if not rel or not isinstance(rel, str):
        return None
    root = h3t_root()
    full = _os.path.realpath(_os.path.join(root, rel))
    return full if full != root and full.startswith(root + _os.sep) else None


def h3t_rm(rel):
    p = h3t_abs(rel)
    if p and _os.path.isfile(p):
        try:
            _os.remove(p)
            return True
        except OSError:
            return False
    return False


def h3t_rmdir_if_empty(rel):
    p = h3t_abs(rel)
    if p and _os.path.isdir(p):
        try:
            _os.rmdir(p)
        except OSError:
            pass


# ------------------------------------------------------------------ 登记表
def h3t_reg_path(task):
    return _os.path.join(h3t_dir("h3_tasks"), task + ".json")


def h3t_reg_load(task):
    try:
        with open(h3t_reg_path(task), encoding="utf-8") as f:
            data = _js.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def h3t_reg_update(task, fn):
    with _H3T_LOCK:
        data = h3t_reg_load(task) or {"task": task, "created": _tm.time(), "segments": {}}
        if not isinstance(data.get("segments"), dict):
            data["segments"] = {}
        fn(data)
        data["updated"] = _tm.time()
        path = h3t_reg_path(task)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _js.dump(data, f, ensure_ascii=False)
        _os.replace(tmp, path)
        return data


def h3t_reg_delete(task):
    try:
        _os.remove(h3t_reg_path(task))
    except OSError:
        pass


def h3t_latest_complete_task():
    """没有任务编号时用：登记表里最近更新的、总段数已知且每一段的视频片段文件都在的任务；没有就返回 ""。"""
    try:
        d = h3t_dir("h3_tasks")
        names = [n[:-5] for n in _os.listdir(d) if n.endswith(".json")]
    except OSError:
        return ""
    best, best_t = "", -1.0
    for t in names:
        reg = h3t_reg_load(t) or {}
        total = int(reg.get("total") or 0)
        segs = reg.get("segments") or {}
        if total < 1:
            continue
        ok = True
        for i in range(1, total + 1):
            p = h3t_abs((segs.get(str(i)) or {}).get("video"))
            if not p or not _os.path.isfile(p):
                ok = False
                break
        if ok and float(reg.get("updated") or 0) > best_t:
            best, best_t = t, float(reg.get("updated") or 0)
    return best


def h3t_seam_path(task):
    """接缝检查用的临时记录（上一段结尾的缩小画面）：放在登记表旁边，任务清理时一起删。"""
    return h3t_reg_path(task)[:-5] + ".seam.npz"


def h3t_cleanup_task(task, latents=True, videos=False):
    """成功合并后：删登记表；按开关删这个任务登记过的潜空间/片段文件（只删登记表里列出的，且必须在 output 内）。"""
    data = h3t_reg_load(task) or {}
    n = 0
    if h3t_rm(h3t_seam_path(task)):
        n += 1
    for seg in (data.get("segments") or {}).values():
        if latents and h3t_rm(seg.get("latent")):
            n += 1
        if videos and h3t_rm(seg.get("video")):
            n += 1
    if videos and data.get("seg_dir"):
        h3t_rmdir_if_empty(data["seg_dir"])
    h3t_reg_delete(task)
    return n


def h3t_purge_stale(keep_task=""):
    """没跑完的旧任务（超过 H3T_STALE_DAYS 天没动过）：删登记表和它登记过的潜空间文件。片段视频不动，免得误删你想留的东西。"""
    try:
        d = h3t_dir("h3_tasks")
        now = _tm.time()
        for n in _os.listdir(d):
            if not n.endswith(".json") or n[:-5] == keep_task:
                continue
            p = _os.path.join(d, n)
            if now - _os.path.getmtime(p) < H3T_STALE_DAYS * 86400:
                continue
            task = n[:-5]
            k = h3t_cleanup_task(task, latents=True, videos=False)
            h3t_log("", f"已清理过期任务 {task}（{H3T_STALE_DAYS} 天没动过），删除 {k} 个潜空间文件。")
    except Exception:
        pass


# ------------------------------------------------------------------ 从提交的 PROMPT 里读出「现在是哪个任务的第几段」
def h3t_seg_fp(seg):
    clean = {k: v for k, v in seg.items() if isinstance(k, str) and not k.startswith(("_", "ui"))} if isinstance(seg, dict) else seg
    return _hl.sha1(_js.dumps(clean, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def _h3t_inputs_from(v):
    """API 提示词里一个节点直接连着的上游节点编号（连线写成 [编号, 输出口]）。"""
    out = []
    for x in ((v or {}).get("inputs") or {}).values() if isinstance(v, dict) else []:
        if isinstance(x, list) and len(x) == 2 and isinstance(x[0], (str, int)) and isinstance(x[1], int) and not isinstance(x[1], bool):
            out.append(str(x[0]))
    return out


def h3t_upstream(prompt, uid):
    """uid 这个节点的全部上游节点编号（顺着连线一直往上找）。"""
    seen, stack = set(), [str(uid)]
    while stack:
        k = stack.pop()
        for u in _h3t_inputs_from(prompt.get(k)):
            if u not in seen:
                seen.add(u)
                stack.append(u)
    return seen


def h3t_ms_pick(prompt, uid=None):
    """找出 uid 这个节点属于哪一个「H3 多段输入」。一个工作流里可以有好几个多段输入（各个模式各一个）：
    只有一个就用它；有多个时用 uid 上游连着的那个；还分不出来，就用填了任务编号的那个。返回 (编号, 节点)，找不到返回 (None, None)。"""
    if not isinstance(prompt, dict):
        return None, None
    ms = {str(k): v for k, v in prompt.items() if isinstance(v, dict) and v.get("class_type") == H3T_MS_TYPE}
    if len(ms) <= 1:
        return next(iter(ms.items()), (None, None))
    pool = ms
    if uid is not None and str(uid) != "":
        u = str(uid)
        if u in ms:
            return u, ms[u]
        up = h3t_upstream(prompt, u)
        hit = {k: v for k, v in ms.items() if k in up}
        if len(hit) == 1:
            return next(iter(hit.items()))
        if hit:
            pool = hit
    tasked = {k: v for k, v in pool.items() if h3t_clean_id((v.get("inputs") or {}).get("任务编号"))}
    if len(tasked) == 1:
        return next(iter(tasked.items()))
    return None, None


def h3t_context(prompt, uid=None):
    """没有任务编号（手动单段运行）或找不到「H3 多段输入」节点时返回 None，各节点就按自己的控件手动工作。
    uid：调用这个函数的节点编号。工作流里有多个多段输入时，靠它找到连在自己上游的那一个。"""
    try:
        if not isinstance(prompt, dict):
            return None
        ms_id, ms_node = h3t_ms_pick(prompt, uid)
        if ms_node is None:
            return None
        is_ve = (ms_node.get("inputs") or {}).get("生成模式") == H3T_VE_MODE
        inp = ms_node.get("inputs") or {}
        task = h3t_clean_id(inp.get("任务编号"))
        seg = int(inp.get("输出第几段"))
        tl = _js.loads(inp.get("时间线数据"))
        segs = tl.get("segments")
        if not task or not isinstance(segs, list) or not (1 <= seg <= len(segs)):
            return None
        dj = "硬切" if inp.get("生成模式") == "首尾帧视频" else "复制"
        pj = h3t_prefix_join(prompt, ms_id)  # 前缀复制节点上选的衔接方式；没有这个节点就用模式默认（参考生视频 复制，首尾帧 硬切）
        if pj and not is_ve:
            dj = "硬切" if pj == "图片引导" else pj  # 图片引导只有视频编辑模式有；其他模式当作不衔接
        if is_ve:
            joins = h3t_ve_joins(tl)
        else:
            joins = ["硬切"] + [dj] * (len(segs) - 1)  # 全局一个衔接方式（前缀复制节点上选的），第 1 段永远不衔接
        if is_ve:
            h3t_ve_apply(tl, inp, prompt, ms_id)
            joins = h3t_ve_joins(tl)
        fps = [h3t_seg_fp(s) for s in segs]
        if is_ve:  # 视频/分割点/尺寸变了，之前保存的潜空间都作废
            ve = h3t_ve_settings(tl)
            try:
                ve["mp"] = int(inp.get("参考视频最大边长") or 0)  # 最大边长是节点上的控件
            except (TypeError, ValueError):
                ve["mp"] = 0
            v = tl.get("video") if isinstance(tl.get("video"), dict) else {}
            salt = {"video": [v.get("file"), v.get("start"), v.get("end")], "cuts": tl.get("cuts"),
                    "ve": [ve["mp"], ve["join"], ve["head"]]}
            fps = [h3t_seg_fp({**s, "ve_salt": salt}) if isinstance(s, dict) else h3t_seg_fp(s) for s in segs]
        return {
            "task": task,
            "ms_id": ms_id,
            "audio_mode": h3t_ve_settings(tl)["audio"] if is_ve else "",
            "is_ve": bool(is_ve),
            "seg": seg,
            "total": len(segs),
            "join": joins[seg - 1],
            "joins": joins,
            "fps": fps,
            "hard_start": seg == 1 or joins[seg - 1] == "硬切",
        }
    except Exception:
        return None


def h3t_prefix_nodes(prompt, ms_id=None):
    """工作流里的「前缀复制」节点。给了 ms_id（多段输入的编号）时，只要接在这个多段输入下游的那些；
    一个都没有就退回全部（兼容只有一套流程的旧工作流）。"""
    try:
        pcs = [(str(k), v) for k, v in prompt.items() if isinstance(v, dict) and v.get("class_type") == "MiniMaxH3LatentPrefixCopy"]
    except Exception:
        return []
    if ms_id is not None and len(pcs) > 1:
        mine = [(k, v) for k, v in pcs if str(ms_id) in h3t_upstream(prompt, k)]
        if mine:
            return mine
    return pcs


def h3t_prefix_join(prompt, ms_id=None):
    """从「前缀复制」节点的「衔接方式」下拉框读用户选的方式（复制 / 引导 / 图片引导 / 硬切）；没有这个节点就返回 None。"""
    try:
        for _k, v in h3t_prefix_nodes(prompt, ms_id):
            j = (v.get("inputs") or {}).get("衔接方式")
            if j in ("复制", "引导", "图片引导", "硬切"):
                return j
    except Exception:
        pass
    return None


def h3t_overlap(prompt, default=22, ms_id=None):
    """从「前缀复制」节点读重叠帧数（找不到就用 default）。ms_id：只看接在这个多段输入下游的前缀复制。"""
    try:
        for _k, v in h3t_prefix_nodes(prompt, ms_id):
            n = (v.get("inputs") or {}).get("重叠帧数")
            if isinstance(n, int) and not isinstance(n, bool) and n > 0:
                return n
        for k, v in prompt.items():  # 没有前缀复制节点：视频编辑用时间线数据里的值（默认 22）
            if ms_id is not None and str(k) != str(ms_id):
                continue
            if isinstance(v, dict) and v.get("class_type") == H3T_MS_TYPE and (v.get("inputs") or {}).get("生成模式") == H3T_VE_MODE:
                return h3t_ve_settings(_js.loads(v["inputs"]["时间线数据"]))["head"]
    except Exception:
        pass
    return default


def h3t_next_number(folder, stem, ext):
    """像 ComfyUI 保存图片那样：扫描文件夹里 stem_NNNNN.ext 的最大编号 + 1。"""
    pat = _re.compile("^" + _re.escape(stem) + r"_(\d{5,})\." + _re.escape(ext) + "$")
    top = 0
    try:
        for n in _os.listdir(folder):
            m = pat.match(n)
            if m:
                top = max(top, int(m.group(1)))
    except OSError:
        pass
    return top + 1


# ------------------------------------------------------------------ 视频编辑：分段规划（纯算术）
H3T_VE_MODE = "视频编辑"
H3T_VE_FPS = 24
H3T_VE_HEADS = (5, 22, 39, 56)  # 衔接头：整数个视频潜空间步
H3T_VE_MIN, H3T_VE_MAX = 96, 360  # 模型训练范围（采样帧数）


def h3t_ve_settings(tl):
    ve = tl.get("ve") if isinstance(tl, dict) and isinstance(tl.get("ve"), dict) else {}
    try:
        head = int(ve.get("head", 22))
    except (TypeError, ValueError):
        head = 22
    head = min(H3T_VE_HEADS, key=lambda h: (abs(h - head), -h))  # 不合法就吸附到最近的合法重叠帧数（5/22/39/56）
    return {
        "head": head,
        "join": ve.get("join") if ve.get("join") in ("引导", "复制", "图片引导") else "硬切",
        "long_edge": int(ve.get("long_edge") or 1344),
        "width": int(ve.get("width") or 0),
        "height": int(ve.get("height") or 0),
        "audio": ve.get("audio") if ve.get("audio") in ("静音", "保留原音频", "模型生成") else "静音",
    }


def _h3t_files(items):
    out = []
    for it in items or []:
        f = it.get("file") if isinstance(it, dict) else it
        f = str(f or "").replace("\\", "/").strip()
        if f:
            out.append(f)
    return out


def h3t_ve_refs(tl, i):
    """第 i 段（0 起）实际用到的参考图文件列表：公共 + 本段。"""
    common = (tl.get("common") or {}).get("images") if isinstance(tl.get("common"), dict) else []
    seg = tl["segments"][i]
    return _h3t_files(common) + _h3t_files(seg.get("images") if isinstance(seg, dict) else [])


def h3t_ve_apply(tl, inp, prompt=None, ms_id=None):
    """衔接方式、重叠帧数都读工作流里「H3 潜空间前缀复制」节点上的设置（没有该节点：硬切、22）。
    ms_id：这个多段输入的编号——工作流里有好几套流程时，只读接在它下游的前缀复制。
    结果写进 tl["ve"]（界面也会同步写进时间线数据，这里再覆盖一次，没有界面脚本也一致）。"""
    if isinstance(tl, dict) and isinstance(inp, dict):
        ve = dict(tl.get("ve")) if isinstance(tl.get("ve"), dict) else {}
        pj = h3t_prefix_join(prompt, ms_id) if isinstance(prompt, dict) else None
        if pj:
            ve["join"] = pj  # 衔接方式以「前缀复制」节点上的下拉框为准
        else:
            ve["join"] = "硬切"  # 没有前缀复制节点就没有衔接方式可选：每段独立
        ve["head"] = h3t_overlap(prompt, 22, ms_id) if isinstance(prompt, dict) else ve.get("head", 22)
        tl["ve"] = ve
    return tl


def h3t_ve_joins(tl):
    """视频编辑的每段衔接方式：由前缀复制节点上选的衔接方式决定；第 1 段永远是硬切。"""
    segs = tl["segments"]
    j = h3t_ve_settings(tl)["join"]
    return ["硬切"] + [j] * (len(segs) - 1)


def _round_to(value, lo, step, offset):
    """离 value 最近的 step*k+offset（不小于 lo，平局取较大者）。"""
    k = max(0, int((max(value, lo) - offset) / step + 0.5))
    cand = offset + step * k
    return max(cand, lo)


def h3t_ve_plan(tl, total_frames):
    """按分割点算出每段的帧数。返回 (plan 列表, 警告列表)。

    第一段（以及硬切的段）没有衔接头，采样 = 交付 = 17k+5；其余段交付 17m 帧，采样 = 头 + 交付。
    分割点会吸附到最近的合法帧；最后一段补齐到合法长度，多出来的帧在最后裁掉。"""
    segs = tl["segments"]
    n = len(segs)
    cuts = [float(c) for c in (tl.get("cuts") or [])]
    if len(cuts) != n - 1:
        raise ValueError(f"分割点有 {len(cuts)} 个，应该比分段数少 1（现在 {n} 段）。")
    if any(b <= a for a, b in zip(cuts, cuts[1:])) or (cuts and cuts[0] <= 0):
        raise ValueError("分割点必须按时间从小到大，而且都在视频开头之后。")
    head = h3t_ve_settings(tl)["head"]
    joins = h3t_ve_joins(tl)
    total = int(total_frames)
    plan, warns, pos = [], [], 0
    for i in range(n):
        hard = joins[i] == "硬切"
        h = 0 if hard else head
        lo, step, off = (5, 17, 5) if hard else (17, 17, 0)
        if not hard and pos < h:
            raise ValueError(f"第 {i + 1} 段需要 {h} 帧衔接头，但前面只有 {pos} 帧：请把前面的分割点往后移。")
        if i < n - 1:
            want = int(cuts[i] * H3T_VE_FPS + 0.5) - pos
            delivered = _round_to(want, lo, step, off)
            if pos + delivered >= total:
                raise ValueError(f"第 {i + 1} 段的分割点吸附后已到视频结尾，请把分割点往前移或减少分段。")
        else:
            remain = total - pos
            if remain <= 0:
                raise ValueError("最后一段没有画面：分割点太靠近视频结尾。")
            delivered = _round_to(remain, lo, step, off)
            if delivered < remain:
                delivered += step
        sampled = h + delivered
        if sampled > H3T_VE_MAX:
            raise ValueError(f"第 {i + 1} 段采样 {sampled} 帧，超过模型范围 {H3T_VE_MAX} 帧（约 15 秒）：请再加分割点。")
        if sampled < H3T_VE_MIN:
            warns.append(f"第 {i + 1} 段只有 {sampled} 帧（约 {sampled / H3T_VE_FPS:.1f} 秒），低于模型训练范围 {H3T_VE_MIN} 帧，效果可能变差。")
        plan.append({"index": i, "head": h, "delivered": delivered, "sampled": sampled, "story_start": pos,
                     "story_end": pos + delivered, "join": joins[i], "tail_cut": 0})
        pos += delivered
    plan[-1]["tail_cut"] = pos - total
    return plan, warns
# ==== H3 TASK COMMON END ====


CATEGORY = "自定义/H3视频"


def _split_h3_latent(latent, label="潜空间"):
    """Return the two H3 AV streams: video [B,24,T,H,W], audio [B,32,2,T]."""
    if not isinstance(latent, dict) or "samples" not in latent:
        raise ValueError(f"{label}：不是有效的 ComfyUI LATENT，缺少 samples。")

    samples = latent["samples"]
    if hasattr(samples, "unbind"):
        parts = list(samples.unbind())
    elif isinstance(samples, (tuple, list)):
        parts = list(samples)
    else:
        raise ValueError(
            f"{label}：不是 MiniMax H3 AV 潜空间，samples 类型为 "
            f"{type(samples).__name__}。请连接 H3 原生视频/音频成对潜空间。"
        )

    if len(parts) != 2:
        raise ValueError(f"{label}：H3 AV 潜空间应包含视频和音频两路，实际为 {len(parts)} 路。")

    video, audio = parts
    if not torch.is_tensor(video) or video.ndim != 5 or video.shape[1] != 24:
        raise ValueError(
            f"{label}：视频潜空间应为 [B,24,T,H,W]，实际为 "
            f"{tuple(video.shape) if torch.is_tensor(video) else type(video).__name__}。"
        )
    if not torch.is_tensor(audio) or audio.ndim != 4 or audio.shape[1] != 32 or audio.shape[2] != 2:
        raise ValueError(
            f"{label}：音频潜空间应为 [B,32,2,T]，实际为 "
            f"{tuple(audio.shape) if torch.is_tensor(audio) else type(audio).__name__}。"
        )
    return video, audio


def _normalize_video_mask(mask, video_shape):
    """把各种形状的视频遮罩统一成 [1,1,T,H,W]（float32，1=重新生成，0=锁定）。

    SetLatentNoiseMask 会把遮罩整理成 [T,1,H,W]（时间被当成批次），采样器会报「noise_mask batch 与 latent batch 不一致」；
    这里改回时间在第 3 维的标准形状，时空尺寸与潜空间不同时按最近邻缩放。
    """
    if not torch.is_tensor(mask):
        raise ValueError("视频遮罩不是张量。")
    t, h, w = (int(video_shape[0]), int(video_shape[1]), int(video_shape[2]))
    m = mask.detach().to(dtype=torch.float32)
    if m.ndim == 5:
        if m.shape[1] != 1:
            m = m.amax(dim=1, keepdim=True)
        if m.shape[0] != 1:
            m = m.amax(dim=0, keepdim=True)
        m = m[0, 0]
    elif m.ndim == 4:
        if m.shape[1] == 1:          # [T,1,H,W]（SetLatentNoiseMask 的输出）或 [1,1,H,W]
            m = m[:, 0]
        elif m.shape[0] == 1:        # [1,T,H,W]
            m = m[0]
        else:
            m = m.amax(dim=1)        # [B,C,H,W]：多通道取最大
    elif m.ndim == 3:
        pass                         # [T,H,W]
    elif m.ndim == 2:
        m = m.unsqueeze(0)           # [H,W]：所有时间位置相同
    else:
        raise ValueError(f"视频遮罩维度 {m.ndim} 无法识别。")
    if tuple(m.shape) != (t, h, w):
        m = torch.nn.functional.interpolate(m[None, None], size=(t, h, w), mode="nearest")[0, 0]
    return m.reshape(1, 1, t, h, w).contiguous()


def _extract_user_video_mask(latent, video_shape):
    """取出收到的潜空间里的视频遮罩并整理成 [1,1,T,H,W]。返回 (视频遮罩 或 None, 音频遮罩 或 None)。"""
    mask = latent.get("noise_mask") if isinstance(latent, dict) else None
    if mask is None:
        return None, None
    video_mask = audio_mask = None
    hw = (int(video_shape[1]), int(video_shape[2]))
    parts = None
    if isinstance(mask, (tuple, list)):
        parts = list(mask)
    elif bool(getattr(mask, "is_nested", False)) and hasattr(mask, "unbind"):
        parts = list(mask.unbind())
    if parts is not None:
        if len(parts) == 2:
            video_mask, audio_mask = parts
        elif len(parts) == 1:
            video_mask = parts[0]
        else:
            raise ValueError(f"收到的 noise_mask 应为两路（视频、音频），实际 {len(parts)} 项。")
    elif torch.is_tensor(mask):
        # 单个张量：5 维 / 3 维一定是视频；4 维要看后两维是不是潜空间的高宽（否则当作音频遮罩，不处理）
        if mask.ndim in (3, 5) or (mask.ndim == 4 and tuple(mask.shape[-2:]) == hw):
            video_mask = mask
        else:
            audio_mask = mask
    if video_mask is not None:
        video_mask = _normalize_video_mask(video_mask, video_shape)
    return video_mask, audio_mask


def _audio_mask_trivial(audio_mask):
    """音频遮罩缺省或全是 1（整段重新生成），等于没有。"""
    if audio_mask is None:
        return True
    try:
        return bool(torch.all(audio_mask >= 1.0))
    except Exception:
        return False


def _tokens_for_h3_frames(frame_count):
    """H3 legal overlap grid: 17*k+5 pixel frames -> 5*k+2 latent positions."""
    n = int(frame_count)
    if n < 5 or (n - 5) % 17 != 0:
        raise ValueError(
            f"重叠帧数 {n} 不符合 H3 时间网格。请使用 5、22、39、56……（17×k+5）。"
        )
    return ((n - 5) // 17) * 5 + 2


# MiniMax H3 native timing: video 24 fps, audio latent 40 Hz.
_H3_FPS = 24.0
_H3_AUDIO_HZ = 40.0
_H3_FRAME_RESCALE = 5.0 / 3.0
_H3_FRAME_PER_TOKEN = (1, 4, 4, 4, 4)
_LOG = logging.getLogger("h3_video_chain")
_AUDIO_LAYOUT_CHECKED = False


def _h3_pixel_frames(latent_t):
    """Return the pixel-frame span represented by H3 video latent time tokens."""
    return sum(_H3_FRAME_PER_TOKEN[k % 5] for k in range(int(latent_t)))


def _audio_tail_and_anchor(source_video, source_audio, video_overlap_frames):
    """Slice the previous AV latent audio tail and calculate its H3 timeline anchor.

    Mirrors the H3 Motion Context timeline strategy: take audio directly from the
    previous latent (no VAE round-trip), account for the source clip's +/- 1/3
    audio-step grid overhang, snap the end coordinate to the target 40 Hz grid,
    then place the audio keyframe so its END aligns with the video join.
    """
    if not torch.is_tensor(source_video) or source_video.ndim != 5:
        raise ValueError("上一段视频潜空间形状不正确，必须是 [B,24,T,H,W]。")
    if not torch.is_tensor(source_audio) or source_audio.ndim != 4:
        raise ValueError("上一段音频潜空间形状不正确，必须是 [B,32,2,T]。")
    if source_video.shape[0] != 1 or source_audio.shape[0] != 1:
        raise ValueError("音频续接目前要求上一段视频和音频 batch_size 都为 1。")
    if source_audio.shape[1] != 32 or source_audio.shape[2] != 2:
        raise ValueError(
            f"上一段音频潜空间应为 [1,32,2,T]，实际为 {tuple(source_audio.shape)}。"
        )

    source_frames = _h3_pixel_frames(source_video.shape[2])
    audio_steps_total = int(source_audio.shape[-1])
    overhang = audio_steps_total - (_H3_FRAME_RESCALE * source_frames)
    # H3's legal AV clips differ from the ideal 40 Hz grid by 0 or +/- 1/3 step.
    if not (-0.5 < overhang < 0.5):
        _LOG.warning(
            "H3 音频时间网格与视频跨度偏差异常（audio=%d steps, video=%d frames）；"
            "本次按 0 偏差处理。", audio_steps_total, source_frames
        )
        overhang = 0.0

    frames_for_audio = int(video_overlap_frames)
    audio_steps = int(round(frames_for_audio / _H3_FPS * _H3_AUDIO_HZ))
    if audio_steps > audio_steps_total:
        _LOG.warning(
            "重叠帧数对应 %d 个音频位置，但上一段只有 %d 个；将使用全部音频。",
            audio_steps, audio_steps_total
        )
        audio_steps = audio_steps_total
    if audio_steps < 1:
        raise ValueError("重叠帧数太短，换算后没有可用的音频潜空间位置。")

    audio_tail = source_audio[:1, ..., audio_steps_total - audio_steps:].clone()

    # Match the exact H3 Motion Context timeline calculation. The tail ends at
    # the actual audio end of the source clip, then snaps to the target 40 Hz grid.
    end_frame = float(video_overlap_frames) + overhang / _H3_FRAME_RESCALE
    end_coord = round(_H3_FRAME_RESCALE * end_frame)
    end_frame = end_coord / _H3_FRAME_RESCALE
    anchor_index = end_frame - audio_steps / _H3_FRAME_RESCALE

    return audio_tail, audio_steps, float(anchor_index), float(end_frame), float(overhang)


def _ensure_h3_audio_layout_contract():
    """Check that this ComfyUI H3 PackedLayout honors fractional/negative audio anchors.

    This is a read-only runtime probe. It does not patch ComfyUI. If an upstream
    layout change makes the audio continuation anchor unsafe, fail clearly rather
    than producing a silently shifted soundtrack.
    """
    global _AUDIO_LAYOUT_CHECKED
    if _AUDIO_LAYOUT_CHECKED:
        return
    try:
        import comfy.ldm.minimax.model as mm

        packed_layout = mm.PackedLayout
        frame_rescale = float(mm.FRAME_RESCALE)

        class _ShapeStub:
            def __init__(self, shape):
                self.shape = shape

        # Deliberately exercise a fractional negative anchor, which ordinary
        # Add Guide widgets do not generate.
        test_audio_steps = 40
        test_end_coord = 8
        test_end_frame = test_end_coord / frame_rescale
        test_index = test_end_frame - test_audio_steps / frame_rescale
        if test_index >= 0 or abs(test_index - round(test_index)) < 1e-9:
            raise RuntimeError("内部测试参数没有形成负数小数音频锚点。")

        layout = packed_layout(
            7, 7, 22, 38, 16,
            keyframes=[{
                "resolved_frame_index": test_index,
                "audio_latent": _ShapeStub((1, 32, 2, test_audio_steps)),
            }],
            refs=None,
        )

        audio_segments = [
            (a, b) for a, b, kind in layout.segments if kind == "cond_audio"
        ]
        if len(audio_segments) != 1:
            raise RuntimeError(
                f"音频 Keyframe 产生了 {len(audio_segments)} 个 cond_audio 区段，预期 1 个。"
            )
        a, b = audio_segments[0]
        if b - a != 2 * test_audio_steps:
            raise RuntimeError(
                f"音频条件区段长度为 {b-a} 行，预期 {2*test_audio_steps} 行。"
            )

        target_segments = [
            (a, b, kind) for a, b, kind in layout.segments if kind == "video"
        ]
        if not target_segments:
            raise RuntimeError("布局中没有目标视频区段。")
        target_start, _target_end, _kind = target_segments[-1]
        origin = float(layout.position_ids[target_start, 0])
        positions = layout.position_ids[a:b, 0]
        placed_start = float(positions.min()) - origin
        placed_end = placed_start + float(test_audio_steps)
        expected_end = frame_rescale * test_end_frame
        if abs(placed_end - expected_end) > 1e-6:
            raise RuntimeError(
                f"音频窗口结束坐标错误：实际 {placed_end:.6f}，预期 {expected_end:.6f}。"
            )

        # Also verify that reference blocks do not shift keyframe anchors
        # relative to the target timeline (important for Ref2VA workflows).
        ref_latent = _ShapeStub((1, 24, 1, 22, 38))
        ref_layout = packed_layout(
            7, 7, 22, 38, 16,
            keyframes=[{"resolved_frame_index": 3, "latent": ref_latent}],
            refs=[{"kind": "audio", "ref_audio_t": 8}],
        )
        cond_segments = [
            (a, b) for a, b, kind in ref_layout.segments if kind == "cond"
        ]
        video_segments = [
            (a, b, kind) for a, b, kind in ref_layout.segments if kind == "video"
        ]
        if len(cond_segments) != 1 or not video_segments:
            raise RuntimeError("带参考素材时的关键帧布局结构与预期不一致。")
        ca, _cb = cond_segments[0]
        va, _vb, _vk = video_segments[-1]
        relative = float(ref_layout.position_ids[ca, 0]) - float(ref_layout.position_ids[va, 0])
        if abs(relative - frame_rescale * 3) > 1e-6:
            raise RuntimeError(
                "参考素材改变了锚点相对目标视频的时间位置，音频/视频衔接可能错位。"
            )
    except Exception as exc:
        raise RuntimeError(
            "当前 ComfyUI 的 H3 PackedLayout 未通过音频续接兼容检查。"
            "为了避免声音错位，本次停止执行。原始错误：" + str(exc)
        ) from exc

    _AUDIO_LAYOUT_CHECKED = True
    _LOG.info("H3 音频时间锚定兼容检查通过：支持负数/小数音频 Keyframe 坐标。")


def _append_audio_keyframe_to_conditioning(conditioning, audio_keyframe, overlap_frames, target_video, task=""):
    """Append the audio continuation keyframe and avoid conflicting anchors in locked video prefix."""
    if not isinstance(conditioning, (list, tuple)) or not conditioning:
        raise ValueError("条件输入不是有效的 ComfyUI CONDITIONING，请连接原始 H3 文本/参考条件。")

    target_frame_count = _h3_pixel_frames(target_video.shape[2])
    output = []
    dropped = []
    h3t_log(task, f"  条件里原有的锚点位置：{h3t_anchors(conditioning)}；本段共 {target_frame_count} 帧；前 {overlap_frames} 帧被上一段锁定（落在这里的锚点会被移除）；要加的音频关键帧位置={audio_keyframe.get('resolved_frame_index')}")
    for item in conditioning:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            raise ValueError("CONDITIONING 项目结构不正确，无法安全添加音频续接条件。")
        embedding, extra = item[0], item[1]
        if not isinstance(extra, dict):
            raise ValueError("CONDITIONING 的附加信息不是字典，无法添加 H3 音频 Keyframe。")
        metadata = extra.copy()
        existing = metadata.get("minimax_keyframes") or []
        kept = []
        for keyframe in existing:
            if not isinstance(keyframe, dict):
                raise ValueError("现有 minimax_keyframes 中包含非字典项目。")
            position = keyframe.get("resolved_frame_index", 0)
            if position >= target_frame_count:
                raise ValueError(
                    f"已有参考帧锚定在 {position}，但当前视频只有 {target_frame_count} 帧。"
                    "请检查参考条件和目标 Latent 是否来自同一条工作流分支。"
                )
            # The hard-copied video prefix owns the first N frames. Remove any
            # competing guide/keyframe inside that interval, matching Motion Context.
            if position < overlap_frames:
                dropped.append(position)
                continue
            kept.append(dict(keyframe))
        metadata["minimax_keyframes"] = kept + [dict(audio_keyframe)]
        output.append([embedding, metadata])

    if dropped:
        h3t_log(task, "已移除 %d 个落在硬锁前缀 0..%d 内的冲突参考锚点：%s。（这些引导图因为位置太靠前被上一段的内容盖住了；想让它起作用，请把引导点放到本段更靠后的位置）" % (
            len(dropped), overlap_frames - 1, sorted(set(dropped))), _lg.WARNING)
    h3t_log(task, f"  加完音频关键帧后条件里的锚点位置：{h3t_anchors(output)}")
    return output



def _resolve_output_path(path_or_folder, clip_index):
    """Resolve a file or indexed slot safely inside ComfyUI's output folder."""
    root = os.path.realpath(folder_paths.get_output_directory())
    requested = (path_or_folder or "h3_context").strip().strip('"').strip("'")
    if not requested:
        requested = "h3_context"
    candidate = os.path.realpath(
        requested if os.path.isabs(requested) else os.path.join(root, requested)
    )
    try:
        if os.path.commonpath([root, candidate]) != root:
            raise ValueError("读取路径必须位于 ComfyUI 的 output 目录内。")
    except ValueError:
        raise ValueError("读取路径必须位于 ComfyUI 的 output 目录内。")

    if os.path.isfile(candidate):
        return candidate
    if os.path.isdir(candidate):
        idx = int(clip_index)
        if idx < 1:
            raise ValueError("读取片段编号必须从 1 开始；第一段不需要读取上一段潜空间。")
        suffixes = (f"_{idx:05d}.safetensors", f"_clip{idx:03d}.safetensors")
        matches = [
            os.path.join(candidate, name)
            for name in os.listdir(candidate)
            if name.endswith(suffixes)
        ]
        if not matches:
            raise FileNotFoundError(
                f"没有找到第 {idx} 段潜空间：目录 {candidate} 中不存在 "
                f"*_{{{idx:05d}}}.safetensors。请检查保存编号和读取编号。"
            )
        return max(matches, key=os.path.getmtime)
    raise FileNotFoundError(f"潜空间文件或文件夹不存在：{candidate}")


def _atomic_save_safetensors(path, tensors):
    """Atomic replace avoids partial files and Windows mapped-file overwrite issues."""
    tmp = path + ".tmp"
    try:
        _st_save(tensors, tmp, metadata={"format": "h3_motion_context_av_v1"})
        try:
            os.replace(tmp, path)
        except OSError:
            gc.collect()
            os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 「引导」衔接：前缀软遮罩 + 采样时逐步收紧
# 下面这一段（权重、遮罩、逐步 remask 的思路与实现）改编自 ComfyUI_MiniMaxH3_Director
# 的 director/h3_latent_continue.py，原项目采用 Apache License 2.0，
# 已按本文件的潜空间/遮罩约定改写；Apache-2.0 代码可并入 GPL-3.0。
# ---------------------------------------------------------------------------
_SEAM_TAPER_TOKENS = 4
_SEAM_FLOOR_MAX = 0.95
_AUDIO_SOFT_RELEASE_TICKS = 8
_REMASK_WRAPPER_KEY = "h3_video_chain_prefix_remask"
MODE_COPY = "复制"
MODE_GUIDE = "引导"
MODE_CUT = "硬切"
_MODES = (MODE_COPY, MODE_GUIDE, MODE_CUT)
AUDIO_COPY = "复制"
AUDIO_KEYFRAME = "关键帧"


def _clamp_seam(value):
    """重绘幅度：越大越多重画、越少照抄上一段；0 = 完全锁死。"""
    try:
        n = float(value)
    except (TypeError, ValueError):
        n = 0.10
    if not math.isfinite(n):
        n = 0.10
    return max(0.0, min(_SEAM_FLOOR_MAX, n))


def _prefix_token_weights(prefix_steps, seam_min, taper=_SEAM_TAPER_TOKENS):
    """前缀每个潜空间位置的遮罩值：前面几个 1.0（放开），最后 taper 个从 1.0 渐变到 seam_min。"""
    n = int(prefix_steps)
    if n < 1:
        return ()
    taper = max(1, min(int(taper), n))
    head = n - taper
    floor = _clamp_seam(seam_min)
    weights = [1.0] * head
    weights.extend(1.0 + (floor - 1.0) * (float(i + 1) / float(taper)) for i in range(taper))
    return tuple(weights)


def _audio_mask_values(total, start, end, soft_release):
    """音频遮罩（1=放开，0=锁死）：[start,end) 锁死；soft_release>0 时最后这么多个位置用余弦曲线渐变放开。"""
    total, start, end = int(total), max(0, int(start)), min(int(total), int(end))
    values = [1.0] * total
    for i in range(start, end):
        values[i] = 0.0
    release = min(int(soft_release), max(0, end - start))
    for k in range(1, release + 1):
        values[end - release + k - 1] = 0.5 - 0.5 * math.cos(math.pi * k / float(release))
    return values


def _schedule_values(sigmas):
    if torch.is_tensor(sigmas):
        raw = sigmas.detach().float().reshape(-1).cpu().tolist()
    else:
        raw = list(sigmas or ())
    values = [float(v) for v in raw if math.isfinite(float(v)) and float(v) >= 0.0]
    return tuple(sorted(set(values), reverse=True))


def _next_sigma_ratio(current, sigmas):
    cur = float(current)
    if not math.isfinite(cur) or cur <= 0.0:
        return 0.0
    tol = max(1e-7, abs(cur) * 1e-6)
    for cand in _schedule_values(sigmas):
        if cand < cur - tol:
            return max(0.0, min(1.0, cand / cur))
    return 0.0


def _apply_video_prefix_weights(video_mask, weights):
    """把前缀权重写到遮罩的时间前缀上（支持 5D / 4D / 3D 的遮罩形状）。"""
    n = int(weights.numel())
    if n < 1 or not torch.is_tensor(video_mask):
        return video_mask
    w = weights.to(device=video_mask.device, dtype=video_mask.dtype)
    out = video_mask.clone()
    if video_mask.ndim == 5 and n <= int(video_mask.shape[2]):
        out[:, :, :n] = w.view(1, 1, n, 1, 1)
        return out
    if video_mask.ndim == 3 and n <= int(video_mask.shape[-1]):
        out[..., :n] = w.view(1, 1, n)
        return out
    if video_mask.ndim == 4 and n <= int(video_mask.shape[2]):
        out[:, :, :n] = w.view(1, 1, n, 1)
        return out
    return video_mask


def _unbind_mask(mask):
    if torch.is_tensor(mask):
        return [mask]
    if hasattr(mask, "unbind"):
        return list(mask.unbind())
    if hasattr(mask, "tensors"):
        return list(mask.tensors)
    if isinstance(mask, (tuple, list)):
        return list(mask)
    return None


class _PrefixRemask:
    """采样时按噪声大小逐步收紧前缀：前期放开，越往后越接近「重绘幅度」。"""

    def __init__(self, prefix_steps, sigmas, video_shape, seam_min):
        self.prefix_steps = int(prefix_steps)
        self.sigmas = _schedule_values(sigmas)
        self.video_shape = tuple(int(x) for x in video_shape)
        self.seam_min = _clamp_seam(seam_min)

    def _live_weights(self, sigma, extra_options=None):
        current = float(torch.as_tensor(sigma).detach().float().reshape(-1)[0])
        schedule = self.sigmas or _schedule_values((extra_options or {}).get("sigmas", ()))
        ratio = _next_sigma_ratio(current, schedule)
        floor = float(self.seam_min)
        live = []
        for base in _prefix_token_weights(self.prefix_steps, floor):
            value = float(base) * max(0.0, float(ratio))
            if floor > 0.0:
                value = max(floor, value)
            live.append(max(0.0, min(1.0, value)))
        return torch.tensor(live, dtype=torch.float32)

    def _sync_shapes(self, extra_options=None):
        model = (extra_options or {}).get("model") if extra_options else None
        shapes = getattr(model, "latent_shapes", None) if model is not None else None
        if shapes is None and model is not None:
            inner = getattr(model, "inner_model", None) or getattr(model, "model", None)
            shapes = getattr(inner, "latent_shapes", None)
            if shapes is None and inner is not None:
                shapes = getattr(getattr(inner, "inner_model", None), "latent_shapes", None)
        if not shapes:
            return
        try:
            video = tuple(int(x) for x in shapes[0])
            if len(video) == 5:
                self.video_shape = video
        except Exception:
            return

    @staticmethod
    def _quantize(mask):
        return torch.ceil(mask.float() * 256.0).div(256.0).to(dtype=mask.dtype)

    def denoise_mask_function(self, sigma, denoise_mask, extra_options=None):
        self._sync_shapes(extra_options)
        weights = self._live_weights(sigma, extra_options)

        if torch.is_tensor(denoise_mask) and denoise_mask.ndim == 3 and len(self.video_shape) == 5:
            elems = int(math.prod(self.video_shape[1:]))
            last = int(denoise_mask.shape[-1])
            if last >= elems:
                try:
                    packed = denoise_mask.clone()
                    video = packed[..., :elems].reshape(self.video_shape)
                    video = self._quantize(_apply_video_prefix_weights(video, weights))
                    packed[..., :elems] = video.reshape(packed.shape[0], 1, elems).to(dtype=packed.dtype)
                    return packed
                except RuntimeError:
                    return denoise_mask
            return self._quantize(_apply_video_prefix_weights(denoise_mask, weights))

        streams = _unbind_mask(denoise_mask)
        if streams and torch.is_tensor(streams[0]) and streams[0].ndim == 5:
            video = self._quantize(_apply_video_prefix_weights(streams[0], weights))
            rest = [s for s in streams[1:]]
            if rest:
                return comfy.nested_tensor.NestedTensor((video.to(dtype=streams[0].dtype), rest[0]))
            return video.to(dtype=streams[0].dtype)

        if torch.is_tensor(denoise_mask) and denoise_mask.ndim == 5:
            return self._quantize(_apply_video_prefix_weights(denoise_mask, weights))
        return denoise_mask

    def apply_model_wrapper(self, executor, *args, **kwargs):
        sigma = args[1] if len(args) > 1 else kwargs.get("t")
        mask = kwargs.get("denoise_mask")
        if sigma is None or not torch.is_tensor(mask) or mask.ndim != 5:
            return executor(*args, **kwargs)
        if int(mask.shape[2]) < self.prefix_steps:
            return executor(*args, **kwargs)
        kwargs["denoise_mask"] = self._quantize(_apply_video_prefix_weights(mask, self._live_weights(sigma)))
        return executor(*args, **kwargs)


def _install_prefix_remask(model, prefix_steps, sigmas, video_shape, seam_min):
    """克隆 model 并挂上逐步收紧；任何一步不可用就返回原 model（只剩静态软遮罩），并在日志里说明。"""
    if model is None or not callable(getattr(model, "clone", None)):
        return model
    try:
        patched = model.clone()
        if not callable(getattr(patched, "set_model_denoise_mask_function", None)):
            _LOG.warning("引导：这个 MODEL 没有遮罩钩子，只使用静态软遮罩，不做逐步收紧。")
            return model
        state = _PrefixRemask(prefix_steps, sigmas, video_shape, seam_min)
        patched.set_model_denoise_mask_function(state.denoise_mask_function)
        try:
            from comfy.patcher_extension import WrappersMP

            if callable(getattr(patched, "add_wrapper_with_key", None)):
                patched.add_wrapper_with_key(WrappersMP.APPLY_MODEL, _REMASK_WRAPPER_KEY, state.apply_model_wrapper)
        except Exception as exc:
            _LOG.debug("引导：APPLY_MODEL 包装未安装（%s）。", exc)
        _LOG.info(
            "引导：已启用逐步收紧：前缀 %d 个位置，重绘幅度 %.2f（前期放开，越往后越接近该值）。",
            int(prefix_steps), float(state.seam_min),
        )
        return patched
    except Exception as exc:
        _LOG.warning("引导：逐步收紧未安装（%s），只使用静态软遮罩。", exc)
        return model


def _audio_tail_placement(source_audio, audio_steps, audio_end_coord, target_audio_len):
    """音频尾巴写进下一段音频潜空间的位置：结束在视频衔接点（audio_end_coord），长度 audio_steps。
    返回 (源起点, 目标起点, 目标终点)；超出下一段范围的部分裁掉。"""
    end = min(int(audio_end_coord), int(target_audio_len))
    start = end - int(audio_steps)
    src_from = int(source_audio.shape[-1]) - int(audio_steps)
    if start < 0:
        src_from -= start
        start = 0
    if end <= start:
        raise ValueError("音频尾部放不进下一段的音频潜空间，请检查重叠帧数。")
    return src_from, start, end


class MiniMaxH3LatentPrefixCopy:
    """把上一段末尾接到下一段开头。衔接方式（复制 / 引导 / 图片引导 / 硬切）直接在本节点上选。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "重叠帧数": (
                    "INT",
                    {"default": 22, "min": 5, "max": 362, "step": 17,
                     "tooltip": "H3 合法视频重叠长度：5、22、39、56……；建议先用 22。"},
                ),
                "衔接方式": (
                    [MODE_COPY, MODE_GUIDE, "图片引导", MODE_CUT],
                    {"default": MODE_COPY,
                     "tooltip": "复制：把上一段末尾直接复制到这一段开头，整段前缀锁死，不需要接模型。\n引导：逐步遮罩——前缀前段随采样重新加噪，接缝处最少允许重画「重绘幅度」那么多；必须接「模型」并把输出的 model 接给采样器（没接会退回「复制」并在日志提示）。\n图片引导：不复制潜空间，用上一段最后几帧当图片引导（仅视频编辑模式）。\n硬切：不接上一段，这一段当作全新的镜头。\n第 1 段永远不衔接。用「H3 多段输入」跑全部段时，合并裁剪、断点续跑也都按这里选的方式。"},
                ),
                "音频衔接": (
                    [AUDIO_KEYFRAME, AUDIO_COPY],
                    {"default": AUDIO_KEYFRAME,
                     "tooltip": "关键帧：只给条件加一个音频锚点，遮罩只管视频。官方采样器（只认单个视频遮罩）用这个。\n复制：把上一段音频尾部直接写进下一段音频潜空间并锁定（最后几个位置渐变放开），和 Songssx 节点的音频衔接效果相同；遮罩带音频，需要采样器支持音频遮罩。"},
                ),
                "重绘幅度": (
                    "FLOAT",
                    {"default": 0.10, "min": 0.0, "max": 0.95, "step": 0.01,
                     "tooltip": "仅「引导」有效。接缝处最少允许重画多少：0 = 完全锁死，越大越自由。"},
                ),
                "读取文件夹或文件": (
                    "STRING",
                    {"default": "h3_latent_chain", "tooltip": "仅「手动」用：默认读取 output/h3_latent_chain；旧 H3 Motion Context 文件可改为 h3_context。"},
                ),
                "读取编号": (
                    "INT",
                    {"default": 0, "min": 0, "max": 9999, "step": 1, "control_after_generate": "fixed",
                     "tooltip": "仅「手动」用：0 = 不读上一段（直接运行，不复制），≥1 = 读这个编号的文件。"},
                ),
                "读取方式": (
                    [LOAD_AUTO, LOAD_MANUAL],
                    {"default": LOAD_AUTO,
                     "tooltip": "自动：用「H3 多段输入」跑任务时，按任务登记表自动读上一段（第 1 段和硬切的段不读）。\n手动：按「读取编号」读文件夹里的文件；编号 0 = 不读取，直接运行。"},
                ),
            },
            "optional": {
                "下一段初始潜空间": (
                    "LATENT",
                    {"tooltip": "下一段 H3 图生视频/参考生视频节点输出的初始 AV 潜空间。\n不接：本节点只做读取——读取方式选「手动」、读取编号填 ≥1，就把保存的那一段完整 AV 潜空间原样输出（接音视频分离、解码、合并用）。"},
                ),
                "conditioning": (
                    "CONDITIONING",
                    {"tooltip": "连接原始 H3 文本/参考条件。音频衔接选「关键帧」时会在其中加入音频锚点。只做读取时可以不接。"},
                ),
                "模型": (
                    "MODEL",
                    {"tooltip": "仅「引导」需要：接入后采样时逐步收紧前缀。把输出的 model 接给采样器。"},
                ),
                "sigmas": (
                    "SIGMAS",
                    {"tooltip": "仅「引导」用：采样用的 sigmas，让逐步收紧更准确。不接也能工作。"},
                ),
            },
            "hidden": {"prompt": "PROMPT", "unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("LATENT", "CONDITIONING", "MODEL")
    RETURN_NAMES = ("latent", "conditioning", "model")
    FUNCTION = "copy_prefix_and_mask"
    CATEGORY = CATEGORY

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        # 依赖任务上下文（第几段、衔接方式），每次都必须真正执行，不能被复用
        return _tm.time_ns()

    @staticmethod
    def _load_only(kwargs, conditioning):
        """没接「下一段初始潜空间」：只做读取，把保存的那一段完整 AV 潜空间原样输出（不复制、不加遮罩）。"""
        if _is_auto(kwargs.get("读取方式", LOAD_AUTO)):
            raise ValueError("没接「下一段初始潜空间」时本节点只做读取：请把「读取方式」改成「手动」，并在「读取编号」填要读的那一段的编号（≥1）。")
        n_read = int(kwargs.get("读取编号") or 0)
        if n_read < 1:
            raise ValueError("没接「下一段初始潜空间」时本节点只做读取：「读取编号」要填 ≥1（0 表示不读取）。")
        path = _resolve_output_path(kwargs.get("读取文件夹或文件") or "h3_latent_chain", n_read)
        h3t_log("", f"只读取，不接下一段：{path}")
        return (_read_av_file(path), conditioning, kwargs.get("模型"))

    @h3t_logged("H3 潜空间前缀复制")
    def copy_prefix_and_mask(self, **kwargs):
        target_latent = kwargs.get("下一段初始潜空间")
        conditioning = kwargs.get("conditioning")
        if target_latent is None:
            return self._load_only(kwargs, conditioning)
        overlap_frames = int(kwargs["重叠帧数"])
        audio_mode = kwargs.get("音频衔接", AUDIO_KEYFRAME)
        if audio_mode not in (AUDIO_COPY, AUDIO_KEYFRAME):
            raise ValueError(f"音频衔接「{audio_mode}」无效，应为：关键帧、复制。")
        mode = kwargs.get("衔接方式", MODE_COPY)
        if mode == "图片引导":
            mode = MODE_CUT
        if mode not in _MODES:
            raise ValueError(f"衔接方式「{mode}」无效，应为：复制、引导、图片引导、硬切。")
        # 用「H3 多段输入」跑时，第 1 段、以及逐段单独指定了硬切的段，不接上一段
        ctx = h3t_context(kwargs.get("prompt"), kwargs.get("unique_id"))
        tk = ctx["task"] if ctx else ""
        h3t_log(tk, f"  第 0 步·参数：重叠帧数={overlap_frames}，音频衔接={audio_mode}，衔接方式(控件)={mode}，读取方式={kwargs.get('读取方式')}，找到「H3 多段输入」={'是，第 %s/%s 段，本段衔接=%s，硬切起点=%s' % (ctx['seg'], ctx['total'], ctx['join'], ctx['hard_start']) if ctx else '否（手动运行）'}")
        h3t_log(tk, f"  第 0 步·收到的条件锚点位置：{h3t_anchors(conditioning)}")
        if ctx:
            if ctx["hard_start"] or ctx["join"] == "图片引导":
                mode = MODE_CUT
            elif ctx["join"] in (MODE_COPY, MODE_GUIDE):
                mode = ctx["join"]
        h3t_log(tk, f"  第 0 步·最终衔接方式={mode}")
        seam_min = _clamp_seam(kwargs.get("重绘幅度", 0.10))
        model = kwargs.get("模型")
        sigmas = kwargs.get("sigmas")
        guide = mode == MODE_GUIDE
        dynamic = guide and model is not None
        if mode == MODE_COPY:
            seam_min = 0.0
        # ---- 读取上一段：自动按任务登记表，手动按编号；硬切 / 图片引导不读 ----
        auto = _is_auto(kwargs.get("读取方式", LOAD_AUTO))
        source_latent = None
        if mode != MODE_CUT:
            if auto:
                if ctx and not ctx["hard_start"]:
                    source_latent = _load_registered(ctx)[0]
                elif not ctx:
                    h3t_log("", "「读取方式」是自动，但没有用「H3 多段输入」跑任务，没法自动找上一段：不读取，直接运行。"
                                "（要接旧文件请把读取方式改成手动并填读取编号。）", _lg.WARNING)
            else:
                n_read = int(kwargs.get("读取编号") or 0)
                if ctx and not ctx["hard_start"]:
                    h3t_log(ctx["task"], "手动模式下读取编号是固定的；跑全部段请把读取方式改成「自动」。", _lg.WARNING)
                if n_read >= 1:
                    path = _resolve_output_path(kwargs.get("读取文件夹或文件") or "h3_latent_chain", n_read)
                    h3t_log(ctx["task"] if ctx else "", f"手动读取上一段潜空间：{path}")
                    source_latent = _read_av_file(path, tk)
        h3t_log(ctx["task"] if ctx else "", "H3 续接：进入，衔接方式=%s，音频衔接=%s，读取方式=%s，上一段潜空间=%s，任务=%s 第%s段。" % (
            mode, audio_mode, "自动" if auto else "手动", "有" if source_latent is not None else "无",
            ctx["task"] if ctx else "手动", ctx["seg"] if ctx else "-"))
        self._record(ctx, False)

        # 第一段（读取编号 0 得到 None），或这一段选了「硬切」：不衔接，原样放行。
        if mode == MODE_CUT:
            cut_video, _a = _split_h3_latent(target_latent, "下一段初始潜空间")
            _LOG.info("H3 续接：硬切，本段不接上一段。")
            return (self._with_clean_mask(target_latent, cut_video, audio_mode == AUDIO_COPY), conditioning, model)
        if source_latent is None:
            none_video, _a = _split_h3_latent(target_latent, "下一段初始潜空间")
            if ctx and not ctx["hard_start"]:
                h3t_log(ctx["task"], "H3 续接：本段应当接上一段（第 %d 段，%s），但「上一段原始潜空间」是空的——没有复制。"
                        "请检查读取方式和读取编号。" % (ctx["seg"], mode), _lg.WARNING)
            return (self._with_clean_mask(target_latent, none_video, audio_mode == AUDIO_COPY), conditioning, model)

        source_video, source_audio = _split_h3_latent(source_latent, "上一段原始潜空间")
        target_video, target_audio = _split_h3_latent(target_latent, "下一段初始潜空间")
        n_tokens = _tokens_for_h3_frames(overlap_frames)
        h3t_log(tk, f"  第 2 步·拆开两个潜空间：上一段 视频{h3t_shape(source_video)} 音频{h3t_shape(source_audio)}；下一段 视频{h3t_shape(target_video)} 音频{h3t_shape(target_audio)}；要复制 {overlap_frames} 帧 = {n_tokens} 个时间位置")
        h3t_log(tk, "  第 3 步·检查两边是否对得上：批量=1、时间长度符合 5k+2、高宽为偶数、通道数相同、高宽相同、精度相同、长度够用（任何一项不满足都会在这里报错，报错原因会写在下面一行）")

        for label, video in (("上一段", source_video), ("下一段", target_video)):
            if video.shape[0] != 1:
                raise ValueError(f"{label}：当前节点要求 batch_size=1，实际为 {video.shape[0]}。")
            if video.shape[2] < 2 or (video.shape[2] - 2) % 5 != 0:
                raise ValueError(
                    f"{label}：视频潜空间时间长度 {video.shape[2]} 不符合 H3 的 5×k+2 时间网格。"
                )
            if video.shape[3] % 2 != 0 or video.shape[4] % 2 != 0:
                raise ValueError(
                    f"{label}：视频潜空间高宽必须能按 2×2 Patch 对齐，实际 H/W={tuple(video.shape[3:])}。"
                )

        if source_video.shape[1] != target_video.shape[1]:
            raise ValueError("上一段与下一段的视频潜空间通道数不一致。")
        if source_video.shape[3:] != target_video.shape[3:]:
            raise ValueError(
                "上一段与下一段的分辨率不一致："
                f"上一段={tuple(source_video.shape[3:])}，下一段={tuple(target_video.shape[3:])}。"
                "潜空间不能直接跨分辨率复制。"
            )
        if source_video.dtype != target_video.dtype:
            raise ValueError(
                f"上一段与下一段的视频潜空间精度不一致：{source_video.dtype} / {target_video.dtype}。"
            )
        if source_video.shape[2] < n_tokens:
            raise ValueError(
                f"上一段只有 {source_video.shape[2]} 个视频潜空间时间位置，"
                f"复制 {overlap_frames} 帧需要 {n_tokens} 个。"
            )
        if target_video.shape[2] <= n_tokens:
            raise ValueError(
                f"下一段只有 {target_video.shape[2]} 个视频潜空间时间位置，"
                "无法复制前缀并保留后续生成区域。"
            )

        # ---- 视频：前缀照搬上一段末尾 ----
        source_tail = source_video[:, :, -n_tokens:, :, :].to(device=target_video.device)
        copied_video = torch.cat((source_tail, target_video[:, :, n_tokens:, :, :]), dim=2)
        # 收到的视频遮罩（比如 SAM3 的人物遮罩）整理成标准形状后保留：前缀锁定叠在它上面，其余位置沿用它；没有遮罩就整段重新生成。
        user_mask, _user_audio_mask = _extract_user_video_mask(target_latent, target_video.shape[2:])
        h3t_log(tk, f"  第 4 步·复制视频前缀：取上一段最后 {n_tokens} 个位置，接到下一段第 {n_tokens} 个位置之后；复制后视频 {h3t_shape(copied_video)}；下一段原本带的遮罩={'有 '+str(h3t_shape(user_mask)) if user_mask is not None else '无（整段重新生成）'}")
        if user_mask is not None:
            video_mask = user_mask.to(device=target_video.device, dtype=torch.float32).clone()
            _LOG.info("H3 续接：收到视频遮罩，已整理成 %s 并保留（前缀部分另外锁定）。", tuple(video_mask.shape))
        else:
            video_mask = torch.ones(
                (target_video.shape[0], 1, target_video.shape[2], target_video.shape[3], target_video.shape[4]),
                dtype=torch.float32, device=target_video.device,
            )
        # 复制、以及没接模型的「引导」都整段锁死；接了模型的「引导」由采样时的补丁逐步放开/收紧。
        if dynamic:
            weights = torch.tensor(_prefix_token_weights(n_tokens, seam_min), dtype=torch.float32,
                                   device=target_video.device)
            video_mask[:, :, :n_tokens] = weights.view(1, 1, n_tokens, 1, 1)
        else:
            video_mask[:, :, :n_tokens] = 0.0

        h3t_log(tk, f"  第 5 步·视频遮罩：前 {n_tokens} 个位置={'逐步放开' if dynamic else '全部锁死(0)'}，其余位置沿用{'收到的遮罩' if user_mask is not None else '全 1（重新生成）'}；遮罩形状 {h3t_shape(video_mask)}")
        # ---- 音频 ----
        _ensure_h3_audio_layout_contract()
        audio_tail, audio_steps, audio_index, audio_end_frame, audio_overhang = _audio_tail_and_anchor(
            source_video, source_audio, overlap_frames
        )
        output_latent = target_latent.copy()
        output_conditioning = conditioning
        if audio_mode == AUDIO_COPY:
            end_coord = int(round(_H3_FRAME_RESCALE * audio_end_frame))
            a_len = int(target_audio.shape[-1])
            src_from, dst_from, dst_to = _audio_tail_placement(source_audio, audio_steps, end_coord, a_len)
            new_audio = target_audio.clone()
            new_audio[..., dst_from:dst_to] = source_audio[..., src_from:src_from + (dst_to - dst_from)].to(
                device=target_audio.device, dtype=target_audio.dtype
            )
            values = _audio_mask_values(a_len, dst_from, dst_to, _AUDIO_SOFT_RELEASE_TICKS)
            audio_mask = torch.tensor(values, dtype=torch.float32, device=target_audio.device).view(1, 1, 1, a_len)
            output_latent["samples"] = comfy.nested_tensor.NestedTensor((copied_video, new_audio))
            output_latent["noise_mask"] = comfy.nested_tensor.NestedTensor((video_mask, audio_mask))
            audio_note = f"音频复制：写入 {dst_from}..{dst_to} 共 {dst_to - dst_from} 个位置"
        else:
            audio_keyframe = {"resolved_frame_index": audio_index, "audio_latent": audio_tail}
            output_conditioning = _append_audio_keyframe_to_conditioning(
                conditioning, audio_keyframe, overlap_frames, target_video, task=tk
            )
            output_latent["samples"] = comfy.nested_tensor.NestedTensor((copied_video, target_audio))
            output_latent["noise_mask"] = video_mask
            audio_note = f"音频关键帧：锚点 {audio_index:.4f}"

        h3t_log(tk, f"  第 6 步·音频：{audio_note}")
        out_model = model
        if dynamic:
            out_model = _install_prefix_remask(model, n_tokens, sigmas, tuple(target_video.shape), seam_min)
            if out_model is model:
                # 补丁没装上：引导退回整段锁死，不留下一个静态的半开遮罩
                video_mask[:, :, :n_tokens] = 0.0
                _LOG.warning("引导：模型补丁未安装，退回整段锁死。")
        elif guide:
            _LOG.warning("引导：没有接入「模型」，退回整段锁死。（接入「模型」并把输出的 model 接给采样器才会逐步遮罩。）")

        self._record(ctx, True)
        h3t_log(tk, f"  第 7 步·输出：视频 {h3t_shape(copied_video)}，条件锚点位置={h3t_anchors(output_conditioning)}，遮罩={h3t_brief(output_latent.get('noise_mask'))}")

        _LOG.info(
            "H3 续接：%s，重叠 %d 帧（%d 个位置）%s；%s；%s。",
            mode, overlap_frames, n_tokens,
            f"，重绘幅度 {seam_min:.2f}" if guide else "", audio_note,
            "逐步遮罩已装到模型" if out_model is not model else "整段锁死",
        )
        return (output_latent, output_conditioning, out_model)

    @staticmethod
    def _with_clean_mask(latent, video, keep_audio_mask=False):
        """不接上一段时原样放行；只有收到的视频遮罩形状不标准时，才换成标准形状（其余字段不动）。

        输出只带视频遮罩（普通 5D 张量）：渐进式采样器（SelfLift）不接受嵌套遮罩（会报 isfinite ... NestedTensor）。
        只有「音频衔接=复制」且音频遮罩里确实有锁定的位置时，才保留成 (视频, 音频) 嵌套遮罩。
        """
        video_mask, audio_mask = _extract_user_video_mask(latent, video.shape[2:])
        if video_mask is None:
            return latent
        out = dict(latent)
        video_mask = video_mask.to(device=video.device)
        if keep_audio_mask and not _audio_mask_trivial(audio_mask):
            out["noise_mask"] = comfy.nested_tensor.NestedTensor((video_mask, audio_mask))
            note = "（音频遮罩一并保留）"
        else:
            out["noise_mask"] = video_mask
            note = "" if _audio_mask_trivial(audio_mask) else "（音频衔接=关键帧：音频遮罩没有传给采样器）"
        _LOG.info("H3 续接：收到视频遮罩，已整理成 %s%s。", tuple(video_mask.shape), note)
        return out

    @staticmethod
    def _record(ctx, copied):
        """把「这一段是否真的复制了上一段」记进任务记录，合并时据此决定要不要修剪重叠帧。"""
        if not ctx:
            return
        try:
            def fn(data):
                data.setdefault("segments", {}).setdefault(str(ctx["seg"]), {})["copied"] = bool(copied)
            h3t_reg_update(ctx["task"], fn)
        except Exception:
            pass


class MiniMaxH3LatentSave:
    """Save paired H3 AV tensors using the Motion Context-compatible safetensors format."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "latent": ("LATENT", {"tooltip": "连接 H3 采样器输出的完整 AV 潜空间。"}),
                "保存文件名前缀": (
                    "STRING",
                    {"default": "h3_latent_chain/clip", "tooltip": "相对于 ComfyUI/output；使用独立目录，避免覆盖旧 h3_context 文件。"},
                ),
                "保存编号": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "max": 9999,
                        "step": 1,
                        "control_after_generate": "fixed",
                        "tooltip": "0 = 自动递增（像保存图片那样，永远不会覆盖旧文件）。填 ≥1 则固定用这个编号（会覆盖同名文件）。用「H3 多段输入」跑全部段时一律自动递增，并登记到任务登记表。",
                    },
                ),
            },
            "hidden": {"prompt": "PROMPT", "unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("STRING", "LATENT")
    RETURN_NAMES = ("保存路径", "latent")
    FUNCTION = "save_latent"
    OUTPUT_NODE = True
    CATEGORY = CATEGORY

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return _tm.time_ns()  # 每次都执行并登记

    @h3t_logged("H3 潜空间保存")
    def save_latent(self, **kwargs):
        ctx = h3t_context(kwargs.get("prompt"), kwargs.get("unique_id"))
        task = ctx["task"] if ctx else ""
        try:
            return self._save(ctx, task, **kwargs)
        except Exception as exc:
            h3t_log(task, f"保存潜空间失败：{exc}", _lg.ERROR)
            raise

    def _save(self, ctx, task, **kwargs):
        if _st_save is None:
            raise RuntimeError("当前 Python 环境没有 safetensors，无法保存 H3 潜空间。")
        latent = kwargs["latent"]
        filename_prefix = kwargs["保存文件名前缀"]
        manual = int(kwargs["保存编号"])
        if manual < 0:
            raise ValueError("保存编号不能为负数。")

        video, audio = _split_h3_latent(latent, "待保存的潜空间")
        h3t_log(task, f"  保存：收到视频 {h3t_shape(video)} {getattr(video, 'dtype', '?')}，音频 {h3t_shape(audio)} {getattr(audio, 'dtype', '?')}；前缀={filename_prefix}，保存编号={manual}，{'任务模式（自动递增）' if ctx else '手动'}")
        if video.shape[0] != audio.shape[0]:
            raise ValueError("H3 视频与音频潜空间的 batch_size 不一致。")

        video_cpu = video.detach().to(device="cpu").contiguous()
        audio_cpu = audio.detach().to(device="cpu").contiguous()
        if ctx:
            _check_copy_kept(ctx, task, latent, video_cpu, kwargs.get("prompt"))
        folder, filename, _counter, _subfolder, _prefix = folder_paths.get_save_image_path(
            filename_prefix, folder_paths.get_output_directory()
        )
        os.makedirs(folder, exist_ok=True)
        if ctx or manual == 0:
            clip_index = h3t_next_number(folder, filename, "safetensors")  # 自动递增，不覆盖
        else:
            clip_index = manual
        path = os.path.join(folder, f"{filename}_{clip_index:05d}.safetensors")
        _atomic_save_safetensors(path, {"video": video_cpu, "audio": audio_cpu})
        try:
            h3t_log(task, f"  已写入文件：{path}（{os.path.getsize(path) / 1e6:.1f} MB）")
        except Exception:
            pass
        if ctx:
            self._register(ctx, path)
        else:
            h3t_log("", f"已保存潜空间：{path}")

        output_root = folder_paths.get_output_directory()
        subfolder = os.path.relpath(os.path.dirname(path), output_root)
        subfolder = "" if subfolder == "." else subfolder.replace(os.sep, "/")
        return {
            "ui": {"latents": [{"filename": os.path.basename(path), "subfolder": subfolder, "type": "output"}]},
            "result": (path, latent),
        }

    @staticmethod
    def _register(ctx, path):
        task, seg = ctx["task"], ctx["seg"]
        rel = h3t_rel(path)

        def upd(data):
            data["total"] = ctx["total"]
            segs = data["segments"]
            old = segs.get(str(seg)) or {}
            if old.get("latent") and old["latent"] != rel:
                h3t_rm(old["latent"])  # 同一段重跑：删掉自己上一次的旧文件
            segs[str(seg)] = {**old, "fp": ctx["fps"][seg - 1], "latent": rel, "time": _tm.time()}
            # 这一段重跑了，后面各段依赖它，作废（并删掉它们的潜空间文件）
            for k in [k for k in segs if k.isdigit() and int(k) > seg]:
                h3t_rm(segs[k].get("latent"))
                h3t_rm(segs[k].get("video"))
                del segs[k]

        h3t_reg_update(task, upd)
        if seg == ctx["seg"] and ctx["seg"] == 1:
            h3t_purge_stale(keep_task=task)
        h3t_prune_logs(protect=task)
        h3t_log(task, f"第 {seg}/{ctx['total']} 段：潜空间已保存并登记 → {rel}")


def _check_copy_kept(ctx, task, latent, video, prompt):
    """检查「复制」衔接到底有没有保住：采样完成后，把这一段开头 n 个潜空间时间位置和上一段最后 n 个位置逐个比较，写进日志。
    只记日志，不改任何东西；出任何问题都只写一行警告，不影响保存。"""
    try:
        seg, join = ctx["seg"], ctx.get("join")
        if seg < 2 or ctx.get("hard_start") or join not in ("复制", "引导"):
            h3t_log(task, f"  【复制检查】第 {seg} 段衔接={join or '硬切'}{'（第 1 段）' if seg < 2 else ''}，没有复制，不检查。")
            return
        ov = int(h3t_overlap(prompt, 22, ctx.get("ms_id")))
        n = _tokens_for_h3_frames(ov)
        reg = h3t_reg_load(task) or {}
        entry = (reg.get("segments") or {}).get(str(seg - 1)) or {}
        path = h3t_abs(entry.get("latent")) if entry.get("latent") else None
        if not path or not os.path.isfile(path):
            h3t_log(task, f"  【复制检查】找不到第 {seg - 1} 段保存的潜空间（{entry.get('latent') or '登记表里没有'}），没法比较。", _lg.WARNING)
            return
        prev = _st_load(path)["video"].float()
        cur = video.float()
        if prev.shape[1:2] != cur.shape[1:2] or prev.shape[3:] != cur.shape[3:] or prev.shape[2] < n or cur.shape[2] < n:
            h3t_log(task, f"  【复制检查】两段形状对不上：上一段 {tuple(prev.shape)}，这一段 {tuple(cur.shape)}，没法比较。", _lg.WARNING)
            return
        a = prev[:, :, -n:, :, :]
        b = cur[:, :, :n, :, :]
        scale = float(cur.std()) or 1.0
        per = [(float((a[:, :, i] - b[:, :, i]).abs().mean()) / scale) for i in range(n)]
        rest = float((cur[:, :, n:n + 1] - cur[:, :, n - 1:n]).abs().mean()) / scale if cur.shape[2] > n else float("nan")
        worst = max(per)
        h3t_log(task, f"  【复制检查】第 {seg} 段：重叠 {ov} 帧 = {n} 个时间位置。拿采样完成后这一段开头 {n} 个位置，和第 {seg - 1} 段保存的最后 {n} 个位置比较"
                      f"（数值 = 平均差异 ÷ 这一段潜空间的整体幅度；0 = 完全一样，1 左右 = 毫不相关）：")
        h3t_log(task, "  【复制检查】逐个位置：" + "，".join(f"第{i + 1}个 {v:.3f}" for i, v in enumerate(per)))
        m = latent.get("noise_mask") if isinstance(latent, dict) else None
        if m is not None and hasattr(m, "shape"):
            try:
                if type(m).__name__ == "NestedTensor":
                    m = m.unbind()[0]  # 视频+音频两层遮罩：只看视频那一层
                mm = m.float()
                head = mm[:, :, :n] if mm.ndim == 5 else mm[:n]  # [B,1,T,H,W] 或 [T,1,H,W]
                h3t_log(task, f"  【复制检查】保存时潜空间带的遮罩：形状 {tuple(m.shape)}；开头 {n} 个位置的值 最小 {float(head.min()):.3f} 最大 {float(head.max()):.3f}（0 = 锁定，1 = 重新生成）")
            except Exception as exc:
                h3t_log(task, f"  【复制检查】遮罩读不出来：{exc}")
        else:
            h3t_log(task, "  【复制检查】保存时潜空间上没有遮罩（采样器输出时把遮罩去掉了，这本身不一定有问题）。")
        if worst < 0.02:
            verdict = "复制的内容完整保住了（采样没有改动这几个位置）。如果成片接缝仍然明显，问题在解码或合并，不在复制。"
        elif worst < 0.2:
            verdict = "复制的内容基本保住，有少量改动（可能是采样器按遮罩混合时的细微变化）。"
        else:
            verdict = "复制的内容在采样时被改掉了：这几个位置被重新生成，和上一段结尾不一样，所以接缝看起来像硬切。改动发生在「前缀复制」输出之后、「潜空间保存」之前（中间只有采样器）：锁定遮罩没有被采样器照着执行。复制节点送出去的内容是对的。"
        h3t_log(task, f"  【复制检查】结论：最大差异 {worst:.3f} → {verdict}"
                      + (f"（参考：这一段第 {n} 和第 {n + 1} 个位置之间的正常变化约 {rest:.3f}）" if rest == rest else ""),
                _lg.INFO if worst < 0.2 else _lg.WARNING)
    except Exception as exc:
        h3t_log(task, f"  【复制检查】检查时出错（不影响保存）：{exc}", _lg.WARNING)


LOAD_AUTO = "自动"
LOAD_MANUAL = "手动"


def _is_auto(v):
    """兼容旧版保存在工作流里的取值（「自动（按任务登记表）」「手动指定编号」）。"""
    return not str(v).startswith("手动")


def _read_av_file(path, task=""):
    try:
        _sz = os.path.getsize(path) / 1e6
        h3t_log(task, f"  读取潜空间文件：{path}（{_sz:.1f} MB）")
    except Exception:
        h3t_log(task, f"  读取潜空间文件：{path}（读不到文件大小，文件可能不存在）", _lg.WARNING)
    data = _st_load(path)
    if "video" not in data or "audio" not in data:
        raise ValueError(
            f"文件不是 H3 AV 潜空间文件（缺少 video/audio）：{path}。"
            "普通 ComfyUI Save Latent 文件不能作为 H3 AV 潜空间读取。"
        )
    video = data["video"].contiguous().clone()
    audio = data["audio"].contiguous().clone()
    # Validate before exposing it to the graph.
    _split_h3_latent({"samples": [video, audio]}, "读取的文件")
    h3t_log(task, f"  读取成功：视频 {h3t_shape(video)} {getattr(video, 'dtype', '?')}，音频 {h3t_shape(audio)} {getattr(audio, 'dtype', '?')}")
    return {"samples": comfy.nested_tensor.NestedTensor((video, audio))}


def _load_registered(ctx):
    task, seg = ctx["task"], ctx["seg"]
    if ctx["hard_start"]:
        h3t_log(task, f"第 {seg}/{ctx['total']} 段：{'第一段' if seg == 1 else '硬切'}，不读取上一段潜空间。")
        return (None,)
    prev = seg - 1
    reg = h3t_reg_load(task) or {}
    entry = (reg.get("segments") or {}).get(str(prev))
    h3t_log(task, f"  第 1 步·查登记表：登记表里已登记的段={sorted((reg.get('segments') or {}).keys())}，第 {prev} 段的记录={'有' if entry else '没有'}")
    if not entry or not entry.get("latent"):
        raise FileNotFoundError(
            f"第 {seg} 段需要第 {prev} 段的潜空间，但任务登记表里没有它"
            f"（第 {prev} 段没有跑完，或文件已被清理）。请把「输出第几段」设为 0，"
            f"选择从第 {prev} 段继续，或从第 1 段重新开始。任务编号：{task}。"
        )
    path = h3t_abs(entry["latent"])
    if not path or not os.path.isfile(path):
        raise FileNotFoundError(
            f"登记表记录的第 {prev} 段潜空间文件不存在：{entry['latent']}（可能被手动删除）。"
            f"请从第 {prev} 段重新开始。任务编号：{task}。"
        )
    if entry.get("fp") and entry["fp"] != ctx["fps"][prev - 1]:
        raise ValueError(
            f"第 {prev} 段的内容在保存潜空间之后被改过（提示词或素材不同），不能直接接着它生成。"
            f"请从第 {prev} 段重新开始。任务编号：{task}。"
        )
    h3t_log(task, f"第 {seg}/{ctx['total']} 段：读取第 {prev} 段潜空间 ← {entry['latent']}")
    return (_read_av_file(path, task),)


class H3SeparateAVLatent:
    """Split native H3 NestedTensor samples into separate video/audio LATENTs."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "av_latent": (
                    "LATENT",
                    {"tooltip": "连接 H3 原生联合音视频潜空间；支持本文件前缀复制节点输出。"},
                )
            }
        }

    RETURN_TYPES = ("LATENT", "LATENT")
    RETURN_NAMES = ("video_latent", "audio_latent")
    FUNCTION = "split"
    CATEGORY = CATEGORY
    DESCRIPTION = (
        "按 MiniMax H3 原生 NestedTensor 结构分离视频和音频；"
        "单独的视频 noise_mask 只传给视频，不伪造音频掩码。"
    )

    @h3t_logged("H3 音视频潜空间分离")
    def split(self, av_latent):
        if not isinstance(av_latent, dict) or "samples" not in av_latent:
            raise ValueError("输入不是有效的 ComfyUI LATENT，缺少 samples。")

        samples = av_latent["samples"]
        if hasattr(samples, "unbind"):
            try:
                streams = list(samples.unbind())
            except Exception as exc:
                raise ValueError(f"无法读取 H3 NestedTensor 的视频/音频分量：{exc}") from exc
        elif isinstance(samples, (tuple, list)):
            streams = list(samples)
        else:
            raise ValueError(
                "samples 不是 H3 NestedTensor 或 (video, audio) 列表/元组；"
                f"实际类型：{type(samples).__name__}。"
            )

        if len(streams) != 2:
            raise ValueError(f"H3 联合潜空间应包含视频和音频两路，实际为 {len(streams)} 路。")

        video, audio = streams
        if not torch.is_tensor(video) or video.ndim != 5 or video.shape[1] != 24:
            shape = tuple(video.shape) if torch.is_tensor(video) else type(video).__name__
            raise ValueError(f"H3 视频潜空间应为 [B,24,T,H,W]，实际为 {shape}。")
        if not torch.is_tensor(audio) or audio.ndim != 4 or audio.shape[1] != 32 or audio.shape[2] != 2:
            shape = tuple(audio.shape) if torch.is_tensor(audio) else type(audio).__name__
            raise ValueError(f"H3 音频潜空间应为 [B,32,2,T]，实际为 {shape}。")

        video_latent = dict(av_latent)
        audio_latent = dict(av_latent)
        video_latent["samples"] = video
        audio_latent["samples"] = audio

        # PrefixCopy emits a regular 5D video mask on the joint AV latent.
        # Never unbind it on the batch axis or duplicate it onto audio.
        mask = av_latent.get("noise_mask")
        video_mask = None
        audio_mask = None

        if mask is not None:
            if isinstance(mask, (tuple, list)):
                if len(mask) == 2:
                    video_mask, audio_mask = mask
                elif len(mask) == 1:
                    only = mask[0]
                    rank = getattr(only, "ndim", None)
                    if rank == 5:
                        video_mask = only
                    elif rank == 4:
                        audio_mask = only
                    else:
                        raise ValueError("单项 noise_mask 无法根据维度判断属于视频还是音频。")
                else:
                    raise ValueError(f"H3 noise_mask 应为两路掩码，实际包含 {len(mask)} 项。")
            elif bool(getattr(mask, "is_nested", False)) and hasattr(mask, "unbind"):
                masks = list(mask.unbind())
                if len(masks) != 2:
                    raise ValueError(f"嵌套 noise_mask 应包含两路，实际为 {len(masks)} 路。")
                video_mask, audio_mask = masks
            else:
                rank = getattr(mask, "ndim", None)
                if rank == 5:
                    video_mask = mask
                elif rank == 4:
                    audio_mask = mask
                else:
                    raise ValueError(
                        "noise_mask 不是可识别的 H3 两路掩码，也不是单路 5D 视频/4D 音频掩码；"
                        f"实际维度：{rank}。为避免误传掩码，节点已停止。"
                    )

        video_latent.pop("noise_mask", None)
        audio_latent.pop("noise_mask", None)
        if video_mask is not None:
            video_latent["noise_mask"] = video_mask
        if audio_mask is not None:
            audio_latent["noise_mask"] = audio_mask

        return (video_latent, audio_latent)


NODE_CLASS_MAPPINGS = {
    "MiniMaxH3LatentPrefixCopy": MiniMaxH3LatentPrefixCopy,
    "MiniMaxH3LatentSave": MiniMaxH3LatentSave,
    "H3SeparateAVLatent": H3SeparateAVLatent,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "MiniMaxH3LatentPrefixCopy": "H3 潜空间前缀复制",
    "MiniMaxH3LatentSave": "H3 潜空间保存",
    "H3SeparateAVLatent": "H3 音视频潜空间分离",
}
