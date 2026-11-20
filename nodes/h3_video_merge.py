# -*- coding: utf-8 -*-
"""H3 视频合并 —— 配合「H3 多段输入」逐段运行使用的单文件节点（适配 ComfyUI-Prompt 自动加载框架）。

每运行一段：把这一段（已修剪重叠帧的）画面和声音编码成 output/<保存名称>/seg_001.mp4、seg_002.mp4……，
然后把第 1 段到当前段的文件按顺序拼成 output/<保存名称>/merged.mp4。
最后一段跑完时，merged.mp4 就是完整视频。「当前段号」跑全部段时由「H3 多段输入」自动设置。

v2：成片和官方保存视频一样自动编号（不覆盖）：保存名称填 video/MiniMax H3 → output/video/MiniMax H3_00001.mp4；
各段片段固定放在 <成片所在文件夹>/片段/<任务编号>/seg_001.mp4。用「H3 多段输入」跑全部段时，只在最后一段才合并，
并且只合并任务登记表里这个任务自己的各段（少一段或文件被动过就报错，绝不去合并文件夹里别的视频）。
"""

import logging
import os
import time
import shutil
import subprocess
import tempfile
import wave

import folder_paths

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

_LOG = logging.getLogger("h3_video_merge")
CATEGORY = "自定义/H3视频"
_AUDIO_SR = 48000

# 编码格式 -> (扩展名, 视频编码器, 音频编码器)
FORMATS = {
    "mp4 (H.264)": ("mp4", "libx264", "aac"),
    "mp4 (H.265)": ("mp4", "libx265", "aac"),
    "webm (VP9)": ("webm", "libvpx-vp9", "libopus"),
    "mkv (H.264)": ("mkv", "libx264", "aac"),
    "mov (H.264)": ("mov", "libx264", "aac"),
    "mkv (H.265)": ("mkv", "libx265", "aac"),
    "mov (ProRes 422)": ("mov", "prores_ks", "pcm_s16le"),  # 剪辑软件用，文件很大；像素格式固定 yuv422p10le，CRF/预设不起作用
}
PIX_FMTS = ["yuv420p", "yuv420p10le", "yuv444p"]
PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow"]
AUDIO_BITRATES = ["128k", "192k", "256k", "320k"]
_VP9_CPU_USED = {"ultrafast": 5, "superfast": 5, "veryfast": 4, "faster": 3, "fast": 2, "medium": 1, "slow": 0}


def _ffmpeg_bin():
    try:
        from imageio_ffmpeg import get_ffmpeg_exe

        return get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


def _split_prefix(name):
    """和官方保存视频一样：video/MiniMax H3 → (output/video, "MiniMax H3")。"""
    root = os.path.realpath(folder_paths.get_output_directory())
    rel = (name or "h3_video").strip().strip('"').strip("'").replace("\\", "/").strip("/")
    if not rel:
        rel = "h3_video"
    sub, base = os.path.split(rel)
    base = "".join(ch for ch in base if ch not in '<>:"|?*').strip(". ") or "h3_video"
    full = os.path.realpath(os.path.join(root, sub)) if sub else root
    if full != root and not full.startswith(root + os.sep):
        raise ValueError("保存名称必须在 ComfyUI 的 output 目录内。")
    os.makedirs(full, exist_ok=True)
    return full, base


def _seg_dir(folder, task):
    d = os.path.join(folder, "片段", task or "手动")
    os.makedirs(d, exist_ok=True)
    return d


def _seg_path(folder, idx, ext="mp4"):
    return os.path.join(folder, f"seg_{int(idx):03d}.{ext}")


# ---------------- 接缝检查（只写日志，不改成片） ----------------
_SEAM_W = 96  # 比较用的缩小画面宽度（像素），够看出画面是否连续


