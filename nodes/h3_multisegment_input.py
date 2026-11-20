# -*- coding: utf-8 -*-
"""H3 多段输入（单文件节点，放进 nodes/ 即可）

多段提示词 + 公共/本段参考素材（图/视频/音频），按「输出第几段」输出带官方标签的提示词和素材，
接官方 MiniMaxH3ReferenceToVideo 同编号的输入。只做输入，不做采样/解码/拼接。
前端编辑器在 web/h3_multiseg.js（和本文件一起放进你的框架目录）。

编号：公共素材占前面的编号，本段额外素材接在后面（图最多 9、视频 3、音频 3）。
提示词引用：[[c:img:1]] 公共第 1 张图；[[s:img:1]] 本段第 1 张图；vid/aud 同理；
也可直接写官方标签 <Picture N> / <Video N> / <Audio N>（会校验编号存在）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
import subprocess

# ============================================================ 纯逻辑（时间线解析/编号/校验）

# kind -> (JSON 键, 官方标签名, 上限, 中文名)
KINDS = {
    "img": ("images", "Picture", 9, "参考图"),
    "vid": ("videos", "Video", 3, "参考视频"),
    "aud": ("audios", "Audio", 3, "参考音频"),
}
# 与上一段的衔接方式（第 2 段起有意义）。与「H3 潜空间前缀复制」节点的下拉选项一致。
JOINS = ("复制", "引导", "硬切")
DEFAULT_JOIN = "复制"
MODE_REF = "参考生视频"
MODE_FL = "首尾帧视频"
MODE_VE = "视频编辑"
MODES = (MODE_REF, MODE_FL, MODE_VE)
GUIDE_MAX = 6  # 每段最多几张引导图片
_TOKEN_RE = re.compile(r"\[\[\s*([cs])\s*:\s*(img|vid|aud)\s*:\s*(\d+)\s*\]\]", re.IGNORECASE)
_LITERAL_RE = re.compile(r"<(Picture|Video|Audio)\s+(\d+)\s*>", re.IGNORECASE)
_TAG_TO_KIND = {"picture": "img", "video": "vid", "audio": "aud"}


class TimelineError(ValueError):
    """时间线数据有问题（会原样显示给用户）。"""


def _clean_items(raw, where: str) -> list[dict]:
    """只保留填了文件名的素材项，保持顺序。"""
    out: list[dict] = []
    if raw is None:
        return out
    if not isinstance(raw, list):
        raise TimelineError(f"{where}：素材列表格式不对（应为列表）")
    for item in raw:
        if isinstance(item, str):
            item = {"file": item}
        if not isinstance(item, dict):
            continue
        f = str(item.get("file") or item.get("fileName") or "").replace("\\", "/").strip()
        if f:
            out.append({"file": f})
    return out


def _clean_one(raw):
    """单张素材（首帧/尾帧）：{"file": ...} 或字符串；没填返回 None。"""
    items = _clean_items([raw] if raw else [], "单张素材")
    return items[0] if items else None


MAX_GUIDE_POINTS = 30  # 视频编辑最多 30 个引导点（它们都装在同一个「引导」输出口里）


def parse_timeline(text: str) -> dict:
    """解析并规整 timeline_data。"""
    if isinstance(text, dict):
        data = text
    else:
        s = (text or "").strip()
        if not s:
            raise TimelineError("时间线是空的：请先在节点里添加至少一段。")
        try:
            data = json.loads(s)
        except json.JSONDecodeError as exc:
            raise TimelineError(f"时间线数据不是有效的 JSON：{exc}")
    if not isinstance(data, dict):
        raise TimelineError("时间线数据格式不对（应为对象）。")

    common_raw = data.get("common") or {}
    if not isinstance(common_raw, dict):
        raise TimelineError("公共区域格式不对。")
    common = {"prompt": str(common_raw.get("prompt") or "")}
    for _k, (key, _tag, _mx, _cn) in KINDS.items():
        common[key] = _clean_items(common_raw.get(key), f"公共区域的{_cn}")

    segs_raw = data.get("segments")
    if not isinstance(segs_raw, list) or not segs_raw:
        raise TimelineError("没有任何分段：请至少添加一段。")
    segments = []
    for i, sr in enumerate(segs_raw):
        if not isinstance(sr, dict):
            raise TimelineError(f"第 {i + 1} 段格式不对。")
        seg = {
            "prompt": str(sr.get("prompt") or ""),
            "use_common": bool(sr.get("use_common", True)),
            "first": _clean_one(sr.get("first")),
            "last": _clean_one(sr.get("last")),
            "guides": _clean_items(sr.get("guides"), f"第 {i + 1} 段的引导图片"),
        }
        for _k, (key, _tag, _mx, _cn) in KINDS.items():
            seg[key] = _clean_items(sr.get(key), f"第 {i + 1} 段的{_cn}")
        segments.append(seg)
    out = {"common": common, "segments": segments}
    video = data.get("video") if isinstance(data.get("video"), dict) else {}
    out["video"] = {"file": str(video.get("file") or "").replace("\\", "/").strip(),
                    "start": max(0.0, float(video.get("start") or 0.0)), "end": max(0.0, float(video.get("end") or 0.0))}
    try:
        out["cuts"] = [float(x) for x in (data.get("cuts") or [])]
    except (TypeError, ValueError):
        raise TimelineError("分割点必须是数字（秒）。")
    out["ve"] = data.get("ve") if isinstance(data.get("ve"), dict) else {}
    # 视频编辑的引导点：[{file, f}]，f = 在整条视频里的帧号（24 fps，从 0 数）；一个点一张图占一帧。
    # 兼容上一版的 gtracks（每条轨道一个点）。
    raw = data.get("gpoints") if isinstance(data.get("gpoints"), list) else (data.get("gtracks") if isinstance(data.get("gtracks"), list) else [])
    gps = []
    for g in raw[:MAX_GUIDE_POINTS]:
        g = g if isinstance(g, dict) else {}
        try:
            f = max(0, int(g.get("f")))
        except (TypeError, ValueError):
            f = 0
        gps.append({"file": str(g.get("file") or "").replace("\\", "/").strip(), "f": f})
    out["gpoints"] = gps
    return out


def _resolve_prompt(text: str, common_counts: dict, seg_counts: dict, seg_no: int, free_av: bool = False) -> str:
    """把 [[c:img:1]] 之类换成官方标签，并校验所有引用都存在。"""
    totals = {k: common_counts[k] + seg_counts[k] for k in KINDS}

    def repl(m: re.Match) -> str:
        scope, kind, n = m.group(1).lower(), m.group(2).lower(), int(m.group(3))
        _key, tag, _mx, cn = KINDS[kind]
        if n < 1:
            raise TimelineError(f"第 {seg_no} 段：引用编号必须从 1 开始：{m.group(0)}")
        if scope == "c":
            if n > common_counts[kind]:
                raise TimelineError(
                    f"第 {seg_no} 段：提示词引用了公共区域第 {n} 个{cn}，但公共区域只有 {common_counts[kind]} 个。"
                )
            return f"<{tag} {n}>"
        if n > seg_counts[kind]:
            raise TimelineError(
                f"第 {seg_no} 段：提示词引用了本段第 {n} 个{cn}，但本段只添加了 {seg_counts[kind]} 个。"
            )
        return f"<{tag} {common_counts[kind] + n}>"

    out = _TOKEN_RE.sub(repl, text)

    for m in _LITERAL_RE.finditer(out):
        kind = _TAG_TO_KIND[m.group(1).lower()]
        n = int(m.group(2))
        if free_av and kind in ("vid", "aud"):  # 视频编辑：<Video N>/<Audio N> 由后面的节点负责，这里原样放行
            continue
        if n < 1 or n > totals[kind]:
            _key, tag, _mx, cn = KINDS[kind]
            raise TimelineError(
                f"第 {seg_no} 段：提示词里的 <{tag} {n}> 找不到对应{cn}（本段共有 {totals[kind]} 个）。"
            )
    return out


def resolve_segment(timeline: dict, index: int, mode: str = MODE_REF, common_prompt=None) -> dict:
    """取出第 index 段（0 起）最终要用的提示词与素材文件列表。

    mode：参考生视频（图/视频/音频参考）或 首尾帧视频（只用首帧/尾帧，不用参考素材）。
    common_prompt：不为 None 时，用它替代时间线里的公共提示词（来自节点上的连线）。
    """
    if mode not in MODES:
        raise TimelineError(f"生成模式「{mode}」无效，应为：{'、'.join(MODES)}。")
    segs = timeline["segments"]
    count = len(segs)
    if index < 0 or index >= count:
        raise TimelineError(f"段序号 {index} 超出范围：一共 {count} 段（序号从 0 开始）。")
    common = timeline["common"]
    seg = segs[index]
    seg_no = index + 1

    files: dict[str, list[str]] = {}
    common_counts: dict[str, int] = {}
    seg_counts: dict[str, int] = {}
    for kind, (key, _tag, mx, cn) in KINDS.items():
        if mode == MODE_FL:  # 首尾帧模式不使用参考素材
            common_counts[kind] = seg_counts[kind] = 0
            files[kind] = []
            continue
        common_counts[kind] = len(common[key])
        seg_counts[kind] = len(seg[key])
        files[kind] = [x["file"] for x in common[key]] + [x["file"] for x in seg[key]]
        if len(files[kind]) > mx:
            raise TimelineError(
                f"第 {seg_no} 段：{cn}共 {len(files[kind])} 个（公共 {common_counts[kind]} + 本段 {seg_counts[kind]}），"
                f"超过官方上限 {mx} 个。"
            )

    free_av = mode == MODE_VE
    seg_prompt = _resolve_prompt(seg["prompt"].strip(), common_counts, seg_counts, seg_no, free_av)
    common_text = ""
    if seg["use_common"]:
        raw_common = common["prompt"] if common_prompt is None else str(common_prompt)
        common_text = _resolve_prompt(raw_common.strip(), common_counts, seg_counts, seg_no, free_av)
    prompt = f"{common_text}\n\n{seg_prompt}" if (common_text and seg_prompt) else (common_text or seg_prompt)
    if not prompt.strip() and mode != MODE_VE:  # 视频编辑可以不写提示词（比如只做超分放大）
        raise TimelineError(f"第 {seg_no} 段的提示词是空的（公共提示词也没有）。")

    guides = [x["file"] for x in seg["guides"]]
    if len(guides) > GUIDE_MAX:
        raise TimelineError(f"第 {seg_no} 段：引导图片 {len(guides)} 张，超过上限 {GUIDE_MAX} 张。")
    default_join = "硬切" if mode == MODE_FL else DEFAULT_JOIN
    return {
        "mode": mode,
        "first": seg["first"]["file"] if (mode == MODE_FL and seg["first"]) else None,
        "last": seg["last"]["file"] if (mode == MODE_FL and seg["last"]) else None,
        "guides": guides,
        "prompt": prompt,
        "files": files,
        "common_counts": common_counts,
        "seg_counts": seg_counts,
        # 第 1 段没有上一段，输出「硬切」（前缀复制节点会原样放行）。
        "join": "硬切" if (index == 0 or mode == MODE_VE) else default_join,
        "index": index,
        "count": count,
        "is_first": index == 0,
        "is_last": index == count - 1,
    }


def make_report(res: dict) -> str:
    lines = [
        f"第 {res['index'] + 1}/{res['count']} 段"
        + ("（第一段）" if res["is_first"] else "")
        + ("（最后一段）" if res["is_last"] else ""),
    ]
    for kind, (_key, tag, mx, cn) in KINDS.items():
        fl = res["files"][kind]
        if fl:
            parts = [f"<{tag} {i + 1}>={f}" for i, f in enumerate(fl)]
            lines.append(f"{cn}（{len(fl)}/{mx}）：" + "；".join(parts))
    lines.append("最终提示词：")
    lines.append(res["prompt"])
    return "\n".join(lines)


# ============================================================ 视频编辑：源视频读取 / 遮罩（numpy + OpenCV，惰性导入）
VE_FPS = 24
VE_VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v")


def ve_fit_size(src_w, src_h, width, height, long_edge):
    """渲染尺寸（对齐 32）：宽高都给了就用；否则按最长边和源视频比例。"""
    def snap(v):
        return max(32, int(round(v / 32.0)) * 32)
    if width > 0 and height > 0:
        return snap(width), snap(height)
    if src_w <= 0 or src_h <= 0:
        raise TimelineError("读不到源视频的宽高。")
    scale = float(long_edge) / max(src_w, src_h)
    return snap(src_w * scale), snap(src_h * scale)


def ve_probe(path):
    """{fps, width, height, duration}。优先 PyAV，没有就用 OpenCV。"""
    try:
        import av
    except ImportError:
        av = None
    if av is not None:
        with av.open(path) as c:
            s = c.streams.video[0]
            fps = float(s.average_rate) if s.average_rate else float(s.guessed_rate or 0)
            if s.duration is not None and s.time_base:
                dur = float(s.duration * s.time_base)
            elif c.duration:
                dur = c.duration / av.time_base
            else:
                dur = 0.0
            return {"fps": fps, "width": s.codec_context.width, "height": s.codec_context.height, "duration": dur}
    import cv2
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise TimelineError(f"打不开视频：{path}")
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
    n = float(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    cap.release()
    return {"fps": fps, "width": w, "height": h, "duration": (n / fps) if fps > 0 else 0.0}


def _ve_cover(sw, sh, w, h):
    scale = max(w / float(sw), h / float(sh))
    return max(w, int(round(sw * scale))), max(h, int(round(sh * scale)))


def _ve_center_crop(a, w, h):
    y0, x0 = (a.shape[0] - h) // 2, (a.shape[1] - w) // 2
    return a[y0:y0 + h, x0:x0 + w]


def _ve_decode_pyav(path, t0, count, fps, w, h):
    import av
    import numpy as np
    targets = [t0 + i / float(fps) for i in range(count)]
    out, idx, prev = [], 0, None
    with av.open(path) as c:
        s = c.streams.video[0]
        s.thread_type = "AUTO"
        tb = float(s.time_base)
        origin = float(s.start_time * s.time_base) if s.start_time is not None else 0.0
        dw, dh = _ve_cover(s.codec_context.width, s.codec_context.height, w, h)
        c.seek(int(max(0.0, t0 - 0.5 + origin) / tb), stream=s, backward=True)

        def arr(fr):
            return _ve_center_crop(fr.to_ndarray(width=dw, height=dh, format="rgb24"), w, h)

        for fr in c.decode(s):
            ft = (float(fr.pts * s.time_base) if fr.pts is not None else 0.0) - origin
            while idx < count and ft >= targets[idx] - 1e-6:
                pick = fr
                if prev is not None and abs(prev[0] - targets[idx]) < abs(ft - targets[idx]):
                    pick = prev[1]
                out.append(arr(pick))
                idx += 1
            prev = (ft, fr)
            if idx >= count:
                break
        if idx < count:  # 视频比要求的短：重复最后一帧
            if prev is None:
                raise TimelineError(f"读不到任何画面：{path}（起点 {t0:.3f} 秒）")
            last = arr(prev[1])
            out.extend([last] * (count - idx))
    return np.stack(out, 0)


def _ve_decode_cv2(path, t0, count, fps, w, h):
    import cv2
    import numpy as np
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise TimelineError(f"打不开视频：{path}")
    nfps = float(cap.get(cv2.CAP_PROP_FPS) or fps)
    sw, sh = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    dw, dh = _ve_cover(sw, sh, w, h)
    wanted = [min(int(round((t0 + i / float(fps)) * nfps)), max(total - 1, 0)) for i in range(count)]
    cap.set(cv2.CAP_PROP_POS_FRAMES, wanted[0])
    cur, cache, last = wanted[0], {}, None
    need, top = set(wanted), max(wanted)
    while cur <= top:
        ok, bgr = cap.read()
        if not ok:
            break
        if cur in need:
            rgb = cv2.cvtColor(cv2.resize(bgr, (dw, dh), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB)
            cache[cur] = last = _ve_center_crop(rgb, w, h)
        cur += 1
    cap.release()
    if last is None:
        raise TimelineError(f"读不到任何画面：{path}（起点 {t0:.3f} 秒）")
    out = []
    for f in wanted:
        last = cache.get(f, last)
        out.append(last)
    return np.stack(out, 0)


def ve_decode_window(path, t0, count, fps, w, h):
    """从 t0 秒起取 count 帧（按 fps 重采样），缩放并居中裁成 w×h，返回 uint8 [count,h,w,3]。"""
    if count < 1:
        raise TimelineError("要读取的帧数必须大于 0。")
    try:
        import av  # noqa: F401
    except ImportError:
        return _ve_decode_cv2(path, t0, count, fps, w, h)
    return _ve_decode_pyav(path, t0, count, fps, w, h)


def ve_shape_mask(gray, shape="边界框", grow=16, threshold=0.5):
    """gray: float [n,h,w] 0..1 → float32 [n,h,w]，1 = 要重绘。边界框：每帧取外接矩形再外扩；轮廓：只取遮罩本身再外扩。没有遮罩的帧全 0。"""
    import numpy as np
    hole = gray > threshold
    n, h, w = hole.shape
    grow = max(0, int(grow))
    if shape == "边界框":
        out = np.zeros((n, h, w), np.float32)
        rows, cols = hole.any(axis=2), hole.any(axis=1)
        for i in range(n):
            if not rows[i].any():
                continue
            top, bottom = int(rows[i].argmax()), h - int(rows[i][::-1].argmax())
            left, right = int(cols[i].argmax()), w - int(cols[i][::-1].argmax())
            out[i, max(0, top - grow):min(h, bottom + grow), max(0, left - grow):min(w, right + grow)] = 1.0
        return out
    if grow <= 0:
        return hole.astype(np.float32)
    import cv2
    k = np.ones((2 * grow + 1, 2 * grow + 1), np.uint8)
    return np.stack([cv2.dilate(f.astype(np.uint8), k) > 0 for f in hole], 0).astype(np.float32)


def fit_edge_size(src_w, src_h, edge):
    """参考视频/源视频输出尺寸：edge<=0 原样；否则长边=edge（可放大可缩小），等比，宽高取偶数。前后端同一规则。"""
    src_w, src_h = int(src_w), int(src_h)
    if int(edge or 0) <= 0:
        scale = 1.0
    else:
        scale = float(int(edge)) / float(max(src_w, src_h))
    ev = lambda v: max(2, int(math.floor(v / 2.0 + 0.5)) * 2)  # 四舍五入（0.5 进位），和前端 Math.round 一致
    return ev(src_w * scale), ev(src_h * scale)


def ve_size_mp(src_w, src_h, mp):
    """按百万像素算生成尺寸：保持源视频比例，总像素约 mp×1,000,000，宽高各对齐到 32 的倍数。"""
    if src_w <= 0 or src_h <= 0:
        raise TimelineError("读不到源视频的宽高。")
    scale = (float(mp) * 1e6 / (src_w * src_h)) ** 0.5

    def snap(v):
        return max(32, int(round(v / 32.0)) * 32)
    return snap(src_w * scale), snap(src_h * scale)


def ve_audio_window(path, t0, seconds, sr=44100):
    """取源视频 [t0, t0+seconds) 的音频：float32 立体声 [2, T]；不够长或没有音轨就补静音。
    返回 (numpy 数组, 采样率, 说明)。"""
    import subprocess
    import numpy as np
    from shutil import which
    try:
        from imageio_ffmpeg import get_ffmpeg_exe
        ff = get_ffmpeg_exe()
    except Exception:
        ff = which("ffmpeg")
    need = max(1, int(round(seconds * sr)))
    if not ff:
        return np.zeros((2, need), np.float32), sr, "找不到 ffmpeg，音频用静音"
    cmd = [ff, "-v", "error", "-nostdin", "-ss", f"{max(0.0, t0):.4f}", "-t", f"{seconds:.4f}", "-i", path,
           "-vn", "-ac", "2", "-ar", str(sr), "-f", "f32le", "-"]
    try:
        raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    except Exception:
        raw = b""
    n = len(raw) // 8
    if n <= 0:
        return np.zeros((2, need), np.float32), sr, "源视频没有音轨，音频用静音"
    a = np.frombuffer(raw[: n * 8], dtype=np.float32).reshape(-1, 2).T.copy()
    note = "ok"
    if a.shape[1] < need:
        note = f"音频比画面短 {(need - a.shape[1]) / sr:.2f} 秒，已补静音"
        a = np.concatenate([a, np.zeros((2, need - a.shape[1]), np.float32)], axis=1)
    return a[:, :need], sr, note


# ============================================================ ComfyUI 节点
import asyncio
import copy
import json
import threading
import time
import urllib.error
import urllib.request


log = logging.getLogger("H3MultiSegment")

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


def h3t_ve_link_prompts(tl, data, kw):
    """视频编辑：接了「全局提示词」/「局部提示词」输入时，以连进来的文字为准，替换界面里写的提示词。
    没接线就不动（界面已经把提示词写进各段）。局部文字用一行「---」分段；某段留空就用全局。"""
    if not isinstance(kw, dict):
        return tl
    gin, lin = kw.get(W_GIN), kw.get(W_LIN)
    if gin is None and lin is None:
        return tl
    try:
        raw = json.loads(data) if isinstance(data, str) and data.strip() else {}
    except ValueError:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    g = str(raw.get("gprompt") or "") if gin is None else str(gin)
    if lin is None:
        ls = [str(x or "") for x in (raw.get("lprompts") or [])]
        local_on = raw.get("pmode") == "local"
    else:
        parts = [[]]
        for line in str(lin).split("\n"):
            if line.strip() == "---":
                parts.append([])
            else:
                parts[-1].append(line)
        ls = ["\n".join(x).strip() for x in parts]
        local_on = True  # 接了局部输入：就用局部（空段仍用全局）
    for i, seg in enumerate(tl.get("segments", [])):
        loc = ls[i].strip() if i < len(ls) else ""
        seg["prompt"] = loc if (local_on and loc) else g
    return tl


CATEGORY = "自定义/H3视频"
W_DATA = "时间线数据"
W_MODE = "生成模式"
W_COMMON = "公共提示词"  # 旧名：已不再作为输入口（保留常量只为兼容）
W_GIN = "全局提示词"  # 视频编辑：接进来的文字替换界面里的全局提示词
W_LIN = "局部提示词"  # 视频编辑：接进来的文字替换局部提示词；用一行「---」分成各段（按段顺序，空段用全局）
W_SEG = "输出第几段"
W_EDGE = "参考视频最大边长"
W_TASK = "任务编号"
W_TOEND = "到最后一段"
W_JOIN = "段间衔接"
W_HEAD = "重叠帧数"
W_FPS = "参考视频帧率"

_AUDIO_SR = 48000
_REF_VIDEO_FPS = 24.0
_REF_VIDEO_MAX_SECONDS = 15.0  # 官方：每个参考视频 2~15 秒
_REF_VIDEO_MIN_FRAMES = 5      # 官方：至少 5 帧

# 输出口的固定顺序（前端只能从末尾删口，所以按「序号」交错排：每一组 参考图N 参考视频N 视频音轨N 参考音频N 引导图片N）。
# 首尾帧模式下，第 3、4 个口（参考图1、参考视频1 的位置，同为 IMAGE）由前端改名为「首帧」「尾帧」。
# 前端 JS 里有同一份表，两边必须一致。
OUTPUT_LAYOUT: list[tuple[str, str, str, int]] = [  # (名字, 类型, 种类, 序号)
    ("提示词", "STRING", "prompt", 0),
]
for _r in (1, 2, 3):
    OUTPUT_LAYOUT += [
        (f"参考图{_r}", "IMAGE", "img", _r),
        (f"参考视频{_r}", "IMAGE", "vid", _r),
        (f"参考视频音轨{_r}", "AUDIO", "vaud", _r),  # 对应官方 ref_video_audio_N：参考视频自带的声音
        (f"参考音频{_r}", "AUDIO", "aud", _r),
        (f"引导图片{_r}", "IMAGE", "guide", _r),
    ]
for _r in (4, 5, 6):
    OUTPUT_LAYOUT += [(f"参考图{_r}", "IMAGE", "img", _r), (f"引导图片{_r}", "IMAGE", "guide", _r)]
for _r in (7, 8, 9):
    OUTPUT_LAYOUT.append((f"参考图{_r}", "IMAGE", "img", _r))
# 三个模式共用这些输出口，位置的含义各不相同（见下面的 LAYOUT_*），所以所有口的类型都声明为任意类型 "*"。
# 其它模式下前端会把它们改名，连线时 ComfyUI 不再按类型拦截，接错口请自己留意名字。
OUTPUT_LAYOUT = [(o[0], "*", o[2], o[3]) for o in OUTPUT_LAYOUT]

# 每个模式的输出口排布：(名字, 种类, 序号)。位置一旦定下就不随素材数量变化——素材变多只在末尾追加，已接的线不会错位。
LAYOUT_REF = [(n, k, i) for n, _t, k, i in OUTPUT_LAYOUT]
LAYOUT_FL = [("首帧", "first", 0), ("尾帧", "last", 0), ("提示词", "prompt", 0)] + \
            [(f"引导图片{r}", "guide", r) for r in range(1, 7)]
def ve_layout(n_ref):
    """视频编辑的输出口：缩放视频 / 原视频 / 音频 / 提示词 / 长度 / 参考图1…N / 引导。
    参考图只显示用到的张数（N = 各段里最多的张数），「引导」永远是最后一个口；N 变化时界面会把「引导」口上的线一起搬过去。"""
    out = [("缩放视频", "video", 0), ("原视频", "orig", 0), ("音频", "audio", 0), ("提示词", "prompt", 0), ("长度", "len", 0)]
    out += [(f"参考图{r}", "img", r) for r in range(1, int(n_ref) + 1)]
    return out + [("引导", "guide", 0)]


LAYOUT_VE = ve_layout(0)


def _pack(layout, singles, pools):
    out = []
    for _name, kind, n in layout:
        if kind in singles:
            out.append(singles[kind])
        else:
            pool = pools.get(kind, [])
            out.append(pool[n - 1] if 0 < n <= len(pool) else None)
    return tuple(out + [None] * (len(OUTPUT_LAYOUT) - len(out)))


# --------------------------------------------------------------------------- 文件读取
def _input_path(rel: str) -> str:
    import folder_paths

    base = os.path.abspath(folder_paths.get_input_directory())
    full = os.path.abspath(os.path.join(base, rel.replace("/", os.sep)))
    if full != base and not full.startswith(base + os.sep):
        raise TimelineError(f"素材路径不在 input 目录内，已拒绝：{rel}")
    if not os.path.isfile(full):
        raise TimelineError(f"找不到素材文件：{rel}（应在 ComfyUI 的 input 目录里）")
    return full


def _ffmpeg_bin():
    try:
        from imageio_ffmpeg import get_ffmpeg_exe

        return get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


def _load_image(path: str):
    import numpy as np
    import torch
    from PIL import Image, ImageOps

    img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    arr = np.asarray(img, dtype=np.float32) / 255.0
    return torch.from_numpy(arr).unsqueeze(0)  # [1, H, W, 3]


def _load_audio(path: str, *, max_seconds: float | None = None):
    """用 ffmpeg 解成立体声 float32 PCM，返回 ComfyUI AUDIO 字典；没有音轨返回 None。"""
    import numpy as np
    import torch

    ffmpeg = _ffmpeg_bin()
    if not ffmpeg:
        raise TimelineError("找不到 ffmpeg，无法读取音频。")
    args = [ffmpeg, "-v", "error", "-nostdin", "-i", path, "-vn", "-ac", "2", "-ar", str(_AUDIO_SR)]
    if max_seconds:
        args += ["-t", str(max_seconds)]
    args += ["-f", "f32le", "-"]
    try:
        res = subprocess.run(args, capture_output=True, check=True)
    except subprocess.CalledProcessError:
        return None
    n = len(res.stdout) // 4
    n -= n % 2
    if n < 2:
        return None
    pcm = np.frombuffer(res.stdout[: n * 4], dtype=np.float32).reshape(-1, 2).T.copy()  # [2, T]
    return {"waveform": torch.from_numpy(pcm).unsqueeze(0), "sample_rate": _AUDIO_SR}


def _load_video(path: str, long_edge: int, fps: float = 0.0):
    """读参考视频：按 fps 重采样（0 = 24），最长 15 秒（最多 360 帧），长边=long_edge（0=原样）。返回 [N,H,W,3] float32。"""
    try:
        import cv2
    except ImportError:
        raise TimelineError("缺少 opencv（cv2），无法读取参考视频。")
    import numpy as np
    import torch

    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise TimelineError(f"无法打开参考视频：{os.path.basename(path)}")
    try:
        native_fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0) or _REF_VIDEO_FPS
        native_n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        if native_n <= 0 or w <= 0 or h <= 0:
            raise TimelineError(f"参考视频信息读取失败：{os.path.basename(path)}")
        rate = float(fps) if fps and float(fps) > 0 else _REF_VIDEO_FPS
        duration = min(native_n / native_fps, _REF_VIDEO_MAX_SECONDS)
        want = max(1, min(int(duration * rate + 1e-6), int(_REF_VIDEO_MAX_SECONDS * _REF_VIDEO_FPS)))
        out_w, out_h = fit_edge_size(w, h, long_edge)
        frames = []
        last_native = -1
        last_rgb = None
        for i in range(want):
            native = min(native_n - 1, int(round(i * native_fps / rate)))
            if native != last_native:
                if native != last_native + 1:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, native)
                ok, bgr = cap.read()
                if not ok:
                    break
                if (out_w, out_h) != (w, h):
                    bgr = cv2.resize(bgr, (out_w, out_h), interpolation=cv2.INTER_AREA)
                last_rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                last_native = native
            frames.append(last_rgb)
    finally:
        cap.release()
    if len(frames) < _REF_VIDEO_MIN_FRAMES:
        raise TimelineError(
            f"参考视频太短：{os.path.basename(path)} 只读到 {len(frames)} 帧（官方要求至少 {_REF_VIDEO_MIN_FRAMES} 帧、2~15 秒）。"
        )
    arr = np.stack(frames, axis=0).astype(np.float32) / 255.0
    return torch.from_numpy(arr)


def pack_outputs(prompt, images, videos, video_audios, audios, guides=(), first=None, last=None, frame_mode=False):
    """按模式的固定顺序摆放；没用到的口放 None。"""
    pools = {"img": list(images), "vid": list(videos), "vaud": list(video_audios), "aud": list(audios), "guide": list(guides)}
    singles = {"prompt": prompt}
    if frame_mode:
        singles.update(first=first, last=last)
        return _pack(LAYOUT_FL, singles, pools)
    return _pack(LAYOUT_REF, singles, pools)


VE_NAMES = [n for n, _k, _i in ve_layout(0)]


def pack_ve(prompt, video, orig, audio, length, images, guide, n_ref):
    """guide = {"items": [{"image", "frame", "file"}, …]}：落在这一段里的所有引导点（段内帧号从 0 数）。"""
    singles = {"prompt": prompt, "video": video, "orig": orig, "audio": audio, "len": length, "guide": guide}
    return _pack(ve_layout(n_ref), singles, {"img": list(images)})


def _vlog(task, msg, level=logging.INFO):
    """视频编辑的日志：同时写到 ComfyUI 控制台和任务日志（界面「查看日志」里能看到）。"""
    log.log(level, "[H3 视频编辑] %s", msg)
    if task:
        h3t_log(task, msg, level)


# --------------------------------------------------------------------------- 节点
class H3MultiSegmentInput:
    """H3 多段输入：多段提示词 + 公共/本段参考素材（图/视频/音频）或 首帧/尾帧，按段输出。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                W_DATA: ("STRING", {"multiline": True, "default": "", "tooltip": "由节点上的编辑器自动写入，不用手动改。"}),
                W_MODE: ([MODE_REF, MODE_FL, MODE_VE], {
                    "default": MODE_REF,
                    "tooltip": "参考生视频：输出参考图/视频/音频，接 MiniMaxH3ReferenceToVideo。\n首尾帧视频：只输出首帧和尾帧，接 MiniMaxH3ImageToVideo。\n视频编辑：上传源视频，在时间轴上分段、给每段配参考图（换人）。",
                }),
                W_SEG: ("INT", {
                    "default": 0, "min": 0, "max": 9999,
                    "tooltip": "0 = 点一次运行，自动从第 1 段依次跑到最后一段；填 N = 只跑第 N 段。",
                }),
                W_EDGE: ("INT", {
                    "default": 0, "min": 0, "max": 4096, "step": 16,
                    "tooltip": "0 = 原样输出。填数值 = 视频长边正好是这个像素（等比，宽高取偶数，可放大可缩小）。越大越占内存。参考生视频：只影响参考视频；视频编辑：影响源视频片段；首尾帧模式不起作用。",
                }),
            },
            "optional": {
                W_GIN: ("STRING", {
                    "forceInput": True,
                    "tooltip": "全局提示词：参考生视频 / 首尾帧 / 视频编辑都生效。可接其他提示词节点；连接后，全局框里写的内容不再使用，改用连进来的文字。",
                }),
                W_LIN: ("STRING", {
                    "forceInput": True,
                    "tooltip": "视频编辑模式：可接其他提示词节点。连接后，局部提示词框里写的内容不再使用。文字用一行「---」分成各段，按段顺序对应；某段是空的就用全局提示词。",
                }),
                W_TASK: ("STRING", {
                    "default": "",
                    "tooltip": "由界面在「输出第几段=0」跑全部段时自动填写（时间戳），不用管。保存/读取/合并节点靠它找到同一个任务的文件；手动单段运行时保持为空。",
                }),
                W_TOEND: ("BOOLEAN", {
                    "default": False,
                    "tooltip": "打开：从「输出第几段」填的那一段开始，一直跑到最后一段（前面的段必须已在同一个任务里跑完）。\n关闭：只跑「输出第几段」那一段。\n「输出第几段」为 0 时，总是从第 1 段跑到最后，这个开关不起作用。",
                }),
                W_JOIN: (["硬切", "复制", "引导", "图片引导"], {
                    "default": "复制",
                    "tooltip": "已不使用（保留只为兼容旧工作流）。衔接方式请在「H3 潜空间前缀复制」节点上选。",
                }),
                W_HEAD: ("INT", {
                    "default": 22, "min": 5, "max": 56, "step": 1,
                    "tooltip": "已不使用（保留只为兼容旧工作流）。重叠帧数请在「H3 潜空间前缀复制」节点里设。",
                }),
                W_FPS: ("FLOAT", {
                    "default": 0, "min": 0, "max": 240, "step": 1, "round": 0.001,
                    "tooltip": "参考生视频模式读参考视频时的采样帧率。0 = 24（H3 的标准帧率，动作快慢和原视频一致）。\n填别的数：每秒从原视频里取这么多帧，H3 仍按 24 帧播放，所以动作快慢会变（比如 12 = 快 2 倍，48 = 慢一半）；总帧数最多 360。\n视频编辑模式不用这个设置（源视频固定按 24 帧读取，保证和时间线对得上）。",
                }),
            },
            "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO", "unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = tuple(t for _n, t, _k, _i in OUTPUT_LAYOUT)
    RETURN_NAMES = tuple(n for n, _t, _k, _i in OUTPUT_LAYOUT)
    FUNCTION = "run"
    CATEGORY = CATEGORY
    DESCRIPTION = (
        "多段提示词 + 公共/本段素材。参考生视频模式：输出带官方标签（<Picture N> / <Video N> / <Audio N>）的提示词，"
        "以及对应编号的参考图、参考视频、参考视频音轨、参考音频，接官方 MiniMaxH3ReferenceToVideo 同编号的输入；"
        "首尾帧视频模式：输出首帧、尾帧，接 MiniMaxH3ImageToVideo。"
        "「衔接方式」（复制/引导/硬切）接「H3 潜空间前缀复制」的「衔接方式(来自多段输入)」。"
        "「引导图片N」接官方「添加引导」的图输入。没用到的口输出 None。"
    )

    @classmethod
    def IS_CHANGED(cls, **kw):
        data, seg, edge = kw.get(W_DATA, ""), kw.get(W_SEG, 1), kw.get(W_EDGE, 0)
        mode = kw.get(W_MODE, MODE_REF)
        h = hashlib.sha1()
        h.update(str(data).encode("utf-8", "ignore"))
        h.update(f"|{seg}|{edge}|{mode}|{kw.get(W_GIN)}|{kw.get(W_LIN)}|{kw.get(W_TASK, '')}|{kw.get(W_TOEND, False)}|{kw.get(W_JOIN, '')}|{kw.get(W_HEAD, '')}|{kw.get(W_FPS, 0)}".encode("utf-8", "ignore"))
        try:
            if mode == MODE_VE:
                tl0 = parse_timeline(data)
                names = [(tl0.get("video") or {}).get("file")] + [x["file"] for sg in tl0["segments"] for x in sg["images"]] + [g["file"] for g in tl0.get("gpoints", []) if g["file"]]
                for rel in names:
                    if rel:
                        try:
                            h.update(f"|{rel}:{os.path.getmtime(_input_path(rel))}".encode())
                        except Exception:
                            h.update(f"|{rel}:missing".encode())
                return h.hexdigest()
            res = resolve_segment(parse_timeline(data), int(seg) - 1, mode, common)
            names = [f for fl in res["files"].values() for f in fl] + list(res["guides"])
            names += [f for f in (res["first"], res["last"]) if f]
            for rel in names:
                try:
                    h.update(f"|{rel}:{os.path.getmtime(_input_path(rel))}".encode())
                except Exception:
                    h.update(f"|{rel}:missing".encode())
        except Exception:
            pass
        return h.hexdigest()

    def _auto_run_all(self, kw):
        """「输出第几段=0」却没有被界面接管（前端脚本没生效）：由后端自己把各段依次排进队列，然后安静地结束当前这次运行。"""
        prompt, uid = kw.get("prompt"), str(kw.get("unique_id", ""))
        if not isinstance(prompt, dict):
            raise ValueError("[H3 多段输入] 「输出第几段」为 0，但拿不到完整的提交内容，请填具体的段号。")
        nodes, total, start = [], 0, 1
        for nid, node in prompt.items():
            inp0 = (node.get("inputs") or {}) if isinstance(node, dict) else {}
            if isinstance(node, dict) and node.get("class_type") == H3T_MS_TYPE and (
                    int(inp0.get(W_SEG, 1) or 0) == 0
                    or (bool(inp0.get(W_TOEND)) and int(inp0.get(W_SEG, 1) or 0) >= 1 and not h3t_clean_id(inp0.get(W_TASK)))):
                if int(inp0.get(W_SEG, 1) or 0) > 1:
                    start = int(inp0.get(W_SEG))
                try:
                    cnt = len(parse_timeline(node["inputs"].get(W_DATA, ""))["segments"])
                except Exception as exc:
                    raise ValueError(f"节点 #{nid} 的时间线读不出来：{exc}") from None
                nodes.append({"id": nid, "count": cnt})
                total = max(total, cnt)
        if not nodes:
            raise ValueError("找不到需要跑全部段的节点。")
        if start > 1:
            # 从第 N 段继续：沿用最近一个已经跑完前 N-1 段的任务（登记表里有这些段保存的潜空间）
            task = self._find_task_to_continue(start)
            if not task:
                raise ValueError("[H3 多段输入] 要从第 %d 段继续，但找不到已经跑完前 %d 段的任务（登记表里没有）。"
                                 "请把「输出第几段」设为 0，从头跑。" % (start, start - 1))
            log.info("[H3 多段输入] 从第 %d 段继续，沿用任务 %s，由后端依次提交第 %d~%d 段。", start, task, start, total)
        else:
            task = time.strftime("%Y%m%d_%H%M%S")
            log.info("[H3 多段输入] 由后端依次提交 %d 段，任务编号 %s。", total, task)
        h3t_log(task, f"由后端依次提交第 {start}~{total} 段。")
        from server import PromptServer
        inst = PromptServer.instance
        bases = [f"http://127.0.0.1:{getattr(inst, 'port', 8188)}"]
        data = {"output": prompt, "nodes": nodes, "task": task, "start": start, "total": total, "client_id": getattr(inst, "client_id", None),
                "workflow": (kw.get("extra_pnginfo") or {}).get("workflow")}
        res = _submit_all(bases, data)
        if not res.get("ok"):
            raise ValueError("后端自动提交各段失败：" + str(res.get("error")))
        try:
            import comfy.model_management as mm
        except Exception:
            raise ValueError(f"已自动提交 {total} 段到队列（任务 {task}）；这一次「0」的运行本身无需执行，可以忽略这条提示。") from None
        raise mm.InterruptProcessingException()  # 安静结束这一次运行，真正的各段已经在队列里

    @staticmethod
    def _find_task_to_continue(start):
        """最近一个登记表里已有第 1..start-1 段的任务；没有返回空串。"""
        try:
            d = h3t_dir("h3_tasks")
            files = sorted((f for f in _os.listdir(d) if f.endswith(".json")),
                           key=lambda f: _os.path.getmtime(_os.path.join(d, f)), reverse=True)
            for f in files:
                task = f[:-5]
                segs = (h3t_reg_load(task) or {}).get("segments") or {}
                if all(isinstance(segs.get(str(i)), dict) and (segs[str(i)].get("latent") or segs[str(i)].get("video"))
                       for i in range(1, start)):
                    return task
        except Exception:
            pass
        return ""

    @h3t_logged("H3 多段输入", task_of=lambda kw: h3t_clean_id(kw.get(W_TASK)) or (h3t_context(kw.get("prompt"), kw.get("unique_id")) or {}).get("task") or "")
    def run(self, **kw):
        task = h3t_clean_id(kw.get(W_TASK))
        seg0 = int(kw.get(W_SEG) or 0)
        if seg0 == 0 or (bool(kw.get(W_TOEND)) and seg0 >= 1 and not task):
            self._auto_run_all(kw)
        try:
            return self._run(task, **kw)
        except Exception as exc:
            if task:
                h3t_log(task, f"多段输入失败：{exc}", logging.ERROR)
            raise

    def _run(self, task, **kw):
        data, seg, edge = kw[W_DATA], int(kw[W_SEG]), int(kw[W_EDGE])
        try:
            fps_in = float(kw.get(W_FPS, 0) or 0)
        except (TypeError, ValueError):
            fps_in = 0.0
        if fps_in < 0:
            fps_in = 0.0
        mode = kw.get(W_MODE, MODE_REF)
        common = kw.get(W_GIN)
        if isinstance(common, str) and not common.strip():
            common = None
        if mode == MODE_VE:
            return self._run_ve(task, data, seg, common, edge, kw)
        if seg < 1:
            raise ValueError(
                "[H3 多段输入] 「输出第几段」为 0（跑全部段）时，需要由界面按 1、2、3… 依次提交每一段；"
                "当前没有这样提交（可能是用 API 直接提交，或前端脚本没加载）。请改填具体的段号，或用界面上的运行按钮。"
            )
        try:
            tl = parse_timeline(data)
            res = resolve_segment(tl, seg - 1, mode, common)
        except TimelineError as exc:
            raise ValueError(f"[H3 多段输入] {exc}") from None

        if task:
            n_seg = len(tl["segments"]) if isinstance(tl, dict) and "segments" in tl else 0
            h3t_reg_update(task, lambda d: d.setdefault("total", n_seg))
            if seg == 1:
                h3t_purge_stale(keep_task=task)
            h3t_log(task, f"第 {seg}/{n_seg} 段开始（衔接：{(h3t_context(kw.get('prompt'), kw.get('unique_id')) or {}).get('join') or res['join']}）")

        files = res["files"]
        try:
            guides = [_load_image(_input_path(f)) for f in res["guides"]]
            if mode == MODE_FL:
                first = _load_image(_input_path(res["first"])) if res["first"] else None
                last = _load_image(_input_path(res["last"])) if res["last"] else None
                log.info("[H3 多段输入] %s", make_report(res).splitlines()[0])
                return pack_outputs(res["prompt"], [], [], [], [], guides, first, last, True)
            images = [_load_image(_input_path(f)) for f in files["img"]]
            videos, video_audios = [], []
            for f in files["vid"]:
                p = _input_path(f)
                videos.append(_load_video(p, edge, fps_in))
                _sh = tuple(getattr(videos[-1], "shape", ()))
                h3t_log(task, f"参考视频 {f}：最大边长={edge or '0（原样）'}，采样帧率={fps_in or '0（按 24）'} → 输出{_sh[2] if len(_sh) > 2 else '?'}×{_sh[1] if len(_sh) > 1 else '?'}，{_sh[0] if _sh else '?'} 帧")
                # 参考视频自带的声音；视频没有音轨时为 None
                video_audios.append(_load_audio(p, max_seconds=_REF_VIDEO_MAX_SECONDS))
            audios = [self._require_audio(f) for f in files["aud"]]
        except TimelineError as exc:
            raise ValueError(f"[H3 多段输入] 第 {seg} 段：{exc}") from None

        log.info("[H3 多段输入] %s", make_report(res).splitlines()[0])
        return pack_outputs(res["prompt"], images, videos, video_audios, audios, guides)


    # ------------------------------------------------------------------ 视频编辑模式
    def _run_ve(self, task, data, seg, common, mp, kw=None):
        import time as _t

        t_start = _t.time()
        if seg < 1:
            raise ValueError("「输出第几段」为 0（跑全部段）时，需要由界面按 1、2、3… 依次提交每一段；"
                             "当前没有这样提交（可能是用 API 直接提交，或前端脚本没加载）。请用界面上的运行按钮，或填具体的段号。")
        try:
            tl = h3t_ve_apply(parse_timeline(data), kw, (kw or {}).get("prompt"), (kw or {}).get("unique_id"))
            h3t_ve_link_prompts(tl, data, kw)
            n_seg = len(tl["segments"])
            if seg > n_seg:
                raise TimelineError(f"「输出第几段」是 {seg}，但时间轴只有 {n_seg} 段。")
            res = resolve_segment(tl, seg - 1, MODE_VE, common)
            _vlog(task, f"━━ 第 {seg}/{n_seg} 段开始 ━━ 任务编号={task or '（无，手动单段运行）'}，参考视频最大边长={mp or '0（原样）'}")
            if task:
                h3t_reg_update(task, lambda d: d.setdefault("total", n_seg))
                if seg == 1:
                    h3t_purge_stale(keep_task=task)
            _vlog(task, f"提示词：{res['prompt'] or '（空）'}")
            images = []
            for f in res["files"]["img"]:
                img = _load_image(_input_path(f))
                images.append(img)
                _vlog(task, f"参考图 {len(images)}：{f}，尺寸 {tuple(getattr(img, 'shape', ()))}")
            if not images:
                _vlog(task, "这一段没有参考图（图片轨没有盖住这一段）", logging.WARNING)
            ve = self._ve_outputs(task, tl, seg, mp)
        except TimelineError as exc:
            raise ValueError(f"第 {seg} 段：{exc}") from None
        _vlog(task, f"第 {seg}/{n_seg} 段输出完成：源视频 {ve['len']} 帧 {ve['w']}×{ve['h']}，"
                    f"参考图 {len(images)} 张，用时 {_t.time() - t_start:.1f} 秒")
        n_ref = min(9, max([len(sg["images"]) for sg in tl["segments"]] or [0]))
        prompt_out = res["prompt"]
        return pack_ve(prompt_out, ve["video"], ve["orig"], ve["audio"], ve["len"], images, ve["guide"], n_ref)

    @staticmethod
    def _ve_plan(tl, mp):
        """读源视频信息 → (规划, 警告, 渲染宽, 渲染高, 起点秒, 总帧数, 源视频路径, 视频信息)。"""
        name = (tl.get("video") or {}).get("file")
        if not name:
            raise TimelineError("还没有上传源视频：请在节点的视频轨上传。")
        path = _input_path(name)
        info = ve_probe(path)
        start = float(tl["video"]["start"])
        dur = float(info["duration"])
        end = float(tl["video"]["end"]) or dur
        if dur > 0:
            end = min(end, dur)
        if end <= start:
            raise TimelineError(f"结束时间（{end:.2f} 秒）必须大于起始时间（{start:.2f} 秒）。")
        total = int((end - start) * H3T_VE_FPS + 0.5)
        try:
            plan, warns = h3t_ve_plan(tl, total)
        except ValueError as exc:
            raise TimelineError(str(exc)) from None
        w, h = fit_edge_size(info["width"], info["height"], mp)
        return plan, warns, w, h, start, total, path, info

    def _ve_outputs(self, task, tl, seg, mp):
        import time as _t
        import torch

        plan, warns, w, h, start, total, path, info = self._ve_plan(tl, mp)
        entry = plan[seg - 1]
        if seg == 1:
            _vlog(task, f"源视频 {tl['video']['file']}：{info['width']}×{info['height']}，{info['fps']:.2f} fps，{info['duration']:.2f} 秒；"
                        f"按 {H3T_VE_FPS} fps 共 {total} 帧；最大边长={mp or '0（原样）'} → 输出尺寸 {w}×{h}")
            _vlog(task, "分段规划（全部硬切）：" + "；".join(
                f"第{e['index'] + 1}段 {e['sampled']}帧" for e in plan) + f"；最后一段多出的 {plan[-1]['tail_cut']} 帧由「H3 视频合并」裁掉")
            for msg in warns:
                _vlog(task, msg, logging.WARNING)
        head = int(entry["head"])  # 引导/复制：第 2 段起，往前多取 head 帧（和上一段结尾重合）
        t0 = start + (entry["story_start"] - head) / float(H3T_VE_FPS)
        n = entry["sampled"]
        if head:
            _vlog(task, f"段间衔接={entry['join']}：本段往前多取 {head} 帧（源视频第 {entry['story_start'] - head} 帧起），合并时裁掉")
        _vlog(task, f"读取源视频：从 {t0:.3f} 秒起 {n} 帧，缩放并居中裁切到 {w}×{h}")
        t1 = _t.time()
        frames = ve_decode_window(path, t0, n, H3T_VE_FPS, w, h)
        if int(frames.shape[0]) != n:
            _vlog(task, f"警告：期望读到 {n} 帧，实际读到 {int(frames.shape[0])} 帧", logging.WARNING)
        image = torch.from_numpy(frames).float().div_(255.0)
        del frames
        _vlog(task, f"缩放视频读取完成：{tuple(image.shape)}，用时 {_t.time() - t1:.1f} 秒")
        ow, oh = fit_edge_size(info["width"], info["height"], 0)  # 原视频 = 不缩放（只把宽高取偶数）
        if (ow, oh) == (w, h):
            orig = image  # 没有缩放：两个口是同一份数据，不多占内存
            _vlog(task, f"原视频：和缩放视频尺寸相同（{ow}×{oh}），共用同一份数据")
        else:
            t2 = _t.time()
            frames = ve_decode_window(path, t0, n, H3T_VE_FPS, ow, oh)
            orig = torch.from_numpy(frames).float().div_(255.0)
            del frames
            _vlog(task, f"原视频读取完成：{tuple(orig.shape)}，用时 {_t.time() - t2:.1f} 秒")
        wav, sr, note = ve_audio_window(path, t0, n / float(H3T_VE_FPS))
        audio = {"waveform": torch.from_numpy(wav).unsqueeze(0), "sample_rate": sr}
        _vlog(task, f"音频窗口：{n / float(H3T_VE_FPS):.2f} 秒，{sr} Hz，{'' if note == 'ok' else note}")
        if task:
            h3t_reg_update(task, lambda d: d.update({"ve": {
                "plan": plan, "total": total, "video": tl["video"]["file"], "start": start,
                "width": w, "height": h, "fps": H3T_VE_FPS,
                "audio": h3t_ve_settings(tl)["audio"]}}))
        guides = self._ve_guides(task, tl, plan, seg, total)
        return {"video": image, "orig": orig, "audio": audio, "len": n, "w": w, "h": h, "guide": guides,
                "join": "硬切" if seg == 1 or entry["join"] not in ("复制", "引导") else entry["join"],
                "head": head, "join_kind": entry["join"]}

    @staticmethod
    def _ve_prev_tail(task, plan, seg):
        """段间衔接=图片引导：读上一段已保存的片段，取最后 head 帧，作为这一段开头的引导（整批一个引导项，帧号 0）。"""
        import torch

        e = plan[seg - 1]
        head = int(e["head"])
        if seg == 1 or e["join"] != "图片引导" or head <= 0:
            return None
        if not task:
            _vlog(task, "段间衔接=图片引导：没有任务编号（手动单段运行），读不到上一段的结果，这一段不加衔接引导。", logging.WARNING)
            return None
        reg = h3t_reg_load(task) or {}
        prev = (reg.get("segments") or {}).get(str(seg - 1)) or {}
        rel = prev.get("video")
        if not rel or not os.path.isfile(h3t_abs(rel)):
            raise TimelineError(f"段间衔接=图片引导：找不到第 {seg - 1} 段已保存的片段（要先跑完并保存第 {seg - 1} 段，且「H3 视频合并」要接在后面）。")
        path = h3t_abs(rel)
        info = ve_probe(path)
        pw, ph = fit_edge_size(info["width"], info["height"], 0)
        t0 = (int(plan[seg - 2]["delivered"]) - head) / float(H3T_VE_FPS)
        frames = ve_decode_window(path, t0, head, H3T_VE_FPS, pw, ph)
        if int(frames.shape[0]) != head:
            raise TimelineError(f"段间衔接=图片引导：从第 {seg - 1} 段片段里读到 {int(frames.shape[0])} 帧，需要 {head} 帧。")
        img = torch.from_numpy(frames).float().div_(255.0)
        _vlog(task, f"段间衔接=图片引导：取第 {seg - 1} 段最后 {head} 帧（{pw}×{ph}）作为本段第 0 帧起的引导")
        return {"image": img, "frame": 0, "file": f"（第 {seg - 1} 段最后 {head} 帧）"}

    @staticmethod
    def _ve_guides(task, tl, plan, seg, total):
        """引导点（整条视频第 f 帧）落在哪一段：只把落在当前这一段的点装进「引导」输出（图 + 段内帧号，从 0 数）。"""
        items, used = [], set()
        tail = H3MultiSegmentInput._ve_prev_tail(task, plan, seg)
        if tail is not None:
            items.append(tail)
            used.add(0)
        for k, g in enumerate(tl.get("gpoints", []), 1):
            f, file = int(g["f"]), g["file"]
            if not file:
                continue
            if f >= total:
                _vlog(task, f"引导点 {k}：帧号 {f} 超出视频总帧数 {total}，已忽略。", logging.WARNING)
                continue
            hit = next((e for e in plan if e["story_start"] <= f < e["story_end"]), None)
            if hit is None or hit["index"] != seg - 1:
                continue
            local = f - hit["story_start"] + int(hit["head"])  # 本段采样是从「起点 - 重叠帧数」开始的
            if local in used:
                _vlog(task, f"引导点 {k}：本段第 {local} 帧已经有引导图了，这一个已忽略。", logging.WARNING)
                continue
            used.add(local)
            img = _load_image(_input_path(file))
            _vlog(task, f"引导点 {k}：{file}，尺寸 {tuple(getattr(img, 'shape', ()))}，整条视频第 {f} 帧 = 本段第 {local} 帧（从 0 数）")
            items.append({"image": img, "frame": local, "file": file})
        items.sort(key=lambda x: x["frame"])
        if not items:
            _vlog(task, "这一段没有引导点。")
        return {"items": items}

    @staticmethod
    def _require_audio(rel: str):
        audio = _load_audio(_input_path(rel))
        if audio is None:
            raise TimelineError(f"无法解码参考音频：{rel}")
        return audio


