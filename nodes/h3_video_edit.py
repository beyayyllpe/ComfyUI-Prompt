# -*- coding: utf-8 -*-
"""H3 视频联合解码（视频编辑模式用）。

视频编辑模式下每一段只采样、保存潜空间，不解码。最后一段跑完后，本节点把各段潜空间在「潜空间里」接起来，
再按窗口逐段解码并流式写成 mp4（任何时刻只解码一个窗口，所以不会爆内存）。
H3 的视频 VAE 以 17 帧为一个片段解码，相邻片段会互相混合；每段单独解码会在每个接缝出现亮度/对比度的小跳变。
这里按「整条连续潜空间」的方式解码：每段的窗口从前一个片段开始，到覆盖本段最后一帧的片段结束。
音频：静音（默认）/ 保留原视频音频 / 模型生成（需要接音频 VAE）。
没有解完也不用重跑采样：各段潜空间都在 output 里，把「手动任务编号」填上，单独运行本节点就能重新解码。
"""
import gc
import logging
import os
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

CATEGORY = "自定义/H3视频"
_AUDIO_SR = 48000
VAE_CLIP_STEPS, VAE_CLIP_FRAMES = 5, 17
FRAMES_PER_STEP = (1, 4, 4, 4, 4)
_LOG = logging.getLogger("h3_video_edit")
PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow"]


# ------------------------------------------------------------------ 纯逻辑：潜空间步 ↔ 像素帧，窗口解码
def pixel_frames(steps):
    return sum(FRAMES_PER_STEP[k % 5] for k in range(steps))


def steps_for_frames(frames):
    steps = covered = 0
    while covered < frames:
        covered += FRAMES_PER_STEP[steps % 5]
        steps += 1
    if covered != frames:
        raise ValueError(f"{frames} 帧不是整数个潜空间步（可用 5/22/39/56）。")
    return steps


def joined_runs(plan):
    """把能在潜空间里接上的段分成一组：有衔接头的段接在前一段后面；硬切（头为 0）另起一组。"""
    runs = []
    for e in plan:
        if e["head"] > 0 and runs:
            runs[-1].append(e)
        else:
            runs.append([e])
    return runs


def delivered_frames(plan, get_video, decode, cat, slicer):
    """逐段产出 (段号, 交付的画面)。

    get_video(段号) → 该段完整视频潜空间 [1,24,T,h,w]（含衔接头）；decode(窗口) → 画面 [n,H,W,3]；
    cat(列表, 维度)、slicer(张量, 起, 止) 沿时间轴（第 3 维）拼接/切片。"""
    for run in joined_runs(plan):
        pieces = []
        for e in run:
            v = get_video(e["index"])
            pieces.append(slicer(v, steps_for_frames(e["head"]), None) if e["head"] else v)
        total = sum(p.shape[2] for p in pieces)
        origin = run[0]["story_start"]
        for e in run:
            first = (e["story_start"] - origin) // VAE_CLIP_FRAMES
            last = (e["story_end"] - origin - 1) // VAE_CLIP_FRAMES
            start = max(first - 1, 0) * VAE_CLIP_STEPS
            stop = min((last + 1) * VAE_CLIP_STEPS + 2, total)
            parts, at = [], 0
            for p in pieces:
                if at < stop and start < at + p.shape[2]:
                    parts.append(slicer(p, max(start - at, 0), stop - at))
                at += p.shape[2]
            window = parts[0] if len(parts) == 1 else cat(parts, 2)
            frames = decode(window)
            if frames.shape[0] != pixel_frames(stop - start):
                raise ValueError(f"第 {e['index'] + 1} 段：{stop - start} 个潜空间步解出 {frames.shape[0]} 帧，应为 {pixel_frames(stop - start)} 帧。")
            offset = (e["story_start"] - origin) - start // VAE_CLIP_STEPS * VAE_CLIP_FRAMES
            n = e["delivered"] - (e["tail_cut"] if e["index"] == plan[-1]["index"] else 0)
            yield e["index"], frames[offset:offset + n]


# ------------------------------------------------------------------ ffmpeg / 文件
def _ffmpeg_bin():
    try:
        from imageio_ffmpeg import get_ffmpeg_exe
        return get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