def _seam_small(frames):
    """[N,H,W,3] 张量 → 缩小后的 numpy float32（隔几个像素取一个）。"""
    import numpy as _np
    w = int(frames.shape[2])
    st = max(1, w // _SEAM_W)
    return _np.asarray(frames[:, ::st, ::st, :3].float().cpu().numpy(), dtype=_np.float32)


# 接缝记录：每个任务一个临时文件 h3_tasks/<任务>.seam.npz（只存最近一段结尾的缩小画面，几 MB），任务合并成功时随登记表一起删掉


def _seam_check(task, seg_dir, idx, images, hd, fps):
    """合并前检查第 idx 段和上一段的接缝。images 是这一段还没裁的完整画面，hd 是开头和上一段重合、将被裁掉的帧数。
    1) 重合区：这一段开头 hd 帧 和 上一段最后 hd 帧 逐帧比较——复制保住了，两边应该几乎一样；
    2) 接缝：上一段最后一帧（成片里接缝前一帧）和这一段裁剪后的第一帧比较，再和这一段内部正常的相邻帧变化比；
       比正常变化大很多，成片里就会看到跳变（像硬切）。"""
    try:
        import numpy as _np
        if idx < 2:
            return
        pf = h3t_seam_path(task)
        z = _np.load(pf) if os.path.isfile(pf) else None
        if z is None or int(z["idx"]) != idx - 1:
            h3t_log(task, f"  【接缝检查】第 {idx} 段：没有第 {idx - 1} 段的结尾画面记录（不是依次运行的），没法比较。")
            return
        ptail, pend = z["tail"], int(z["end"])
        n = int(images.shape[0])
        cur = _seam_small(images[: min(n, max(hd, 0) + 6)])
        if ptail.shape[1:] != cur.shape[1:]:
            h3t_log(task, f"  【接缝检查】第 {idx} 段画面尺寸和上一段不同（{ptail.shape[1:3]} / {cur.shape[1:3]}），没法比较。", _lg.WARNING)
            return
        d = lambda a, b: float(_np.abs(a - b).mean())
        inner = [d(cur[i], cur[i + 1]) for i in range(max(hd, 0), cur.shape[0] - 1)]
        normal = float(_np.median(inner)) if inner else float("nan")
        t0 = pend / float(fps)
        h3t_log(task, f"  【接缝检查】第 {idx - 1}→{idx} 段：接缝在成片第 {pend} 帧（约 {int(t0 // 60)}分{t0 % 60:05.2f}秒）。"
                      f"数值是两帧画面的平均像素差（0~1，0 = 完全一样）；这一段内部相邻两帧的正常变化约 {normal:.4f}。")
        if hd > 0:
            k = min(hd, ptail.shape[0], n)
            ov = [d(ptail[-k + i], cur[i]) for i in range(k)]
            h3t_log(task, f"  【接缝检查】重合区（这一段开头 {k} 帧 对比 上一段最后 {k} 帧，合并时这一段的这 {k} 帧会被裁掉）：" + "，".join(f"{v:.3f}" for v in ov))
            ovm = max(ov)
            if ovm < max(0.02, 3 * normal):
                h3t_log(task, f"  【接缝检查】→ 重合区两边基本一样（最大 {ovm:.3f}）：复制的内容在成片画面里是保住的。")
            else:
                h3t_log(task, f"  【接缝检查】→ 重合区两边差别很大（最大 {ovm:.3f}）：这一段开头并不是上一段的结尾画面，复制没有体现在画面上。", _lg.WARNING)
        else:
            h3t_log(task, f"  【接缝检查】这一段没有重合帧（硬切或没复制），只看接缝。")
        if n > max(hd, 0):
            jump = d(ptail[-1], cur[max(hd, 0)])
            ratio = jump / normal if normal and normal == normal and normal > 0 else float("inf")
            msg = (f"  【接缝检查】接缝：上一段最后一帧 → 这一段裁剪后第一帧 差 {jump:.4f}，是正常相邻帧变化的 {ratio:.1f} 倍")
            if ratio <= 3:
                h3t_log(task, msg + " → 接缝连续，看不出跳变。")
            else:
                h3t_log(task, msg + " → 接缝有明显跳变（看起来像硬切）。", _lg.WARNING)
    except Exception as exc:
        h3t_log(task, f"  【接缝检查】检查时出错（不影响合并）：{exc}", _lg.WARNING)


def _seam_save(task, seg_dir, idx, images, fps):
    """记下这一段（已裁好、要进成片的画面）最后几十帧的缩小画面和它在成片里结束的帧号，写到 h3_tasks 里的临时文件给下一段的接缝检查用，任务合并成功时随登记表一起删掉。"""
    try:
        import numpy as _np
        pf = h3t_seam_path(task)
        start = 0
        if idx > 1 and os.path.isfile(pf):
            z = _np.load(pf)
            if int(z["idx"]) == idx - 1:
                start = int(z["end"])
        n = int(images.shape[0])
        _np.savez(pf, idx=int(idx), tail=_seam_small(images[max(0, n - 40):]), end=start + n)  # 同一个文件每段覆盖一次
    except Exception as exc:
        h3t_log(task, f"  【接缝检查】记录第 {idx} 段结尾画面时出错（不影响合并）：{exc}", _lg.WARNING)



def _clean_name(name):
    base = os.path.basename(str(name or "merged").strip().replace("\\", "/"))
    base = os.path.splitext(base)[0] if base.lower().endswith((".mp4", ".webm", ".mkv")) else base
    base = "".join(ch for ch in base if ch not in '<>:"|?*').strip(". ")
    return base or "merged"


def _codec_args(fmt, pix, crf, preset, abr):
    """视频 + 音频编码参数。"""
    ext, vcodec, acodec = FORMATS[fmt]
    crf = int(crf)
    if vcodec == "prores_ks":
        return ["-c:v", vcodec, "-profile:v", "3", "-pix_fmt", "yuv422p10le", "-c:a", acodec, "-ar", str(_AUDIO_SR), "-ac", "2"]
    if vcodec == "libvpx-vp9":
        v = ["-c:v", vcodec, "-crf", str(max(0, min(63, crf))), "-b:v", "0", "-pix_fmt", pix,
             "-row-mt", "1", "-deadline", "good", "-cpu-used", str(_VP9_CPU_USED.get(preset, 4))]
    else:
        v = ["-c:v", vcodec, "-preset", preset, "-crf", str(max(0, min(51, crf))), "-pix_fmt", pix]
        if vcodec == "libx265":
            v += ["-x265-params", "log-level=error"]
            if ext == "mp4":
                v += ["-tag:v", "hvc1"]
    a = ["-c:a", acodec, "-b:a", abr, "-ar", str(_AUDIO_SR), "-ac", "2"]
    return v + a


def _escape_meta(text):
    out = []
    for ch in str(text):
        if ch in "=;#\\":
            out.append("\\" + ch)
        elif ch == "\n":
            out.append("\\\n")
        else:
            out.append(ch)
    return "".join(out)


def _write_ffmetadata(path, items):
    with open(path, "w", encoding="utf-8") as f:
        f.write(";FFMETADATA1\n")
        for k, v in items.items():
            f.write(f"{k}={_escape_meta(v)}\n")


def _frames_to_uint8(images):
    import numpy as np

    arr = images.detach().cpu().numpy() if hasattr(images, "detach") else images.cpu().numpy()
    if arr.ndim != 4 or arr.shape[-1] < 3:
        raise ValueError(f"图像形状应为 [帧数,高,宽,3]，实际为 {tuple(arr.shape)}。")
    arr = (np.clip(arr[..., :3], 0.0, 1.0) * 255.0 + 0.5).astype("uint8")
    return np.ascontiguousarray(arr)


def _write_wav(audio, path):
    """AUDIO 字典 -> 16 位 wav。返回采样率。"""
    import numpy as np

    wf = audio["waveform"]
    wf = wf.detach().cpu().numpy() if hasattr(wf, "detach") else wf.cpu().numpy()
    if wf.ndim == 3:
        wf = wf[0]  # [C, T]
    sr = int(audio["sample_rate"])
    pcm = (np.clip(wf.T, -1.0, 1.0) * 32767.0).astype("<i2")  # [T, C]
    with wave.open(path, "wb") as w:
        w.setnchannels(int(pcm.shape[1]))
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return sr


def _encode_segment(images, audio, fps, out_path, fmt="mp4 (H.264)", pix="yuv420p", crf=18, preset="veryfast", abr="192k"):
    ffmpeg = _ffmpeg_bin()
    if not ffmpeg:
        raise RuntimeError("找不到 ffmpeg，无法编码视频。")
    frames = _frames_to_uint8(images)
    n, h, w, _c = frames.shape
    if n < 1:
        raise ValueError("这一段没有画面。")
    tmp_dir = tempfile.mkdtemp(prefix="h3merge_")
    try:
        cmd = [ffmpeg, "-y", "-v", "error", "-nostdin", "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-s", f"{w}x{h}", "-r", str(fps), "-i", "-"]
        if audio is not None:
            wav = os.path.join(tmp_dir, "a.wav")
            _write_wav(audio, wav)
            cmd += ["-i", wav]
        else:  # 没有声音：补静音，保证每一段都有音轨，拼接才不会出问题
            cmd += ["-f", "lavfi", "-i", f"anullsrc=r={_AUDIO_SR}:cl=stereo"]
        part = out_path + ".part." + FORMATS[fmt][0]
        cmd += ["-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2"] + _codec_args(fmt, pix, crf, preset, abr)
        cmd += ["-af", "apad", "-shortest", part]
        err_path = os.path.join(tmp_dir, "err.txt")
        with open(err_path, "wb") as errf:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=errf)
            try:
                for k in range(n):  # 逐帧写入，避免整段复制一份占内存
                    proc.stdin.write(frames[k].tobytes())
                proc.stdin.close()
            except BrokenPipeError:
                pass
            code = proc.wait()
        if code != 0:
            with open(err_path, "rb") as f:
                raise RuntimeError("ffmpeg 编码失败：" + f.read().decode("utf-8", "ignore")[-400:])
        os.replace(part, out_path)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        try:
            if os.path.exists(part):
                os.remove(part)
        except OSError:
            pass