_POLL_SEC = 2.0   # 监视线程查询间隔（测试里会调小）
_GONE_WAIT = 1.0


def _http_json(url, body=None, timeout=15):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "ignore")
    return json.loads(raw) if raw.strip() else {}


def _queue_ids(base):
    q = _http_json(base + "/queue")
    return {str(it[1]) for key in ("queue_running", "queue_pending") for it in (q.get(key) or []) if isinstance(it, (list, tuple)) and len(it) > 1}


def _drop_queued(base, ids, task):
    ids = [i for i in ids if i]
    if not ids:
        return
    try:
        _http_json(base + "/queue", {"delete": ids})
        h3t_log(task, f"已把还没跑的 {len(ids)} 段从队列里删除。")
    except Exception as exc:
        h3t_log(task, f"删除队列里剩下的段失败：{exc}（请手动在队列里删除）", logging.WARNING)


def _error_text(entry):
    for kind, info in ((entry.get("status") or {}).get("messages") or []):
        if kind == "execution_error" and isinstance(info, dict):
            return f"节点 {info.get('node_type', '?')}（#{info.get('node_id', '?')}）：{info.get('exception_message', '').strip()}"
        if kind == "execution_interrupted":
            return "被手动停止"
    return "未知原因"


def _watch_task(base, task, jobs):
    """jobs = [(段号, prompt_id)]。盯着队列：某一段出错/被停止/被取消，就把后面没跑的段删掉并写日志。"""
    try:
        todo = list(jobs)
        while todo:
            seg, pid = todo[0]
            entry = None
            try:
                entry = _http_json(f"{base}/history/{pid}").get(pid)
            except Exception:
                pass
            if entry:
                status = (entry.get("status") or {}).get("status_str")
                if status == "success":
                    h3t_log(task, f"第 {seg} 段运行完成。")
                    todo.pop(0)
                    continue
                h3t_log(task, f"第 {seg} 段失败：{_error_text(entry)}。日志：{h3t_log_path(task)}", logging.ERROR)
                _drop_queued(base, [p for _s, p in todo[1:]], task)
                h3t_log(task, f"任务已停止，已完成 {seg - 1}/{jobs[-1][0]} 段；下次把「输出第几段」设为 {seg}、打开「到最后一段」，可从第 {seg} 段继续。", logging.WARNING)
                return
            gone = 0
            for _ in range(4):  # 刚跑完到写入历史记录之间有一瞬间两边都查不到，多确认几次
                try:
                    if pid in _queue_ids(base):
                        gone = 0
                        break
                except Exception:
                    gone = 0
                    break
                gone += 1
                time.sleep(_GONE_WAIT)
                try:
                    if _http_json(f"{base}/history/{pid}").get(pid):
                        gone = 0
                        break
                except Exception:
                    pass
            if gone >= 4:
                h3t_log(task, f"第 {seg} 段已不在队列里，也没有运行记录（被取消了？）。", logging.WARNING)
                _drop_queued(base, [p for _s, p in todo[1:]], task)
                return
            time.sleep(_POLL_SEC)
        h3t_log(task, "全部段都已运行完成。")
    except Exception as exc:  # 监视线程自己不能崩得无声无息
        h3t_log(task, f"监视线程异常：{exc}", logging.ERROR)


