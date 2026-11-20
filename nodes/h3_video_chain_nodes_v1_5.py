# -*- coding: utf-8 -*-
# H3 视频续接工具 v1.5
# 本文件适配 ComfyUI-Prompt 的自动加载框架。
# 音频时间锚定算法参考并改编自 NikoDemon80/ComfyUI-H3-Motion-Context，
# Copyright (C) 2026 NikoDemon80，原项目采用 GNU GPL v3.0。
# 本文件是经过修改的独立实现，修改日期：2026-11-06。
# 「引导」部分改编自 ComfyUI_MiniMaxH3_Director（Apache License 2.0）。
# 本文件整体按 GNU GPL v3.0 发布，见同目录 LICENSE-GPL-3.0.txt。

"""MiniMax H3 AV latent continuation nodes for the ComfyUI-Prompt auto-loader.

Nodes:
- H3 潜空间前缀复制: hard-copy the previous video latent prefix, attach the
  native video noise mask, and add the previous audio tail as an H3 timeline
  audio keyframe in Conditioning.
- H3 潜空间保存 / 读取: save/load paired H3 video+audio tensors.
- H3 续接重叠帧修剪: trim duplicated decoded video frames and matching audio,
  optionally matching the audio tail exactly to the remaining video duration.

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


def _audio_tail_and_anchor(source_video, source_audio, video_overlap_frames, audio_context_frames):
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

    frames_for_audio = int(audio_context_frames) or int(video_overlap_frames)
    audio_steps = int(round(frames_for_audio / _H3_FPS * _H3_AUDIO_HZ))
    if audio_steps > audio_steps_total:
        _LOG.warning(
            "音频上下文请求 %d 个潜空间位置，但上一段只有 %d 个；将使用全部音频。",
            audio_steps, audio_steps_total
        )
        audio_steps = audio_steps_total
    if audio_steps < 1:
        raise ValueError("音频上下文长度太短，换算后没有可用的音频潜空间位置。")

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


def _append_audio_keyframe_to_conditioning(conditioning, audio_keyframe, overlap_frames, target_video):
    """Append the audio continuation keyframe and avoid conflicting anchors in locked video prefix."""
    if not isinstance(conditioning, (list, tuple)) or not conditioning:
        raise ValueError("条件输入不是有效的 ComfyUI CONDITIONING，请连接原始 H3 文本/参考条件。")

    target_frame_count = _h3_pixel_frames(target_video.shape[2])
    output = []
    dropped = []
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
        _LOG.warning(
            "已移除 %d 个落在硬锁前缀 0..%d 内的冲突参考锚点：%s。",
            len(dropped), overlap_frames - 1, sorted(set(dropped))
        )
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
        raise ValueError("音频尾部放不进下一段的音频潜空间，请检查重叠帧数和音频上下文长度。")
    return src_from, start, end


class MiniMaxH3LatentPrefixCopy:
    """把上一段末尾接到下一段开头。衔接方式：复制（硬锁）或 引导（软遮罩，可逐步收紧）。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "上一段原始潜空间": (
                    "LATENT",
                    {"tooltip": "上一段由 H3 潜空间保存节点保存的完整 AV 潜空间。第一段可连接读取节点（编号 0）。"},
                ),
                "下一段初始潜空间": (
                    "LATENT",
                    {"tooltip": "下一段 H3 图生视频/参考生视频节点输出的初始 AV 潜空间。"},
                ),
                "重叠帧数": (
                    "INT",
                    {"default": 22, "min": 5, "max": 362, "step": 17,
                     "tooltip": "H3 合法视频重叠长度：5、22、39、56……；建议先用 22。"},
                ),
                "conditioning": (
                    "CONDITIONING",
                    {"tooltip": "连接原始 H3 文本/参考条件。音频衔接选「关键帧」时会在其中加入音频锚点。"},
                ),
                "音频上下文长度": (
                    "INT",
                    {"default": 18, "min": 0, "max": 240, "step": 1,
                     "tooltip": "从上一段末尾取多少帧等效时长的音频潜空间。推荐 18；0 表示跟随重叠帧数。音频自动对齐视频衔接点。"},
                ),
                "衔接方式": (
                    [MODE_COPY, MODE_GUIDE, MODE_CUT],
                    {"default": MODE_COPY,
                     "tooltip": "复制：把上一段末尾原样锁死。\n引导：把上一段末尾作为起点，接缝处允许重新绘制，过渡更自然（可能比复制多一点变化）。\n硬切：不接上一段，这一段当作全新的镜头（视频、音频都不衔接）。\n如果连接了「衔接方式(来自多段输入)」，以连接的值为准。"},
                ),
                "音频衔接": (
                    [AUDIO_COPY, AUDIO_KEYFRAME],
                    {"default": AUDIO_COPY,
                     "tooltip": "复制：把上一段音频尾部直接写进下一段音频潜空间并按衔接方式锁定（需要采样器支持音频遮罩）。\n关键帧：旧做法，只给条件加一个音频锚点，不改音频潜空间。"},
                ),
                "重绘幅度": (
                    "FLOAT",
                    {"default": 0.10, "min": 0.0, "max": 0.95, "step": 0.01,
                     "tooltip": "仅「引导」有效。接缝处最少允许重画多少：0 = 完全锁死，越大越自由。"},
                ),
            },
            "optional": {
                "模型": (
                    "MODEL",
                    {"tooltip": "仅「引导」需要：接入后采样时逐步收紧前缀。把输出的 model 接给采样器。不接则只用静态软遮罩。"},
                ),
                "sigmas": (
                    "SIGMAS",
                    {"tooltip": "可选：采样用的 sigmas，用于更准确的逐步收紧。不接也能工作。"},
                ),
                "衔接方式(来自多段输入)": (
                    "STRING",
                    {"forceInput": True,
                     "tooltip": "接「H3 多段输入」的「衔接方式」输出。连接后每一段按各自设置的方式衔接，覆盖本节点的「衔接方式」下拉框。"},
                ),
            },
        }

    RETURN_TYPES = ("LATENT", "CONDITIONING", "MODEL")
    RETURN_NAMES = ("latent", "conditioning", "model")
    FUNCTION = "copy_prefix_and_mask"
    CATEGORY = CATEGORY

    def copy_prefix_and_mask(self, **kwargs):
        source_latent = kwargs["上一段原始潜空间"]
        target_latent = kwargs["下一段初始潜空间"]
        conditioning = kwargs["conditioning"]
        overlap_frames = int(kwargs["重叠帧数"])
        audio_context_frames = int(kwargs["音频上下文长度"])
        mode = kwargs.get("衔接方式", MODE_COPY)
        override = kwargs.get("衔接方式(来自多段输入)")
        if isinstance(override, str) and override.strip():
            override = override.strip()
            if override not in _MODES:
                raise ValueError(f"收到的衔接方式「{override}」无效，应为：{'、'.join(_MODES)}。")
            mode = override
        if mode not in _MODES:
            raise ValueError(f"衔接方式「{mode}」无效，应为：{'、'.join(_MODES)}。")
        audio_mode = kwargs.get("音频衔接", AUDIO_COPY)
        seam_min = _clamp_seam(kwargs.get("重绘幅度", 0.10))
        model = kwargs.get("模型")
        sigmas = kwargs.get("sigmas")
        guide = mode == MODE_GUIDE

        # 第一段（读取编号 0 得到 None），或这一段选了「硬切」：不衔接，原样放行。
        if mode == MODE_CUT:
            _split_h3_latent(target_latent, "下一段初始潜空间")
            _LOG.info("H3 续接：硬切，本段不接上一段。")
            return (target_latent, conditioning, model)
        if source_latent is None:
            _split_h3_latent(target_latent, "下一段初始潜空间")
            return (target_latent, conditioning, model)

        source_video, source_audio = _split_h3_latent(source_latent, "上一段原始潜空间")
        target_video, target_audio = _split_h3_latent(target_latent, "下一段初始潜空间")
        n_tokens = _tokens_for_h3_frames(overlap_frames)

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
        video_mask = torch.ones(
            (target_video.shape[0], 1, target_video.shape[2], target_video.shape[3], target_video.shape[4]),
            dtype=torch.float32, device=target_video.device,
        )
        if guide:
            weights = torch.tensor(_prefix_token_weights(n_tokens, seam_min), dtype=torch.float32,
                                   device=target_video.device)
            video_mask[:, :, :n_tokens] = weights.view(1, 1, n_tokens, 1, 1)
        else:
            video_mask[:, :, :n_tokens] = 0.0

        # ---- 音频 ----
        _ensure_h3_audio_layout_contract()
        audio_tail, audio_steps, audio_index, audio_end_frame, audio_overhang = _audio_tail_and_anchor(
            source_video, source_audio, overlap_frames, audio_context_frames
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
            values = _audio_mask_values(
                a_len, dst_from, dst_to, _AUDIO_SOFT_RELEASE_TICKS if guide else 0
            )
            audio_mask = torch.tensor(values, dtype=torch.float32, device=target_audio.device).view(1, 1, 1, a_len)
            output_latent["samples"] = comfy.nested_tensor.NestedTensor((copied_video, new_audio))
            output_latent["noise_mask"] = comfy.nested_tensor.NestedTensor((video_mask, audio_mask))
            audio_note = f"音频直接复制：写入 {dst_from}..{dst_to} 共 {dst_to - dst_from} 个位置"
        else:
            audio_keyframe = {"resolved_frame_index": audio_index, "audio_latent": audio_tail}
            output_conditioning = _append_audio_keyframe_to_conditioning(
                conditioning, audio_keyframe, overlap_frames, target_video
            )
            output_latent["samples"] = comfy.nested_tensor.NestedTensor((copied_video, target_audio))
            output_latent["noise_mask"] = video_mask
            audio_note = f"音频关键帧：锚点 {audio_index:.4f}"

        # ---- 引导：采样时逐步收紧 ----
        out_model = model
        if guide:
            if model is not None:
                out_model = _install_prefix_remask(model, n_tokens, sigmas, tuple(target_video.shape), seam_min)
            else:
                _LOG.warning("引导：没有接入模型，只使用静态软遮罩（接入「模型」可获得逐步收紧）。")

        _LOG.info(
            "H3 续接：%s，重叠 %d 帧（%d 个位置）%s；%s。",
            mode, overlap_frames, n_tokens,
            f"，重绘幅度 {seam_min:.2f}" if guide else "", audio_note,
        )
        return (output_latent, output_conditioning, out_model)


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
                        "default": 1,
                        "min": 1,
                        "max": 9999,
                        "step": 1,
                        "control_after_generate": "fixed",
                        "tooltip": "第一段从 1 开始。右侧控制方式选‘递增’，每次生成后自动加 1；选‘固定’则保持当前编号。",
                    },
                ),
            }
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("保存路径",)
    FUNCTION = "save_latent"
    OUTPUT_NODE = True
    CATEGORY = CATEGORY

    def save_latent(self, **kwargs):
        if _st_save is None:
            raise RuntimeError("当前 Python 环境没有 safetensors，无法保存 H3 潜空间。")
        latent = kwargs["latent"]
        filename_prefix = kwargs["保存文件名前缀"]
        clip_index = int(kwargs["保存编号"])
        if clip_index < 1:
            raise ValueError("保存片段编号必须从 1 开始。")

        video, audio = _split_h3_latent(latent, "待保存的潜空间")
        if video.shape[0] != audio.shape[0]:
            raise ValueError("H3 视频与音频潜空间的 batch_size 不一致。")

        video_cpu = video.detach().to(device="cpu").contiguous()
        audio_cpu = audio.detach().to(device="cpu").contiguous()
        folder, filename, _counter, _subfolder, _prefix = folder_paths.get_save_image_path(
            filename_prefix, folder_paths.get_output_directory()
        )
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"{filename}_{clip_index:05d}.safetensors")
        _atomic_save_safetensors(path, {"video": video_cpu, "audio": audio_cpu})

        output_root = folder_paths.get_output_directory()
        subfolder = os.path.relpath(os.path.dirname(path), output_root)
        subfolder = "" if subfolder == "." else subfolder.replace(os.sep, "/")
        return {
            "ui": {"latents": [{"filename": os.path.basename(path), "subfolder": subfolder, "type": "output"}]},
            "result": (path,),
        }