def _contiguous_segments(folder, upto, ext="mp4"):
    """第 1 段到第 upto 段里，从头开始连续存在的文件（遇到缺口就停）。"""
    found = []
    for i in range(1, int(upto) + 1):
        p = _seg_path(folder, i, ext)
        if not os.path.isfile(p):
            break
        found.append(p)
    return found


def _concat(files, out_path, ext, codec_args, metadata=None):
    """把各段按顺序拼成 out_path（默认直接拼接不重新编码；失败则按 codec_args 重新编码）。可选写入元数据。"""
    ffmpeg = _ffmpeg_bin()
    part = os.path.splitext(out_path)[0] + ".part." + ext
    list_path = out_path + ".list.txt"
    meta_path = out_path + ".meta.txt"
    with open(list_path, "w", encoding="utf-8") as f:
        for p in files:
            f.write("file '" + p.replace("'", "'\\''") + "'\n")
    try:
        base = [ffmpeg, "-y", "-v", "error", "-nostdin", "-f", "concat", "-safe", "0", "-i", list_path]
        tail = []
        if metadata:
            _write_ffmetadata(meta_path, metadata)
            base += ["-i", meta_path]
            tail = ["-map", "0", "-map_metadata", "1"]
        flags = ["-movflags", "+faststart+use_metadata_tags"] if ext == "mp4" else []
        res = subprocess.run(base + tail + ["-c", "copy"] + flags + [part], capture_output=True)
        if res.returncode != 0:  # 直接拼接失败就重新编码一次
            _LOG.warning("H3 视频合并：直接拼接失败，改为重新编码。%s", res.stderr.decode("utf-8", "ignore")[-200:])
            res = subprocess.run(base + tail + codec_args + flags + [part], capture_output=True)
            if res.returncode != 0:
                raise RuntimeError("ffmpeg 合并失败：" + res.stderr.decode("utf-8", "ignore")[-400:])
        os.replace(part, out_path)
    finally:
        for p in (list_path, meta_path, part):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass


def _ms_segment(prompt):
    """工作流里只有一个「H3 多段输入」时，读它的「输出第几段」（≥1）；否则返回 0。"""
    try:
        nodes = [v for v in prompt.values() if isinstance(v, dict) and v.get("class_type") == H3T_MS_TYPE]
        if len(nodes) == 1:
            n = (nodes[0].get("inputs") or {}).get("输出第几段")
            if isinstance(n, int) and not isinstance(n, bool) and n >= 1:
                return n
    except Exception:
        pass
    return 0