def _submit_all(bases, data):
    """把各段依次放进 ComfyUI 队列（走它自己的 /prompt 接口）。返回要给前端的 dict。"""
    output = data.get("output")
    nodes = data.get("nodes") or []
    task = h3t_clean_id(data.get("task"))
    start, total = int(data.get("start", 1)), int(data.get("total", 0))
    if not isinstance(output, dict) or not task or total < 1 or not nodes or not (1 <= start <= total):
        return {"ok": False, "error": "提交参数不完整（缺少任务编号、段数或节点）。"}
    for n in nodes:
        node = output.get(str(n.get("id")))
        if not isinstance(node, dict) or node.get("class_type") != H3T_MS_TYPE:
            return {"ok": False, "error": f"提交内容里找不到「H3 多段输入」节点 #{n.get('id')}。"}
    client_id = data.get("client_id")
    extra = {"extra_pnginfo": {"workflow": data["workflow"]}} if data.get("workflow") is not None else {}
    jobs, base_ok = [], None
    h3t_log(task, f"开始提交第 {start}~{total} 段（共 {total} 段）。")
    for seg in range(start, total + 1):
        prompt = copy.deepcopy(output)
        for n in nodes:
            inp = prompt[str(n["id"])].setdefault("inputs", {})
            inp[W_SEG] = min(seg, int(n.get("count", total)))
            inp[W_TASK] = task
        body = {"prompt": prompt, "extra_data": extra}
        if client_id:
            body["client_id"] = client_id
        resp, last_err = None, None
        for base in ([base_ok] if base_ok else list(bases)):
            try:
                resp = _http_json(base + "/prompt", body, timeout=60)
                base_ok = base
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "ignore")
                try:
                    info = json.loads(detail)
                    msg = (info.get("error") or {}).get("message") or detail[:300]
                    node_errors = info.get("node_errors") or {}
                    if node_errors:
                        msg += "；" + "；".join(f"节点 #{k}：" + "、".join(e.get("message", "") for e in v.get("errors", [])) for k, v in list(node_errors.items())[:3])
                except Exception:
                    msg = detail[:300]
                last_err = f"ComfyUI 拒绝了第 {seg} 段：{msg}"
                base_ok = base
                break
            except Exception as exc:
                last_err = f"连不上 ComfyUI 的 /prompt 接口（{base}）：{exc}"
        if resp is None or not resp.get("prompt_id"):
            h3t_log(task, f"提交第 {seg} 段失败：{last_err}", logging.ERROR)
            _drop_queued(base_ok or bases[0], [p for _s, p in jobs], task)
            return {"ok": False, "error": last_err or "提交失败", "failed_seg": seg, "need_fallback": base_ok is None}
        jobs.append((seg, str(resp["prompt_id"])))
        h3t_log(task, f"第 {seg}/{total} 段已排入队列。")
    threading.Thread(target=_watch_task, args=(base_ok, task, jobs), daemon=True, name=f"h3-watch-{task}").start()
    h3t_prune_logs(protect=task)
    return {"ok": True, "task": task, "ids": [p for _s, p in jobs], "segs": [s for s, _p in jobs], "log": h3t_log_path(task)}