class MiniMaxH3LatentLoad:
    """Load a paired H3 AV latent; accepts files produced by the old Motion Context saver."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "读取文件夹或文件": (
                    "STRING",
                    {"default": "h3_latent_chain", "tooltip": "默认读取 output/h3_latent_chain；旧 H3 Motion Context 文件可将这里改为 h3_context。"},
                ),
                "读取编号": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "max": 9999,
                        "step": 1,
                        "control_after_generate": "fixed",
                        "tooltip": "第一段保持 0（不读取上一段）；右侧控制方式选‘递增’，生成后自动变为 1，第二段读取第 1 段。",
                    },
                ),
            }
        }

    RETURN_TYPES = ("LATENT",)
    RETURN_NAMES = ("latent",)
    FUNCTION = "load_latent"
    CATEGORY = CATEGORY

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        try:
            clip_index = int(kwargs["读取编号"])
            if clip_index == 0:
                return "first-clip-no-previous-latent"
            path = _resolve_output_path(kwargs["读取文件夹或文件"], clip_index)
            st = os.stat(path)
            return f"{path}:{st.st_mtime_ns}:{st.st_size}"
        except Exception:
            # An unresolved file must not be served from a stale node cache.
            return float("NaN")

    def load_latent(self, **kwargs):
        if _st_load is None:
            raise RuntimeError("当前 Python 环境没有 safetensors，无法读取 H3 潜空间。")
        clip_index = int(kwargs["读取编号"])
        if clip_index == 0:
            return (None,)
        path = _resolve_output_path(kwargs["读取文件夹或文件"], clip_index)
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
        latent = {"samples": comfy.nested_tensor.NestedTensor((video, audio))}
        return (latent,)


class MiniMaxH3OverlapTrim:
    """Trim the duplicated decoded image prefix and the same duration from audio."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "图像": ("IMAGE", {"tooltip": "连接 H3 视频 VAE 解码后的图像序列。"}),
                "修剪帧数": ("INT", {"default": 22, "min": 0, "max": 4096, "step": 1, "tooltip": "这里手动输入与前缀复制节点相同的重叠帧数。第二段及以后通常填 22；第一段填 0。"}),
            },
            "optional": {
                "音频": ("AUDIO", {"tooltip": "连接同一段 H3 音频 VAE 解码后的音频；无音频时可不连接。"}),
                "帧率": ("FLOAT", {"default": 24.0, "min": 1.0, "max": 240.0, "step": 0.001, "tooltip": "必须与最终视频帧率一致；你的 H3 工作流通常使用 24 帧/秒。"}),
                "音频尾长匹配": ("BOOLEAN", {"default": True, "tooltip": "开启后，修剪音频开头后再把音频长度校准到剩余视频时长：过长则截短，过短则在末尾补静音，避免每段累计约 8ms 的音画漂移。"}),
            },
        }

    RETURN_TYPES = ("IMAGE", "AUDIO")
    RETURN_NAMES = ("图像", "音频")
    FUNCTION = "trim_overlap"
    CATEGORY = CATEGORY

    def trim_overlap(self, **kwargs):
        images = kwargs["图像"]
        trim_frames = max(0, int(kwargs["修剪帧数"]))
        audio = kwargs.get("音频")
        fps = float(kwargs.get("帧率", 24.0))
        match_tail = bool(kwargs.get("音频尾长匹配", True))

        if fps <= 0:
            raise ValueError("帧率必须大于 0。")
        if not torch.is_tensor(images) or images.ndim != 4:
            raise ValueError("图像输入应为 ComfyUI IMAGE，形状为 [帧数, 高, 宽, 通道]。")

        total_frames = int(images.shape[0])
        if trim_frames >= total_frames and trim_frames > 0:
            raise ValueError(f"要修剪 {trim_frames} 帧，但输入只有 {total_frames} 帧。")
        output_images = images[trim_frames:] if trim_frames else images
        output_audio = audio

        if audio is not None:
            if not isinstance(audio, dict) or "waveform" not in audio or "sample_rate" not in audio:
                raise ValueError("音频输入格式不正确，缺少 waveform 或 sample_rate。")
            waveform = audio["waveform"]
            sample_rate = int(audio["sample_rate"])
            if not torch.is_tensor(waveform) or waveform.ndim < 2 or sample_rate <= 0:
                raise ValueError("音频 waveform 或 sample_rate 无效。")

            cut_samples = int(round((trim_frames / fps) * sample_rate))
            audio_samples = int(waveform.shape[-1])
            if cut_samples >= audio_samples and trim_frames > 0:
                raise ValueError(
                    f"修剪 {trim_frames} 帧需要删除 {cut_samples} 个音频采样，"
                    f"但音频只有 {audio_samples} 个采样。请检查帧率或音频输入。"
                )
            # Remove exactly the same elapsed time from decoded audio.
            waveform = waveform[..., cut_samples:] if cut_samples else waveform

            if match_tail:
                frames_left = total_frames - trim_frames
                wanted_samples = int(round(frames_left / fps * sample_rate))
                have_samples = int(waveform.shape[-1])
                if have_samples > wanted_samples:
                    waveform = waveform[..., :wanted_samples]
                elif have_samples < wanted_samples:
                    # H3's 40 Hz audio grid rounds to the nearest step. If it
                    # leaves a short tail, silence is the only honest fill.
                    waveform = torch.nn.functional.pad(
                        waveform, (0, wanted_samples - have_samples)
                    )

            output_audio = {**audio, "waveform": waveform}

        return (output_images, output_audio)


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
    "MiniMaxH3LatentLoad": MiniMaxH3LatentLoad,
    "MiniMaxH3OverlapTrim": MiniMaxH3OverlapTrim,
    "H3SeparateAVLatent": H3SeparateAVLatent,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "MiniMaxH3LatentPrefixCopy": "H3 潜空间前缀复制",
    "MiniMaxH3LatentSave": "H3 潜空间保存",
    "MiniMaxH3LatentLoad": "H3 潜空间读取",
    "MiniMaxH3OverlapTrim": "H3 续接重叠帧修剪",
    "H3SeparateAVLatent": "H3 音视频潜空间分离",
}