def _chain_prefix(prompt, uid):
    """顺着本节点（图像/音频口）的连线往上找，最近的一个「H3 潜空间前缀复制」节点的输入；找不到返回 None。
    只认接在这条链上的那一个，工作流里别处的前缀复制节点不算。"""
    try:
        if not isinstance(prompt, dict):
            return None
        start = str(uid) if uid is not None and str(uid) in prompt else None
        if start is None:
            ids = [k for k, v in prompt.items() if isinstance(v, dict) and v.get("class_type") == "H3VideoMerge"]
            if len(ids) != 1:
                return None
            start = ids[0]
        seen, queue = {start}, [start]
        while queue:
            cur = queue.pop(0)
            for val in ((prompt.get(cur) or {}).get("inputs") or {}).values():
                if isinstance(val, list) and len(val) == 2 and str(val[0]) in prompt and str(val[0]) not in seen:
                    nid = str(val[0])
                    seen.add(nid)
                    nd = prompt[nid]
                    if isinstance(nd, dict) and nd.get("class_type") == "MiniMaxH3LatentPrefixCopy":
                        return nd.get("inputs") or {}
                    queue.append(nid)
    except Exception:
        pass
    return None


def _manual_trim(prompt, uid, manual_on, own_join, idx):
    """手动运行（没有任务记录）时要裁掉开头多少帧。两层判断：
    ① 接在这条链上的「前缀复制」接了「下一段初始潜空间」（= 完整流程里真的读了上一段并衔接）：只看它——
       衔接方式是复制/引导/图片引导、读取方式=手动、读取编号≥1 就裁「重叠帧数」；
    ② 没有这个信息（读取补救流程：只读取、解码、合并）：「手动」开着时看本节点自己的「衔接方式」——复制/引导，并且「当前段号」≥2 才裁。
       读取编号只是文件名，不代表这个视频的第几段，所以不参与判断。"""
    try:
        pc = _chain_prefix(prompt, uid)
        ov = 22
        if pc is not None:
            n = pc.get("重叠帧数")
            if isinstance(n, int) and not isinstance(n, bool) and n > 0:
                ov = n
        else:
            ov = h3t_overlap(prompt, 22)
        if pc is not None and isinstance(pc.get("下一段初始潜空间"), list):
            num = pc.get("读取编号")
            ok = (pc.get("衔接方式") in ("复制", "引导", "图片引导") and pc.get("读取方式") == "手动"
                  and isinstance(num, int) and not isinstance(num, bool) and num >= 1)
            return ov if ok else 0
        if manual_on and own_join in ("复制", "引导") and int(idx) >= 2:
            return ov
    except Exception:
        pass
    return 0