def _register_routes():
    try:
        from aiohttp import web
        from server import PromptServer
    except Exception:
        return False
    inst = getattr(PromptServer, "instance", None)
    if inst is None or getattr(inst, "_h3_routes", False):
        return False
    routes = inst.routes

    @routes.post("/h3/run_all")
    async def h3_run_all(request):
        try:
            data = await request.json()
            bases = []
            port = getattr(inst, "port", None)
            if port:
                bases.append(f"http://127.0.0.1:{port}")
            bases.append(f"{request.scheme}://{request.host}")
            res = await asyncio.get_running_loop().run_in_executor(None, _submit_all, bases, data)
        except Exception as exc:
            log.exception("[H3 多段输入] /h3/run_all 失败")
            res = {"ok": False, "error": f"后端接口异常：{exc}", "need_fallback": True}
        return web.json_response(res)

    @routes.post("/h3/log")
    async def h3_log(request):
        try:
            data = await request.json()
            level = {"error": logging.ERROR, "warn": logging.WARNING}.get(data.get("level"), logging.INFO)
            h3t_log(h3t_clean_id(data.get("task")), "[界面] " + str(data.get("msg", ""))[:2000], level)
        except Exception:
            pass
        return web.json_response({"ok": True})

    inst._h3_routes = True
    return True