def _split_prefix(name):
    root = os.path.realpath(folder_paths.get_output_directory())
    rel = (name or "h3_video").strip().strip('"').strip("'").replace("\\", "/").strip("/") or "h3_video"
    sub, base = os.path.split(rel)
    base = "".join(ch for ch in base if ch not in '<>:"|?*').strip(". ") or "h3_video"
    full = os.path.realpath(os.path.join(root, sub)) if sub else root
    if full != root and not full.startswith(root + os.sep):
        raise ValueError("保存名称必须在 ComfyUI 的 output 目录内。")
    os.makedirs(full, exist_ok=True)
    return full, base


def _write_wav(waveform, rate, path):
    """waveform: numpy [C,T] float → 16 位 wav。"""
    import numpy as np
    pcm = (np.clip(waveform.T, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(int(pcm.shape[1]))
        w.setsampwidth(2)
        w.setframerate(int(rate))
        w.writeframes(pcm.tobytes())


def _source_audio(ffmpeg, video, start, seconds, out_wav):
    """从源视频截取 [start, start+seconds) 的声音；没有音轨返回 False。"""
    cmd = [ffmpeg, "-y", "-v", "error", "-nostdin", "-ss", f"{start:.6f}", "-t", f"{seconds:.6f}", "-i", video,
           "-vn", "-ac", "2", "-ar", str(_AUDIO_SR), "-af", "apad", "-t", f"{seconds:.6f}", out_wav]
    res = subprocess.run(cmd, capture_output=True)
    return res.returncode == 0 and os.path.isfile(out_wav) and os.path.getsize(out_wav) > 44


class _Writer:
    """一个 ffmpeg 进程：画面逐段从 stdin 写入，声音来自一个 wav 文件（没有就补静音）。"""

    def __init__(self, ffmpeg, out_path, width, height, fps, wav, crf, preset):
        self.out, self.part = out_path, out_path + ".part.mp4"
        cmd = [ffmpeg, "-y", "-v", "error", "-nostdin", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-r", str(fps), "-i", "-"]
        cmd += ["-i", wav] if wav else ["-f", "lavfi", "-i", f"anullsrc=r={_AUDIO_SR}:cl=stereo"]
        cmd += ["-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", "-c:v", "libx264", "-preset", preset, "-crf", str(int(crf)), "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", "-af", "apad", "-shortest", "-movflags", "+faststart", self.part]
        self.err = tempfile.TemporaryFile()
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=self.err)

    def write(self, uint8_frames):
        try:
            for k in range(uint8_frames.shape[0]):
                self.proc.stdin.write(uint8_frames[k].tobytes())
        except BrokenPipeError:
            pass

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        code = self.proc.wait()
        if code != 0:
            self.err.seek(0)
            raise RuntimeError("ffmpeg 编码失败：" + self.err.read().decode("utf-8", "ignore")[-400:])
        os.replace(self.part, self.out)

    def abort(self):
        try:
            self.proc.kill()
        except Exception:
            pass
        try:
            os.remove(self.part)
        except OSError:
            pass


# ------------------------------------------------------------------ 节点
class MiniMaxH3JointDecode:
    """视频编辑模式的收尾：最后一段跑完后，把所有段的潜空间接起来解码并保存成片。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "vae": ("VAE", {"tooltip": "H3 视频 VAE（和采样用的同一个）。"}),
                "保存名称": ("STRING", {"default": "video/h3_edit", "tooltip": "和官方保存视频一样的文件名前缀，自动编号，不覆盖。"}),
                "CRF": ("INT", {"default": 18, "min": 0, "max": 51, "tooltip": "画质，越小越好、文件越大。常用 15~23。"}),
                "编码预设": (PRESETS, {"default": "veryfast", "tooltip": "H.264 编码速度与压缩效率的取舍。ultrafast 最快，slow 较慢；相同 CRF 下，较慢通常压缩效率更高，但不保证画质或体积完全一致。默认 veryfast（很快）。"}),
                "合并后删除潜空间": ("BOOLEAN", {"default": False, "tooltip": "默认保留各段潜空间文件，解码出问题时可以不重跑采样、直接重新解码。"}),
            },
            "optional": {
                "latent": ("LATENT", {"tooltip": "接「H3 潜空间保存」的 latent 输出（保证先保存再解码）。手动重新解码时可以不接。"}),
                "音频VAE": ("VAE", {"tooltip": "只有「多段输入」里音频选「模型生成」时才需要。"}),
                "手动任务编号": ("STRING", {"default": "", "tooltip": "留空：跟随「多段输入」跑全部段（最后一段才解码）。填任务编号：不用采样，直接把那个任务已保存的潜空间全部重新解码。"}),
            },
            "hidden": {"prompt": "PROMPT", "unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("成片路径",)
    FUNCTION = "decode"
    CATEGORY = CATEGORY
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    @h3t_logged("H3 联合解码")
    def decode(self, **kw):
        ctx = h3t_context(kw.get("prompt"), kw.get("unique_id"))
        manual = h3t_clean_id(kw.get("手动任务编号"))
        task = manual or (ctx["task"] if ctx else "")
        try:
            return self._decode(ctx if not manual else None, task, bool(manual), **kw)
        except Exception as exc:
            h3t_log(task, f"联合解码失败：{exc}", logging.ERROR)
            raise

    # ---- 读登记表 / 潜空间文件
    @staticmethod
    def _load_video_latent(rel):
        from safetensors.torch import load_file
        path = h3t_abs(rel)
        if not path or not os.path.isfile(path):
            raise FileNotFoundError(f"潜空间文件不存在：{rel}（可能被删除）。")
        data = load_file(path)
        if "video" not in data:
            raise ValueError(f"不是 H3 潜空间文件：{rel}")
        return data["video"].contiguous(), data.get("audio")

    def _decode(self, ctx, task, manual, **kw):
        import torch

        reg = h3t_reg_load(task) if task else None
        if not task:
            raise ValueError("没有任务编号：请用「H3 多段输入」（视频编辑模式、输出第几段=0）运行，或填「手动任务编号」。")
        if not reg or not (reg.get("ve") or {}).get("plan"):
            raise ValueError(f"任务 {task} 的登记表里没有视频编辑的分段规划（任务不存在或已清理）。")
        ve = reg["ve"]
        plan, total = ve["plan"], len(ve["plan"])
        if ctx and ctx["seg"] < total:
            h3t_log(task, f"第 {ctx['seg']}/{total} 段：采样完成，等最后一段跑完后统一解码。")
            return {"ui": {"text": [f"等待最后一段（{ctx['seg']}/{total}）"]}, "result": ("",)}

        # 各段的潜空间：当前段优先用输入的 latent（还没写登记表时），其余按登记表
        segs = reg.get("segments") or {}
        files = {}
        for e in plan:
            entry = segs.get(str(e["index"] + 1)) or {}
            if not entry.get("latent"):
                if ctx and e["index"] + 1 == ctx["seg"] and kw.get("latent") is not None:
                    continue
                raise FileNotFoundError(f"登记表里没有第 {e['index'] + 1} 段的潜空间（这一段没跑完，或文件被清理）。请先跑完这一段。")
            if ctx and entry.get("fp") and entry["fp"] != ctx["fps"][e["index"]]:
                raise ValueError(f"第 {e['index'] + 1} 段在保存潜空间之后被改过（提示词或素材不同），请从这一段重新跑。")
            files[e["index"]] = entry["latent"]

        cache = {}

        def get_video(i):
            if i in files:
                if i not in cache:
                    cache[i] = self._load_video_latent(files[i])[0]
                return cache[i]
            lat = kw["latent"]["samples"]
            return lat.unbind()[0] if getattr(lat, "is_nested", False) else lat

        vae = kw["vae"]

        def decode(window):
            frames = vae.decode(window)
            return frames.reshape(-1, *frames.shape[-3:]) if frames.ndim == 5 else frames

        def cat(parts, dim):
            return torch.cat(parts, dim)

        def slicer(t, a, b):
            return t[:, :, a:b]

        ffmpeg = _ffmpeg_bin()
        if not ffmpeg:
            raise RuntimeError("找不到 ffmpeg。")
        folder, stem = _split_prefix(kw["保存名称"])
        out_path = os.path.join(folder, f"{stem}_{h3t_next_number(folder, stem, 'mp4'):05d}.mp4")
        tmp = tempfile.mkdtemp(prefix="h3ve_")
        writer = None
        try:
            wav = self._audio(ve, plan, files, kw, tmp, ffmpeg, task)
            h3t_log(task, f"联合解码开始：{total} 段，{ve['total']} 帧，{ve['width']}×{ve['height']}，音频={ve['audio']}。")
            n_done = 0
            for idx, frames in delivered_frames(plan, get_video, decode, cat, slicer):
                u8 = (frames.detach().float().clamp(0, 1) * 255.0 + 0.5).to(torch.uint8).cpu().numpy()
                if writer is None:
                    writer = _Writer(ffmpeg, out_path, u8.shape[2], u8.shape[1], ve.get("fps", 24), wav, kw["CRF"], kw["编码预设"])
                writer.write(u8)
                n_done += u8.shape[0]
                del frames, u8
                h3t_log(task, f"第 {idx + 1}/{total} 段已解码写入（累计 {n_done} 帧）。")
                if idx + 1 < total:
                    gc.collect()
            writer.close()
            writer = None
        except Exception:
            if writer is not None:
                writer.abort()
            raise
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if kw.get("合并后删除潜空间"):
            h3t_cleanup_task(task, latents=True, videos=False)
        h3t_log(task, f"联合解码完成：{h3t_rel(out_path)}（{n_done} 帧）。")
        root = h3t_root()
        sub = os.path.relpath(os.path.dirname(out_path), root)
        sub = "" if sub == "." else sub.replace(os.sep, "/")
        return {"ui": {"images": [{"filename": os.path.basename(out_path), "subfolder": sub, "type": "output"}], "animated": (True,)},
                "result": (out_path,)}

    # ---- 声音：静音 / 保留原音频 / 模型生成
    def _audio(self, ve, plan, files, kw, tmp, ffmpeg, task):
        mode = ve.get("audio", "静音")
        seconds = (plan[-1]["story_end"] - plan[-1]["tail_cut"]) / float(ve.get("fps", 24))
        wav = os.path.join(tmp, "a.wav")
        if mode == "静音":
            return None
        if mode == "保留原音频":
            src = h3t_abs_input(ve["video"])
            if _source_audio(ffmpeg, src, float(ve["start"]), seconds, wav):
                return wav
            h3t_log(task, "源视频没有音轨，成片静音。", logging.WARNING)
            return None
        # 模型生成
        avae = kw.get("音频VAE")
        if avae is None:
            raise ValueError("「多段输入」的音频选了「模型生成」，需要把音频 VAE 接到本节点的「音频VAE」。")
        import torch
        from comfy_extras.nodes_audio import vae_decode_audio
        waves, rate = [], None
        for e in plan:
            if e["index"] in files:
                audio = self._load_video_latent(files[e["index"]])[1]
            else:
                lat = kw["latent"]["samples"]
                audio = lat.unbind()[-1]
            dec = vae_decode_audio(avae, {"samples": audio})
            w, rate = dec["waveform"], int(dec["sample_rate"])
            head = int(round(e["head"] / 24.0 * rate))
            n = round(e["story_end"] / 24.0 * rate) - round(e["story_start"] / 24.0 * rate)
            w = w[..., head:head + n]
            if w.shape[-1] < n:
                w = torch.nn.functional.pad(w, (0, n - w.shape[-1]))
            waves.append(w[0].float().cpu())
        full = torch.cat(waves, -1)
        cut = int(round(plan[-1]["tail_cut"] / 24.0 * rate))
        if cut:
            full = full[..., : full.shape[-1] - cut]
        _write_wav(full.numpy(), rate, wav)
        return wav


def h3t_abs_input(name):
    base = os.path.realpath(folder_paths.get_input_directory())
    full = os.path.realpath(os.path.join(base, str(name).replace("\\", "/")))
    if not full.startswith(base + os.sep) or not os.path.isfile(full):
        raise FileNotFoundError(f"找不到源视频：{name}（应在 input 目录里）。")
    return full


NODE_CLASS_MAPPINGS = {"MiniMaxH3JointDecode": MiniMaxH3JointDecode}
NODE_DISPLAY_NAME_MAPPINGS = {"MiniMaxH3JointDecode": "H3 视频联合解码"}