class H3VideoMerge:
    """每段存成文件，并把第 1 段到当前段拼成一个完整视频，节点上直接预览。"""

    @classmethod
    def INPUT_TYPES(cls):
        # 控件的显示顺序 = 这里的顺序；工作流存档里控件值是按这个顺序存的，调整顺序要同步写前端的旧工作流迁移。
        return {
            "required": {},
            "optional": {
                "图像": ("IMAGE", {"tooltip": "这一段解码之后的画面。\n不接：只合并——把这个任务已经保存的各段片段直接拼成成片（不用再采样、解码）。"}),
                "音频": ("AUDIO", {"tooltip": "这一段修剪后的音频。不接则这一段是静音。"}),
                "帧率": ("INT", {"default": 24, "min": 1, "max": 120, "tooltip": "H3 是 24。"}),
                "编码格式": (list(FORMATS), {"default": "mp4 (H.264)",
                                              "tooltip": "所有段必须用同一种格式；中途换格式要从第 1 段重跑。H.265 在部分浏览器里无法预览；mov (ProRes 422) 是剪辑用的，文件很大，浏览器里也无法预览。"}),
                "保存名称": ("STRING", {"default": "video/MiniMax H3",
                                        "tooltip": "和官方保存视频一样的「文件名前缀」：video/MiniMax H3 → 成片放 output/video，文件名 MiniMax H3_00001.mp4（自动编号，不覆盖）。各段片段放在成片文件夹下的「片段/任务编号」里。"}),
                "音频来源": (["解码音频", "原视频音频"], {"default": "解码音频",
                                                      "tooltip": "解码音频：用「音频」口接进来的每段音频。\n原视频音频（视频编辑用）：忽略「音频」口，合并成片时把原视频整条音轨一次性贴上去，没有逐段拼接的接缝。时间线里「音频」选了「保留原音频」时不用选这个，会自动贴。"}),
                "手动": ("BOOLEAN", {"default": False,
                                      "tooltip": "关：每一段都会自动修剪并保存成片段，但不合并。跑任务（H3 多段输入的「输出第几段」=0）跑完会自动合并；不接图像直接运行，合并最新的任务或手动保存的片段。\n开：手动一段一段运行，填下面的当前段号和总段数，跑到最后一段自动合并。"}),
                "当前段号": ("INT", {"default": 1, "min": 1, "max": 9999, "control_after_generate": "fixed",
                                      "tooltip": "（手动）这一段是第几段。右边的下拉框选「递增」，每运行一次自动加 1（显示的是下一次要跑的那一段）；跑完最后一段后要自己改回 1。"}),
                "总段数": ("INT", {"default": 0, "min": 0, "max": 9999,
                                    "tooltip": "（手动）共几段。大于 0 时，只有「当前段号」跑到最后一段才合并，前面的段只单独保存；0 表示每一段都合并到「<名称>_merged」（会被覆盖）。"}),
                "衔接方式": (["复制", "引导", "硬切"], {"default": "复制",
                                                    "tooltip": "（手动）这一段和上一段的衔接方式，用来决定要不要裁掉开头和上一段重复的帧：复制/引导会裁（当前段号 ≥2 时），硬切不裁。\n只在读取补救流程（只读取、解码、合并，前缀复制没接「下一段初始潜空间」）里起作用；完整流程里节点会自己读前缀复制的设置，不看这里。"}),
                "CRF": ("INT", {"default": 18, "min": 0, "max": 63,
                                "tooltip": "画质，越小越好、文件越大。H.264/H.265 常用 15~23；VP9 常用 25~35。H.264/H.265 最大 51。ProRes 不起作用。", "advanced": True}),
                "编码预设": (PRESETS, {"default": "veryfast", "tooltip": "编码速度与压缩效率的取舍。较慢通常压缩效率更高，但不保证画质或体积完全一致。不同编码器的档位映射不同；ProRes 不使用此设置。", "advanced": True}),
                "像素格式": (PIX_FMTS, {"default": "yuv420p",
                                         "tooltip": "yuv420p 兼容性最好（浏览器、手机都能播）；yuv420p10le 是 10 位；yuv444p 保留完整色彩，但很多播放器不支持。ProRes 固定 yuv422p10le。", "advanced": True}),
                "音频码率": (AUDIO_BITRATES, {"default": "192k", "advanced": True}),
                "保存元数据": ("BOOLEAN", {"default": True, "tooltip": "把工作流和提示词写进合并后的视频文件里（和官方保存视频一样），之后可以拖回 ComfyUI 还原。", "advanced": True}),
                "合并后删除潜空间": ("BOOLEAN", {"default": True, "tooltip": "跑全部段并成功合并后，自动删除这个任务保存的各段潜空间文件（只删任务登记表里列出的）。删掉后这个任务就不能再接着往后延长。", "advanced": True}),
                "合并后删除各段片段": ("BOOLEAN", {"default": False, "tooltip": "成功合并后，同时删除各段单独的片段文件，只留成片。", "advanced": True}),
            },
            "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO", "unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("合并文件路径",)
    FUNCTION = "merge"
    CATEGORY = CATEGORY
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")  # 每次运行都执行

    @h3t_logged("H3 视频合并")
    def merge(self, **kwargs):
        ctx = h3t_context(kwargs.get("prompt"), kwargs.get("unique_id"))
        task = ctx["task"] if ctx else ""
        try:
            return self._merge(ctx, task, **kwargs)
        except Exception as exc:
            h3t_log(task, f"视频合并失败：{exc}", logging.ERROR)
            raise

    def _merge(self, ctx, task, **kwargs):
        images = kwargs.get("图像")
        audio = kwargs.get("音频")
        fps = int(kwargs.get("帧率", 24))
        if images is None:
            return self._merge_existing(ctx, task, **kwargs)
        manual_on = bool(kwargs.get("手动", False))
        if ctx:
            idx, total = ctx["seg"], ctx["total"]
        elif manual_on:
            idx, total = int(kwargs.get("当前段号", 1)), int(kwargs.get("总段数", 0) or 0)
        else:  # 没有任务、手动也没开：只保存这一段（段号取「H3 多段输入」的「输出第几段」，没有就取「当前段号」），不合并
            idx, total = _ms_segment(kwargs.get("prompt")) or int(kwargs.get("当前段号", 1)), 0
        use_src_audio = self._use_src_audio(ctx, kwargs, task)
        ve = ((h3t_reg_load(task) or {}).get("ve") or {}) if (ctx and task) else {}
        if use_src_audio:
            audio = None  # 每段先不带声音，成片再贴原视频音轨
        # 视频编辑（段间衔接=引导/复制）：第 2 段起，开头 head 帧和上一段结尾重合，合并前自动裁掉。
        # 按帧数判断：输入的帧数 = head + 交付（+ 最后一段的尾帧）才裁；已经被「H3 续接重叠帧修剪」裁过的（帧数 = 交付）不再重复裁。
        try:
            pe = (ve.get("plan") or [])[idx - 1] if ve.get("plan") and idx <= len(ve["plan"]) else None
        except Exception:
            pe = None
        _sd = _seg_dir(_split_prefix(kwargs.get("保存名称", "video/MiniMax H3"))[0], task) if ctx else None
        _checked = False
        if pe and int(pe.get("head", 0)) > 0:
            hd = int(pe["head"])
            n_in = int(images.shape[0])
            full = hd + int(pe["delivered"])
            if n_in == full:
                if _sd:
                    _seam_check(task, _sd, idx, images, hd, fps)
                    _checked = True
                images = images[hd:]
                if audio is not None and isinstance(audio, dict) and "waveform" in audio:
                    skip = int(round(hd / float(fps) * audio["sample_rate"]))
                    audio = {**audio, "waveform": audio["waveform"][..., skip:]}
                h3t_log(task, f"第 {idx} 段：裁掉开头和上一段重合的 {hd} 帧（{n_in} → {n_in - hd} 帧）")
            elif n_in == int(pe["delivered"]):
                h3t_log(task, f"第 {idx} 段：重叠的 {hd} 帧已经在前面裁掉了（输入 {n_in} 帧），不再重复裁。")
            else:
                raise ValueError(f"[H3 视频合并] 第 {idx} 段输入 {n_in} 帧，既不是采样的 {full} 帧（含重叠 {hd} 帧），也不是裁掉重叠后的 {int(pe['delivered'])} 帧。请检查接进来的是不是这一段完整的解码画面。")
        # 视频编辑：最后一段为凑合法长度多生成的尾帧，在这里裁掉，成片长度 = 原视频长度
        try:
            cut = int((ve.get("plan") or [])[idx - 1].get("tail_cut", 0)) if ve.get("plan") and idx <= len(ve["plan"]) else 0
        except Exception:
            cut = 0
        if cut > 0:
            n_in = int(images.shape[0])
            if cut >= n_in:
                raise ValueError(f"[H3 视频合并] 要裁掉的尾帧（{cut}）不少于这一段的帧数（{n_in}），请检查输入的图像是不是这一段完整的画面。")
            images = images[: n_in - cut]
            if audio is not None and isinstance(audio, dict) and "waveform" in audio:
                keep = int(round(images.shape[0] / float(fps) * audio["sample_rate"]))
                audio = {**audio, "waveform": audio["waveform"][..., :keep]}
            h3t_log(task, f"第 {idx} 段是最后一段：裁掉多出来的 {cut} 帧（{n_in} → {n_in - cut} 帧），成片长度和原视频一致")
        # 其他模式（参考生视频 / 首尾帧视频）：第 2 段起接上一段的，开头和上一段重合的帧在这里裁掉；第 1 段和「硬切」的段不裁。
        if not ve.get("plan"):
            if ctx:
                trim = 0 if ctx["hard_start"] else h3t_overlap(kwargs.get("prompt"), 22, ctx.get("ms_id"))
                if trim > 0:
                    ent = ((h3t_reg_load(task) or {}).get("segments") or {}).get(str(idx)) or {}
                    if not ent.get("copied"):
                        # 这一段实际没有复制上一段（读取节点没读到 / 前缀复制没执行），开头没有重叠，不能裁
                        h3t_log(task, f"第 {idx} 段：任务记录显示这一段没有复制上一段，开头没有重叠帧，不裁剪。"
                                      "（若本该衔接，请检查读取节点和前缀复制节点的日志。）", _lg.WARNING)
                        trim = 0
            else:
                trim = _manual_trim(kwargs.get("prompt"), kwargs.get("unique_id"), manual_on, kwargs.get("衔接方式", "复制"), idx)
            if trim > 0:
                n_in = int(images.shape[0])
                if trim >= n_in:
                    raise ValueError(f"[H3 视频合并] 要裁掉开头 {trim} 帧，但这一段只有 {n_in} 帧。")
                if _sd:
                    _seam_check(task, _sd, idx, images, trim, fps)
                    _checked = True
                images = images[trim:]
                if audio is not None and isinstance(audio, dict) and "waveform" in audio:
                    skip = int(round(trim / float(fps) * audio["sample_rate"]))
                    if skip >= int(audio["waveform"].shape[-1]):
                        raise ValueError("[H3 视频合并] 要裁掉的音频比这一段的音频还长，请检查帧率或音频输入。")
                    audio = {**audio, "waveform": audio["waveform"][..., skip:]}
                h3t_log(task, f"第 {idx} 段：裁掉开头和上一段重合的 {trim} 帧（{n_in} → {n_in - trim} 帧）")
        # 音频长度对齐到画面时长：多了截短、少了补静音，避免每段累计约 8ms 的音画漂移
        if audio is not None and isinstance(audio, dict) and "waveform" in audio:
            wf = audio["waveform"]
            want = int(round(int(images.shape[0]) / float(fps) * int(audio["sample_rate"])))
            have = int(wf.shape[-1])
            if have > want:
                wf = wf[..., :want]
            elif have < want:
                import torch as _t
                wf = _t.nn.functional.pad(wf, (0, want - have))
            audio = {**audio, "waveform": wf}
        fmt = kwargs.get("编码格式", "mp4 (H.264)")
        if fmt not in FORMATS:
            raise ValueError(f"编码格式「{fmt}」无效。")
        pix = kwargs.get("像素格式", "yuv420p")
        crf = int(kwargs.get("CRF", 18))
        preset = kwargs.get("编码预设", "veryfast")
        abr = kwargs.get("音频码率", "192k")
        ext = FORMATS[fmt][0]
        folder, base = _split_prefix(kwargs.get("保存名称", "video/MiniMax H3"))
        seg_dir = _seg_dir(folder, task)
        root = os.path.realpath(folder_paths.get_output_directory())

        def ui_for(path):
            sub = os.path.relpath(os.path.dirname(path), root).replace(os.sep, "/")
            return {"images": [{"filename": os.path.basename(path), "subfolder": "" if sub == "." else sub, "type": "output"}],
                    "animated": (True,)}

        seg_file = _seg_path(seg_dir, idx, ext)
        if ctx:
            if not _checked and idx > 1:
                _seam_check(task, seg_dir, idx, images, 0, fps)  # 这一段没有重合帧：只看接缝
            _seam_save(task, seg_dir, idx, images, fps)
        _encode_segment(images, audio, fps, seg_file, fmt, pix, crf, preset, abr)
        if ctx:
            rel_seg = h3t_rel(seg_file)

            def upd(data):
                data["total"] = total
                data["seg_dir"] = h3t_rel(seg_dir)
                data["audio_mode"] = ctx.get("audio_mode", "")
                e = data["segments"].setdefault(str(idx), {})
                e.setdefault("fp", ctx["fps"][idx - 1])
                e.update({"video": rel_seg, "size": os.path.getsize(seg_file), "ext": ext, "vtime": time.time()})

            h3t_reg_update(task, upd)
            h3t_prune_logs(protect=task)

        if not ctx and not manual_on:
            h3t_log(task, f"第 {idx} 段已保存为 {seg_file}（没有任务、「手动」关着：只保存这一段，不合并）")
            return {"ui": ui_for(seg_file), "result": (seg_file,)}

        if total > 0 and idx < total:
            # 还没跑到最后一段：只保存这一段，不合并
            h3t_log(task, f"第 {idx}/{total} 段已单独保存为 {seg_file}（跑完最后一段后再合并）")
            return {"ui": ui_for(seg_file), "result": (seg_file,)}

        if ctx:
            files = self._registered_files(ctx, task, total, ext)
            merged = os.path.join(folder, f"{base}_{h3t_next_number(folder, base, ext):05d}.{ext}")
        else:
            files = _contiguous_segments(seg_dir, idx, ext)
            if len(files) < idx:
                # 不静默合并出一个缺段的视频：中间缺文件（常见原因：中途改过编码格式、手动删过文件）就明确报错
                raise ValueError(
                    f"[H3 视频合并] 缺少第 {len(files) + 1} 段的 .{ext} 文件，无法合并第 1~{idx} 段。"
                    "可能是中途改过编码格式，或删除过文件。第 %d 段本身已经保存；请从第 1 段重新跑一遍。" % idx
                )
            merged = os.path.join(folder, f"{base}_merged.{ext}") if total == 0 else os.path.join(
                folder, f"{base}_{h3t_next_number(folder, base, ext):05d}.{ext}")

        return self._finish(ctx, task, kwargs, files, merged, ext, fmt, pix, crf, preset, abr, fps, ve, use_src_audio, ui_for)

    @staticmethod
    def _use_src_audio(ctx, kwargs, task=""):
        """视频编辑任务里时间线的「音频」选了「保留原音频」就自动把原视频音轨贴到成片上；「音频来源」手动选了原视频音频也一样。"""
        if kwargs.get("音频来源") == "原视频音频":
            if ctx and not ctx.get("is_ve"):
                # 防呆：参考生视频 / 首尾帧视频没有原视频，选了原视频音频成片会没声音，自动改用接进来的解码音频
                h3t_log(task, "音频来源=原视频音频，但这是参考生视频/首尾帧视频任务，没有原视频；自动改用解码音频（「音频」口接进来的声音）。",
                        logging.WARNING)
                return False
            return True
        return bool(ctx and ctx.get("audio_mode") == "保留原音频")

    def _merge_existing(self, ctx, task, **kwargs):
        """没接「图像」：只合并。把已经保存好的各段片段（生成时已经裁好重叠、接好衔接）首尾拼起来，不再修剪。
        没有任务编号时：在「最近一个各段齐全的自动任务」和「手动保存的片段（片段/手动，从 seg_001 起连续）」里，选更新的那个。"""
        fps = int(kwargs.get("帧率", 24))
        fmt = kwargs.get("编码格式", "mp4 (H.264)")
        if fmt not in FORMATS:
            raise ValueError(f"编码格式「{fmt}」无效。")
        pix = kwargs.get("像素格式", "yuv420p")
        crf = int(kwargs.get("CRF", 18))
        preset = kwargs.get("编码预设", "veryfast")
        abr = kwargs.get("音频码率", "192k")
        ext = FORMATS[fmt][0]
        folder, base = _split_prefix(kwargs.get("保存名称", "video/MiniMax H3"))
        root = os.path.realpath(folder_paths.get_output_directory())

        def ui_for(path):
            sub = os.path.relpath(os.path.dirname(path), root).replace(os.sep, "/")
            return {"images": [{"filename": os.path.basename(path), "subfolder": "" if sub == "." else sub, "type": "output"}],
                    "animated": (True,)}

        manual_files = []
        if not ctx or not task:
            task = h3t_latest_complete_task()
            ctx = None
            mdir = _seg_dir(folder, "")
            i = 1
            while os.path.isfile(_seg_path(mdir, i, ext)):
                manual_files.append(_seg_path(mdir, i, ext))
                i += 1
            task_t = float((h3t_reg_load(task) or {}).get("updated") or 0) if task else -1.0
            man_t = max((os.path.getmtime(f) for f in manual_files), default=-1.0)
            if manual_files and (not task or man_t > task_t):
                task = ""  # 用手动保存的片段
            else:
                manual_files = []
                if not task:
                    raise ValueError("[H3 视频合并] 没接「图像」时只合并已有片段，但既没找到各段齐全的任务，也没找到手动保存的片段"
                                     f"（{mdir} 里没有 seg_001.{ext}）。")
        if manual_files:
            h3t_log("", f"只合并：用手动保存的 {len(manual_files)} 段片段（{mdir}，seg_001 起连续），不重新采样、不再修剪。")
            for f in manual_files:
                h3t_log("", f"  · {f}")
            merged = os.path.join(folder, f"{base}_{h3t_next_number(folder, base, ext):05d}.{ext}")
            res = self._finish(None, "", kwargs, manual_files, merged, ext, fmt, pix, crf, preset, abr, fps, {}, self._use_src_audio(None, kwargs), ui_for)
            if kwargs.get("合并后删除各段片段", False):
                for f in manual_files:
                    try:
                        os.remove(f)
                    except OSError:
                        pass
            return res
        if ctx is None and task:
            h3t_log(task, f"没有任务编号，自动选用最近一个各段齐全的任务：{task}。")
        reg = h3t_reg_load(task) or {}
        ve = reg.get("ve") or {}
        total = ctx["total"] if ctx else int(reg.get("total") or 0)
        files = self._registered_files(ctx, task, total, ext)
        h3t_log(task, f"只合并：用登记表里已保存的 {len(files)} 段片段，不重新采样。")
        merged = os.path.join(folder, f"{base}_{h3t_next_number(folder, base, ext):05d}.{ext}")
        src_audio = self._use_src_audio(ctx, kwargs, task) or reg.get("audio_mode") == "保留原音频"
        return self._finish(ctx, task, kwargs, files, merged, ext, fmt, pix, crf, preset, abr, fps, ve, src_audio, ui_for)

    def _finish(self, ctx, task, kwargs, files, merged, ext, fmt, pix, crf, preset, abr, fps, ve, use_src_audio, ui_for):
        metadata = None
        if kwargs.get("保存元数据", True):
            import json

            metadata = {}
            if kwargs.get("prompt") is not None:
                metadata["prompt"] = json.dumps(kwargs["prompt"], ensure_ascii=False)
            extra = kwargs.get("extra_pnginfo") or {}
            for k, v in extra.items():
                metadata[str(k)] = json.dumps(v, ensure_ascii=False)
        _concat(files, merged, ext, _codec_args(fmt, pix, crf, preset, abr), metadata)
        h3t_log(task, f"已把第 1~{len(files)} 段合并为 {merged}")
        if use_src_audio:
            self._mux_source_audio(task, merged, ext, ve, abr, fps)

        if task:
            n = h3t_cleanup_task(task, latents=bool(kwargs.get("合并后删除潜空间", True)),
                                 videos=bool(kwargs.get("合并后删除各段片段", False)))
            h3t_log(task, f"任务完成，已清理登记表和 {n} 个中间文件。日志：{h3t_log_path(task)}")
        return {"ui": ui_for(merged), "result": (merged,)}

    @staticmethod
    def _mux_source_audio(task, merged, ext, ve, abr, fps):
        """把原视频整条音轨（从起点秒开始、长度 = 成片长度）贴到成片上。视频流不重新编码。"""
        if not ve.get("video"):
            h3t_log(task, "音频来源=原视频音频，但任务登记表里没有源视频信息（不是视频编辑任务？），成片保持无声。", logging.WARNING)
            return
        src = os.path.join(folder_paths.get_input_directory(), str(ve["video"]).replace("/", os.sep))
        if not os.path.isfile(src):
            h3t_log(task, f"找不到源视频 {ve['video']}，无法贴原视频音轨，成片保持无声。", logging.WARNING)
            return
        secs = float(ve.get("total") or 0) / float(ve.get("fps") or fps)
        out = os.path.splitext(merged)[0] + ".mux." + ext
        acodec = {"mp4": "aac", "mov": "aac", "mkv": "aac", "webm": "libopus"}.get(ext, "aac")
        cmd = [_ffmpeg_bin(), "-y", "-v", "error", "-nostdin", "-i", merged, "-ss", f"{float(ve.get('start') or 0):.4f}", "-t", f"{secs:.4f}",
               "-i", src, "-map", "0:v", "-map", "1:a:0", "-c:v", "copy", "-c:a", acodec, "-b:a", abr, "-map_metadata", "0"]
        if ext == "mp4":
            cmd += ["-movflags", "+faststart+use_metadata_tags"]
        res = subprocess.run(cmd + [out], capture_output=True)
        if res.returncode != 0:
            msg = res.stderr.decode("utf-8", "ignore")[-300:]
            h3t_log(task, f"贴原视频音轨失败（可能源视频没有音轨）：{msg}；成片保持无声。", logging.WARNING)
            try:
                os.remove(out)
            except OSError:
                pass
            return
        os.replace(out, merged)
        h3t_log(task, f"已把原视频音轨（{secs:.2f} 秒）贴到成片上。")

    @staticmethod
    def _registered_files(ctx, task, total, ext):
        """只取任务登记表里第 1~total 段的片段文件；少一段、文件不见了、大小不对、格式变了、内容被改过，都报错。"""
        reg = h3t_reg_load(task) or {}
        segs = reg.get("segments") or {}
        files = []
        for i in range(1, total + 1):
            e = segs.get(str(i)) or {}
            p = h3t_abs(e.get("video"))
            if not e.get("video"):
                raise ValueError(
                    f"[H3 视频合并] 任务登记表里没有第 {i} 段的视频片段，不能合并。请把「输出第几段」设为 0，"
                    f"选择从第 {i} 段继续，或从第 1 段重新开始。任务编号：{task}。")
            if not p or not os.path.isfile(p):
                raise ValueError(f"[H3 视频合并] 第 {i} 段的视频片段文件不存在：{e['video']}。请从第 {i} 段重新开始。任务编号：{task}。")
            if e.get("ext") != ext:
                raise ValueError(f"[H3 视频合并] 第 {i} 段是 .{e.get('ext')} 格式，和当前的 .{ext} 不一致；中途换格式要从第 1 段重跑。")
            if e.get("size") != os.path.getsize(p):
                h3t_log(task, f"⚠ 第 {i} 段的视频片段文件大小和登记的不一致（登记 {e.get('size')} 字节，现在 {os.path.getsize(p)} 字节），"
                              f"可能被手动替换过；照样合并：{p}")
                _LOG.warning("[H3 视频合并] 第 %s 段的片段文件大小和登记的不一致，照样合并：%s", i, p)
            if ctx and e.get("fp") and e["fp"] != ctx["fps"][i - 1]:
                raise ValueError(f"[H3 视频合并] 第 {i} 段的内容在生成之后被改过，不能合并。请从第 {i} 段重新开始。")
            files.append(p)
        return files


NODE_CLASS_MAPPINGS = {"H3VideoMerge": H3VideoMerge}
NODE_DISPLAY_NAME_MAPPINGS = {"H3VideoMerge": "H3 视频合并"}