try:
    _register_routes()
except Exception:  # 接口注册失败不能影响节点本身
    log.exception("[H3 多段输入] 后端接口注册失败，界面会改用一次性提交各段。")


# --------------------------------------------------------------------------- 配套节点：批量添加引导
def _official_add_guide():
    import nodes as _comfy_nodes

    cls = getattr(_comfy_nodes, "NODE_CLASS_MAPPINGS", {}).get("MiniMaxH3AddGuide")
    if cls is None:
        raise ValueError("找不到官方节点 MiniMaxH3AddGuide（「为 MiniMax H3 添加引导」）：请确认 ComfyUI 版本带有 MiniMax H3 的官方节点。")
    return cls


def _guide_kwargs(cls, positive, latent, vae, image, frame):
    """按官方节点实际的输入名摆参数（输入名可能随版本变化，所以先读它的 INPUT_TYPES）。"""
    t = cls.INPUT_TYPES()
    keys = list((t.get("required") or {})) + list((t.get("optional") or {}))

    def find(*cands):
        for c in cands:
            for k in keys:
                if k == c:
                    return k
        for c in cands:
            for k in keys:
                if c in str(k).lower():
                    return k
        return None

    mp = {"positive": find("positive", "正向"), "latent": find("latent", "潜空间"), "vae": find("vae"),
          "image": find("image", "图像"), "frame": find("frame_idx", "frame")}
    miss = [k for k, v in mp.items() if v is None]
    if miss:
        raise ValueError(f"官方「添加引导」节点的输入名和我预期的不一致，缺少 {miss}；它现在的输入是 {keys}。请把这行发给我。")
    return {mp["positive"]: positive, mp["latent"]: latent, mp["vae"]: vae, mp["image"]: image, mp["frame"]: int(frame)}


async def _call_node(cls, kwargs):
    """像 ComfyUI 一样调用节点：实例化，调 FUNCTION，兼容旧式（tuple）和新式（NodeOutput）返回值，也兼容异步。"""
    import inspect

    inst = cls()
    out = getattr(inst, cls.FUNCTION)(**kwargs)
    if inspect.isawaitable(out):
        out = await out
    if isinstance(out, dict) and "result" in out:
        out = out["result"]
    elif hasattr(out, "result") and not isinstance(out, (tuple, list)):
        out = out.result
    if isinstance(out, (tuple, list)):
        return out[0]
    return out


def _fit_cover(img, w, h):
    """把图像缩放到 w×h：保持比例铺满，多出来的部分居中裁掉。img 形状 [B,H,W,C]。"""
    import torch.nn.functional as F

    b, ih, iw, c = img.shape
    if (iw, ih) == (w, h):
        return img
    k = max(w / iw, h / ih)
    nw, nh = max(w, int(round(iw * k))), max(h, int(round(ih * k)))
    x = F.interpolate(img.permute(0, 3, 1, 2), size=(nh, nw), mode="bilinear", align_corners=False)
    y0, x0 = (nh - h) // 2, (nw - w) // 2
    return x[:, :, y0:y0 + h, x0:x0 + w].permute(0, 2, 3, 1).contiguous()


class H3BatchAddGuide:
    """H3 批量添加引导：把「H3 多段输入」的「引导」口里这一段所有的引导图，依次加进条件。整张工作流放一个，所有段共用；这一段没有引导点就原样放行。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "positive": ("CONDITIONING", {"tooltip": "官方「参考生视频」节点输出的条件。"}),
                "latent": ("LATENT", {"tooltip": "和采样用的同一个 latent（定义视频的帧数和尺寸）。"}),
                "vae": ("VAE", {}),
            },
            "optional": {
                "引导": ("H3_GUIDES", {"tooltip": "接「H3 多段输入」最后一个输出口「引导」。没接或这一段没有引导点就原样放行。"}),
                "宽度": ("INT", {"default": 0, "min": 0, "max": 8192, "step": 2, "tooltip": "0 = 自动按 latent 的尺寸（高宽×16）。填了宽高，就按填的尺寸。引导图和参考视频都保持比例铺满后居中裁切到这个尺寸。"}),
                "高度": ("INT", {"default": 0, "min": 0, "max": 8192, "step": 2, "tooltip": "同上。"}),
                "参考视频": ("IMAGE", {"tooltip": "接「H3 多段输入」输出的「缩放视频」（开了遮罩时接遮罩那路缩放节点输出的图像）。会作为整段视频引导加进条件：第 1 段、硬切的段从第 0 帧起；复制/图片引导的段，自动去掉开头和上一段重合的那几帧，从重合帧之后开始（这几帧是上一段结尾，不需要引导）。不接就不加。"}),
                "遮罩": ("MASK", {"tooltip": "（可选，开遮罩换人时用）接和参考视频同一个缩放节点输出的逐帧像素遮罩，人物是白的。接了就先把参考视频里白色区域涂成灰色，再作为整段引导，避免模型照着参考视频把原来的人画回去。只作用于参考视频，不影响引导图。不接 = 和以前一样。"}),
            },
            "hidden": {"prompt": "PROMPT", "unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("CONDITIONING",)
    RETURN_NAMES = ("positive",)
    FUNCTION = "run"
    CATEGORY = CATEGORY
    DESCRIPTION = "把这一段所有的引导点依次加进官方「为 MiniMax H3 添加引导」。没有引导点时什么也不做，所以每一段都可以共用同一个节点。"

    @staticmethod
    def _latent_frames(latent):
        """采样用 latent 对应的视频帧数（17k+5）；读不出来返回 None。"""
        try:
            s = latent["samples"]
            v = s.tensors[0] if hasattr(s, "tensors") else s
            t = int(v.shape[2])
            return (t - 2) // 5 * 17 + 5
        except Exception:
            return None

    @staticmethod
    def _latent_size(latent):
        """采样用 latent 对应的画面宽高（潜空间高宽×16）；读不出来返回 None。"""
        try:
            s = latent["samples"]
            v = s.tensors[0] if hasattr(s, "tensors") else s
            return int(v.shape[-1]) * 16, int(v.shape[-2]) * 16
        except Exception:
            return None

    @staticmethod
    def _gray_out(frames, mask, head, n, n_all, tk):
        """用遮罩把参考视频里人物（白色）区域涂成灰色。frames 已裁成 [n,H,W,C]；mask 是整段 [M,h,w] 或单帧。"""
        import torch

        m = mask
        if m.ndim == 2:
            m = m.unsqueeze(0)
        if m.ndim == 4:  # [B,H,W,1] 之类
            m = m[..., 0] if m.shape[-1] == 1 else m[:, 0]
        mc = int(m.shape[0])
        if mc == 1:
            m = m.expand(n, -1, -1)
        elif mc == n_all:
            m = m[head:head + n]
        elif mc == n:
            pass
        else:
            raise ValueError(f"[H3 批量添加引导] 遮罩有 {mc} 帧，参考视频有 {n_all} 帧，对不上。"
                             "请把遮罩和参考视频接到同一个缩放节点的两个输出上。")
        fh, fw = int(frames.shape[1]), int(frames.shape[2])
        if tuple(m.shape[1:3]) != (fh, fw):
            m = _fit_cover(m.unsqueeze(-1).to(frames.dtype), fw, fh)[..., 0]
            h3t_log(tk, f"    遮罩尺寸和参考视频不同，已按同样方式铺满裁切到 {fw}×{fh}")
        m = (m.to(device=frames.device, dtype=frames.dtype) > 0.5).to(frames.dtype).unsqueeze(-1)
        cover = float(m.mean()) * 100.0
        frames = frames * (1.0 - m) + 0.5 * m
        h3t_log(tk, f"    已用遮罩把参考视频里人物区域涂成灰色（平均覆盖画面 {cover:.1f}%）")
        if cover < 0.5:
            h3t_log(tk, "    ⚠ 遮罩几乎是空的，参考视频里原来的人基本没被抹掉，开遮罩时人物可能换不掉。", logging.WARNING)
        return torch.clamp(frames, 0.0, 1.0)

    async def _add_clip(self, cls, positive, latent, vae, video, w, h, ctx, prompt, tk, mask=None):
        """参考视频作为整段视频引导：开头和上一段重合的 head 帧不引导（那是上一段结尾，已锁定），从第 head 帧起放。"""
        n_all = int(video.shape[0])
        head = int(h3t_overlap(prompt, 22, ctx.get("ms_id"))) if (ctx and not ctx["hard_start"]) else 0
        total = self._latent_frames(latent)
        h3t_log(tk, f"  参考视频：共 {n_all} 帧 {tuple(video.shape[1:3])}；本段{'有' if head else '没有'}重合帧 head={head}（{'复制/引导/图片引导的段，开头这些帧是上一段结尾' if head else '第 1 段、硬切或手动运行，不去帧'}）；本段采样共 {total} 帧")
        frames = video[head:]
        if total is not None:
            frames = frames[:max(0, total - head)]
        n = int(frames.shape[0])
        if n >= 5:
            n = 17 * ((n - 5) // 17) + 5  # 官方节点要求 17k+5 帧
            frames = frames[:n]
        if n < 5:
            h3t_log(tk, f"  参考视频去掉开头 {head} 帧后只剩 {n} 帧（不足 5 帧），不加整段视频引导。", logging.WARNING)
            return positive
        if w > 0 and h > 0:
            frames = _fit_cover(frames, int(w), int(h))
        if mask is not None:
            frames = self._gray_out(frames, mask, head, n, n_all, tk)
        h3t_log(tk, f"  整段视频引导：取参考视频第 {head} 到 {head + n - 1} 帧（共 {n} 帧），放在本段第 {head} 帧；去掉的开头 {head} 帧和结尾 {n_all - head - n} 帧不引导")
        kw = _guide_kwargs(cls, positive, latent, vae, frames, head)
        try:
            positive = await _call_node(cls, kw)
        except Exception as exc:
            raise ValueError(f"[H3 批量添加引导] 添加整段视频引导（第 {head} 帧起，{n} 帧）失败：{exc}") from exc
        h3t_log(tk, f"    添加成功，现在条件锚点位置：{h3t_anchors(positive)}")
        return positive

    @h3t_logged("H3 批量添加引导")
    async def run(self, positive, latent, vae, 引导=None, 宽度=0, 高度=0, 参考视频=None, 遮罩=None, prompt=None, unique_id=None):
        items = (引导 or {}).get("items") if isinstance(引导, dict) else None
        if 引导 is not None and not isinstance(引导, dict):
            raise ValueError("[H3 批量添加引导]「引导」口接错了：请接「H3 多段输入」最后一个输出口「引导」。")
        if not items and 参考视频 is None:
            return (positive,)
        items = items or []
        cls = _official_add_guide()
        _ctx = h3t_context(prompt, unique_id)
        _tk = (_ctx or {}).get("task", "")
        if not (宽度 > 0 and 高度 > 0):
            sz = self._latent_size(latent)
            if sz:
                宽度, 高度 = sz
                h3t_log(_tk, f"  宽高填 0：自动按 latent 尺寸 {宽度}×{高度} 裁切引导图和参考视频")
            else:
                宽度 = 高度 = 0
        if 遮罩 is not None and 参考视频 is None:
            h3t_log(_tk, "  接了「遮罩」但没接「参考视频」：遮罩只作用于参考视频，这次不起作用。", logging.WARNING)
        if 参考视频 is not None:
            positive = await self._add_clip(cls, positive, latent, vae, 参考视频, 宽度, 高度, _ctx, prompt, _tk, mask=遮罩)
        h3t_log(_tk, f"  引导共 {len(items)} 个；最大尺寸设置 宽={宽度} 高={高度}（0 表示不缩放）；进来的条件锚点位置：{h3t_anchors(positive)}；潜空间：{h3t_brief(latent)}")
        for it in items:
            img = it["image"]
            h3t_log(_tk, f"  处理引导：第 {it['frame']} 帧，文件={it.get('file', '')}，原图 {h3t_brief(img)}")
            if 宽度 > 0 and 高度 > 0:
                img = _fit_cover(img, int(宽度), int(高度))
                h3t_log(_tk, f"    已按填满方式裁成 {int(宽度)}×{int(高度)}：{h3t_brief(img)}")
            kw = _guide_kwargs(cls, positive, latent, vae, img, it["frame"])
            try:
                positive = await _call_node(cls, kw)
            except Exception as exc:
                raise ValueError(f"[H3 批量添加引导] 添加第 {it['frame']} 帧的引导（{it.get('file', '')}）失败：{exc}") from exc
            log.info("[H3 批量添加引导] 已在本段第 %s 帧添加引导图 %s", it["frame"], it.get("file", ""))
            h3t_log(_tk, f"    添加成功，现在条件锚点位置：{h3t_anchors(positive)}")
        return (positive,)


NODE_CLASS_MAPPINGS = {"H3MultiSegmentInput": H3MultiSegmentInput, "H3BatchAddGuide": H3BatchAddGuide}
NODE_DISPLAY_NAME_MAPPINGS = {"H3MultiSegmentInput": "H3 多段输入", "H3BatchAddGuide": "H3 批量添加引导"}
