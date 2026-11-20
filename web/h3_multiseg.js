/** H3 多段输入 —— 前端编辑器。
 *
 * 数据只存一份：节点上的「时间线数据」（JSON 文本）。本文件只是它的编辑器；
 * 编辑器没加载时，「时间线数据」文本框仍可直接手改。
 * 编号/校验规则与后端对应（每段下面的预览显示最终发给模型的提示词）。
 * 输出口按需自动增减（只从末尾增删，位置永远和后端一致）。
 */
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const NODE_NAME = "H3MultiSegmentInput";
const DATA_WIDGET = "时间线数据";
const MODE_WIDGET = "生成模式";
const COMMON_INPUT = "公共提示词"; // 旧输入口：已取消，只保留名字以便识别旧工作流
const GLOBAL_INPUT = "全局提示词"; // 视频编辑：接线时替换全局提示词
const LOCAL_INPUT = "局部提示词"; // 视频编辑：接线时替换局部提示词（「---」分段）
/** 这个输入口有没有接线 */
function inputLinked(node, name) {
    const inp = node?.inputs?.find((i) => i.name === name);
    return !!inp && inp.link != null;
}

const KINDS = {
    img: { key: "images", tag: "Picture", max: 9, cn: "图片", short: "图", accept: "image/*" },
    vid: { key: "videos", tag: "Video", max: 3, cn: "视频", short: "视频", accept: "video/*" },
    aud: { key: "audios", tag: "Audio", max: 3, cn: "音频", short: "音频", accept: "audio/*" },
};
const KIND_ORDER = ["img", "vid", "aud"];

/* ----------------------------------------------------------------- 输出口顺序（与后端 OUTPUT_LAYOUT 一致） */
export const OUT_LAYOUT = (() => {
    const L = [
        { name: "提示词", type: "STRING", kind: "prompt", n: 0 },
    ];
    for (let r = 1; r <= 3; r++) {
        L.push({ name: `参考图${r}`, type: "IMAGE", kind: "img", n: r });
        L.push({ name: `参考视频${r}`, type: "IMAGE", kind: "vid", n: r });
        L.push({ name: `参考视频音轨${r}`, type: "AUDIO", kind: "vaud", n: r });
        L.push({ name: `参考音频${r}`, type: "AUDIO", kind: "aud", n: r });
        L.push({ name: `引导图片${r}`, type: "IMAGE", kind: "guide", n: r });
    }
    for (let r = 4; r <= 6; r++) {
        L.push({ name: `参考图${r}`, type: "IMAGE", kind: "img", n: r });
        L.push({ name: `引导图片${r}`, type: "IMAGE", kind: "guide", n: r });
    }
    for (let r = 7; r <= 9; r++) L.push({ name: `参考图${r}`, type: "IMAGE", kind: "img", n: r });
    // 三个模式共用这些口、含义各不相同，后端把它们全部声明成任意类型 "*"，这里保持一致
    return L.map((o) => ({ ...o, type: "*" }));
})();
// 首尾帧模式的输出口排布（位置固定，引导图片只在末尾追加）
const FL_NAMES = ["首帧", "尾帧", "提示词", ...Array.from({ length: 6 }, (_, i) => `引导图片${i + 1}`)];
export const MODE_REF = "参考生视频";
export const MODE_FL = "首尾帧视频";
export const MODE_VE = "视频编辑";
export const GUIDE_MAX = 6;

/** 需要保留到第几个输出口（含）。每类至少 1 个。 */
export function lastNeededOutput(state, mode = MODE_REF) {
    let maxImg = 1;
    let maxVid = 1;
    let maxAud = 1;
    let maxGuide = 0;
    for (const seg of state.segments) {
        maxImg = Math.max(maxImg, state.common.images.length + seg.images.length);
        maxVid = Math.max(maxVid, state.common.videos.length + seg.videos.length);
        maxAud = Math.max(maxAud, state.common.audios.length + seg.audios.length);
        maxGuide = Math.max(maxGuide, seg.guides.length);
    }
    maxImg = Math.min(maxImg, 9);
    maxVid = Math.min(maxVid, 3);
    maxAud = Math.min(maxAud, 3);
    maxGuide = Math.min(maxGuide, GUIDE_MAX);
    const pos = (kind, n) => OUT_LAYOUT.findIndex((o) => o.kind === kind && o.n === n);
    const guidePos = maxGuide ? pos("guide", maxGuide) : 0;
    if (mode === MODE_FL) return 2 + maxGuide; // 首帧、尾帧、提示词（+ 用到的引导图片）
    return Math.max(0, pos("img", maxImg), pos("vid", maxVid), pos("vaud", maxVid), pos("aud", maxAud), guidePos);
}

/** 输出口在当前模式下的显示名：首尾帧模式下「参考图1/参考视频1」的位置显示为「首帧/尾帧」 */
export function outputName(index, mode) {
    if (mode === MODE_FL) return FL_NAMES[index];
    return OUT_LAYOUT[index].name;
}

/* ----------------------------------------------------------------- 数据 */
export const JOINS = ["复制", "引导", "硬切"];

export function emptyMedia() {
    return { images: [], videos: [], audios: [] };
}
export function newSegment() {
    return { prompt: "", use_common: true, join: null, first: null, last: null, guides: [], ...emptyMedia() };
}
export const VE_HEADS = [5, 22, 39, 56];
/** 重叠帧数吸附到最近的合法值（平局取大），和后端 h3t_ve_settings 一致 */
export const snapHead = (h) => {
    const x = Number.isFinite(Number(h)) ? Number(h) : 22;
    return VE_HEADS.reduce((best, c) => (Math.abs(c - x) < Math.abs(best - x) || (Math.abs(c - x) === Math.abs(best - x) && c > best) ? c : best), VE_HEADS[0]);
};
export const VE_MIN = 96;
export const VE_MAX = 360;
export const VE_FPS = 24;
export const VE_AUDIO = ["静音", "保留原音频", "模型生成"];
export function defaultVE() {
    return {
        head: 22, long_edge: 1344, width: 0, height: 0, audio: "静音",
        mask_file: "", mask_shape: "边界框", mask_grow: 16, mask_threshold: 0.5,
        sam_text: "person", sam_threshold: 0.5, sam_refine: 2, sam_model: "",
    };
}
export function defaultState() {
    return {
        version: 1, common: { prompt: "", ...emptyMedia() }, segments: [newSegment()], ui: { common_open: true }, progress: { done: 0, fps: [] },
        video: { file: "", start: 0, end: 0, duration: 0 }, cuts: [], ve: defaultVE(),
    };
}

function cleanItems(raw) {
    const out = [];
    for (const it of Array.isArray(raw) ? raw : []) {
        const f = String(typeof it === "string" ? it : it?.file || "").replace(/\\/g, "/").trim();
        if (f) out.push({ file: f });
    }
    return out;
}

export function normalizeState(data) {
    const st = defaultState();
    if (!data || typeof data !== "object") return st;
    const c = data.common || {};
    st.common.prompt = String(c.prompt || "");
    for (const k of KIND_ORDER) st.common[KINDS[k].key] = cleanItems(c[KINDS[k].key]);
    const segs = Array.isArray(data.segments) ? data.segments : [];
    st.segments = segs.map((s) => {
        const seg = newSegment();
        seg.prompt = String(s?.prompt || "");
        seg.use_common = s?.use_common !== false;
        seg.join = null; // 逐段衔接已取消：衔接方式统一在「前缀复制」节点上选
        seg.first = cleanItems(s?.first ? [s.first] : [])[0] || null;
        seg.last = cleanItems(s?.last ? [s.last] : [])[0] || null;
        seg.guides = cleanItems(s?.guides);
        for (const k of KIND_ORDER) seg[KINDS[k].key] = cleanItems(s?.[KINDS[k].key]);
        return seg;
    });
    if (!st.segments.length) st.segments.push(newSegment());
    st.ui = { common_open: data.ui?.common_open !== false, ve_open: data.ui?.ve_open !== false };
    const v = data.video && typeof data.video === "object" ? data.video : {};
    st.video = {
        file: String(v.file || "").replace(/\\/g, "/").trim(),
        start: Math.max(0, Number(v.start) || 0), end: Math.max(0, Number(v.end) || 0), duration: Math.max(0, Number(v.duration) || 0),
    };
    st.cuts = (Array.isArray(data.cuts) ? data.cuts : []).map(Number).filter((x) => Number.isFinite(x));
    const ve = data.ve && typeof data.ve === "object" ? data.ve : {};
    const d = defaultVE();
    st.ve = {
        head: VE_HEADS.includes(ve.head) ? ve.head : d.head,
        long_edge: Math.max(64, parseInt(ve.long_edge, 10) || d.long_edge),
        width: Math.max(0, parseInt(ve.width, 10) || 0), height: Math.max(0, parseInt(ve.height, 10) || 0),
        audio: VE_AUDIO.includes(ve.audio) ? ve.audio : d.audio,
        mask_file: String(ve.mask_file || ""),
        mask_shape: ve.mask_shape === "轮廓" ? "轮廓" : "边界框",
        mask_grow: Math.max(0, Number.isFinite(Number(ve.mask_grow)) ? Math.round(Number(ve.mask_grow)) : d.mask_grow),
        mask_threshold: Number.isFinite(Number(ve.mask_threshold)) && ve.mask_threshold !== "" ? Number(ve.mask_threshold) : d.mask_threshold,
        sam_text: String(ve.sam_text ?? d.sam_text), sam_threshold: Number.isFinite(Number(ve.sam_threshold)) ? Number(ve.sam_threshold) : d.sam_threshold,
        sam_refine: Math.min(5, Math.max(0, parseInt(ve.sam_refine, 10) || 0)) || (ve.sam_refine === 0 ? 0 : d.sam_refine),
        sam_model: String(ve.sam_model || ""),
    };
    const pr = data.progress || {};
    st.progress = {
        done: Math.max(0, parseInt(pr.done, 10) || 0),
        fps: Array.isArray(pr.fps) ? pr.fps.map(String) : [],
        task: typeof pr.task === "string" ? pr.task : "",
    };
    // 视频编辑的图片轨 / 提示词：本编辑器不用，但原样带着，切换生成模式时不丢
    for (const k of ["vtracks", "pmode", "gprompt", "lprompts", "gtracks", "gpoints"]) if (data[k] !== undefined) st[k] = data[k];
    return st;
}


/* ----------------------------------------------------------------- 视频编辑：分段规划（与后端 h3t_ve_plan 完全一致） */
export function veRefs(state, i) {
    return [...state.common.images, ...state.segments[i].images].map((x) => x.file);
}
export function veJoins(state) {
    const j = state.ve && (state.ve.join === "引导" || state.ve.join === "复制" || state.ve.join === "图片引导") ? state.ve.join : "硬切";
    return state.segments.map((_, i) => (i === 0 ? "硬切" : j)); // 第 1 段永远硬切；其余由「段间衔接」决定
}
export function veTotalFrames(state) {
    const v = state.video;
    const end = v.end > 0 ? Math.min(v.end, v.duration || v.end) : v.duration;
    return Math.max(0, Math.floor((end - v.start) * VE_FPS + 0.5));
}
function roundTo(value, lo, step, offset) {
    const k = Math.max(0, Math.floor((Math.max(value, lo) - offset) / step + 0.5));
    return Math.max(offset + step * k, lo);
}
/** → {plan, warns, error}：出错时 plan 为空数组、error 是提示。 */
export function vePlan(state, total) {
    const n = state.segments.length;
    const cuts = state.cuts;
    const fail = (error) => ({ plan: [], warns: [], error });
    if (cuts.length !== n - 1) return fail(`分割点有 ${cuts.length} 个，应该比分段数少 1（现在 ${n} 段）。`);
    for (let i = 1; i < cuts.length; i++) if (cuts[i] <= cuts[i - 1]) return fail("分割点必须按时间从小到大。");
    if (cuts.length && cuts[0] <= 0) return fail("分割点必须在视频开头之后。");
    const head = snapHead(state.ve.head);
    const joins = veJoins(state);
    const plan = [];
    const warns = [];
    let pos = 0;
    for (let i = 0; i < n; i++) {
        const hard = joins[i] === "硬切";
        const h = hard ? 0 : head;
        const [lo, step, off] = hard ? [5, 17, 5] : [17, 17, 0];
        if (!hard && pos < h) return fail(`第 ${i + 1} 段需要 ${h} 帧衔接头，但前面只有 ${pos} 帧：请把前面的分割点往后移。`);
        let delivered;
        if (i < n - 1) {
            const want = Math.floor(cuts[i] * VE_FPS + 0.5) - pos;
            delivered = roundTo(want, lo, step, off);
            if (pos + delivered >= total) return fail(`第 ${i + 1} 段的分割点吸附后已到视频结尾，请把分割点往前移或减少分段。`);
        } else {
            const remain = total - pos;
            if (remain <= 0) return fail("最后一段没有画面：分割点太靠近视频结尾。");
            delivered = roundTo(remain, lo, step, off);
            if (delivered < remain) delivered += step;
        }
        const sampled = h + delivered;
        if (sampled > VE_MAX) return fail(`第 ${i + 1} 段采样 ${sampled} 帧，超过模型范围 ${VE_MAX} 帧（约 15 秒）：请再加分割点。`);
        if (sampled < VE_MIN) warns.push(`第 ${i + 1} 段只有 ${sampled} 帧（约 ${(sampled / VE_FPS).toFixed(1)} 秒），低于模型训练范围 ${VE_MIN} 帧，效果可能变差。`);
        plan.push({ index: i, head: h, delivered, sampled, story_start: pos, story_end: pos + delivered, join: joins[i], tail_cut: 0 });
        pos += delivered;
    }
    plan[plan.length - 1].tail_cut = pos - total;
    return { plan, warns, error: "" };
}

/* ----------------------------------------------------------------- 解析（与 core 部分对应） */
const TOKEN_RE = /\[\[\s*([cs])\s*:\s*(img|vid|aud)\s*:\s*(\d+)\s*\]\]/gi;
const LITERAL_RE = /<(Picture|Video|Audio)\s+(\d+)\s*>/gi;
const TAG_TO_KIND = { picture: "img", video: "vid", audio: "aud" };

function resolvePrompt(text, commonCounts, segCounts, errors) {
    const totals = {};
    for (const k of KIND_ORDER) totals[k] = commonCounts[k] + segCounts[k];
    const out = text.replace(TOKEN_RE, (m, scope, kind, nStr) => {
        scope = scope.toLowerCase();
        kind = kind.toLowerCase();
        const n = parseInt(nStr, 10);
        const K = KINDS[kind];
        if (n < 1) {
            errors.push(`引用编号必须从 1 开始：${m}`);
            return m;
        }
        if (scope === "c") {
            if (n > commonCounts[kind]) {
                errors.push(`引用了全局区域第 ${n} 个${K.cn}，但全局区域只有 ${commonCounts[kind]} 个`);
                return m;
            }
            return `<${K.tag} ${n}>`;
        }
        if (n > segCounts[kind]) {
            errors.push(`引用了本段第 ${n} 个${K.cn}，但本段只添加了 ${segCounts[kind]} 个`);
            return m;
        }
        return `<${K.tag} ${commonCounts[kind] + n}>`;
    });
    out.replace(LITERAL_RE, (m, tag, nStr) => {
        const kind = TAG_TO_KIND[tag.toLowerCase()];
        const n = parseInt(nStr, 10);
        if (n < 1 || n > totals[kind]) {
            errors.push(`提示词里的 <${KINDS[kind].tag} ${n}> 找不到对应${KINDS[kind].cn}（本段共有 ${totals[kind]} 个）`);
        }
        return m;
    });
    return out;
}

/** 返回 {prompt, errors[], files:{img,vid,aud}, commonCounts, segCounts}
 *  mode：参考生视频 / 首尾帧视频（首尾帧不使用参考素材）；commonLinked：公共提示词由连线提供（运行时才知道，这里不检查）。 */
export function resolveSegment(state, index, mode = MODE_REF, commonLinked = false) {
    const errors = [];
    const seg = state.segments[index];
    const files = {};
    const commonCounts = {};
    const segCounts = {};
    for (const k of KIND_ORDER) {
        const key = KINDS[k].key;
        if (mode === MODE_FL) {
            commonCounts[k] = 0;
            segCounts[k] = 0;
            files[k] = [];
            continue;
        }
        commonCounts[k] = state.common[key].length;
        segCounts[k] = seg[key].length;
        files[k] = [...state.common[key], ...seg[key]].map((x) => x.file);
        if (files[k].length > KINDS[k].max) {
            errors.push(
                `${KINDS[k].cn}共 ${files[k].length} 个（全局 ${commonCounts[k]} + 本段 ${segCounts[k]}），超过官方上限 ${KINDS[k].max} 个`,
            );
        }
    }
    if (seg.guides.length > GUIDE_MAX) errors.push(`引导图片 ${seg.guides.length} 张，超过上限 ${GUIDE_MAX} 张`);
    const segPrompt = resolvePrompt(seg.prompt.trim(), commonCounts, segCounts, errors);
    let commonPrompt = "";
    if (seg.use_common && !commonLinked) commonPrompt = resolvePrompt(state.common.prompt.trim(), commonCounts, segCounts, errors);
    const prompt = commonPrompt && segPrompt ? `${commonPrompt}\n\n${segPrompt}` : commonPrompt || segPrompt;
    if (!prompt.trim() && !(commonLinked && seg.use_common) && mode !== MODE_VE) errors.push("提示词是空的（全局提示词也没有）");
    return { prompt, errors: [...new Set(errors)], files, commonCounts, segCounts };
}

/* ----------------------------------------------------------------- 样式 */
const CSS = `
.h3ms{box-sizing:border-box;height:100%;overflow:auto;padding:6px;font:13px/1.45 sans-serif;color:#ddd;background:#1b1b1b;border-radius:6px}
.h3ms *{box-sizing:border-box}
.h3ms button{background:#3a3a3a;color:#ddd;border:1px solid #555;border-radius:4px;padding:3px 9px;cursor:pointer;font-size:12px}
.h3ms button:hover{background:#4a4a4a}
.h3ms button.danger:hover{background:#7a2a2a}
.h3ms textarea{width:100%;min-height:84px;resize:vertical;background:#141414;color:#eee;border:1px solid #444;border-radius:4px;padding:6px;font:13px/1.45 sans-serif}
.h3ms textarea:focus{border-color:#4fa36a;outline:none}
.h3ms details.common{border:1px solid #4a6a8a;border-radius:6px;margin-bottom:8px;background:#222}
.h3ms details.common>summary{cursor:pointer;padding:7px 10px;font-weight:600;background:#2a2f36;border-radius:6px;list-style:none;display:flex;gap:8px;align-items:center}
.h3ms details.common>summary::before{content:"▸";color:#8ab}
.h3ms details.common[open]>summary::before{content:"▾"}
.h3ms details.common .sum{color:#9ab;font-weight:400;font-size:12px}
.h3ms .pad{padding:8px}
.h3ms .tabs{display:flex;gap:4px;overflow-x:auto;padding-bottom:4px;margin-bottom:6px;border-bottom:1px solid #3a3a3a}
.h3ms .tab{flex:0 0 auto;border-radius:6px 6px 0 0!important;padding:5px 12px!important;background:#2a2a2a!important}
.h3ms .tab.on{background:#3d6b4f!important;border-color:#4fa36a!important;color:#fff!important;font-weight:600}
.h3ms .tab .warn{color:#ffb86b;margin-left:4px}
.h3ms .tab.add{background:#2d3a2d!important}
.h3ms .bar{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin-bottom:6px}
.h3ms .bar .sp{flex:1}
.h3ms label{display:inline-flex;align-items:center;gap:5px;cursor:pointer}
.h3ms .sec{margin-top:10px;color:#9ab;font-size:12px}
.h3ms .joinbox{display:flex;gap:4px;align-items:center;color:#9cb;margin-right:8px}
.h3ms .status{margin:4px 0 6px;font-size:12px;color:#9ab;display:flex;gap:8px;align-items:center}
.h3ms .status:empty{display:none}
.h3ms textarea:disabled{opacity:.5}
.h3ms select{background:#2b2b2b;color:#ddd;border:1px solid #555;border-radius:4px;padding:2px 4px}
.h3ms .kind{margin-top:6px}
.h3ms .kind .kt{color:#999;font-size:12px;margin-bottom:3px}
.h3ms .cards{display:flex;flex-wrap:wrap;gap:8px}
.h3ms .card{width:132px;border:1px solid #4a4a4a;border-radius:6px;background:#262626;overflow:hidden}
.h3ms .card .thumb{position:relative;width:100%;height:100px;background:#0f0f0f;display:flex;align-items:center;justify-content:center;font-size:34px}
.h3ms .card .thumb img,.h3ms .card .thumb video{width:100%;height:100%;object-fit:cover}
.h3ms .card .no{position:absolute;left:4px;top:4px;background:rgba(0,0,0,.7);color:#8fd;font-weight:700;font-size:12px;padding:0 6px;border-radius:9px}
.h3ms .card .nm{padding:3px 6px 0;font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#bbb}
.h3ms .card .acts{display:flex;gap:4px;padding:4px 6px 6px}
.h3ms .card .acts button{flex:1;padding:1px 0}
.h3ms .card.ro{opacity:.9;border-style:dashed}
.h3ms .addbtn{padding:6px 14px!important;border:1px dashed #666!important;border-radius:6px!important;background:#222!important;font-size:13px!important}
.h3ms .addbtn.big{width:132px;height:154px}
.h3ms .preview{margin-top:6px;padding:7px;background:#161616;border:1px dashed #444;border-radius:4px;white-space:pre-wrap;word-break:break-word;color:#9bd;font-size:12px}
.h3ms .err{margin-top:4px;color:#ff7b7b;font-size:12px}
.h3ms .hint{color:#888;font-size:12px;margin-top:4px}
.h3ms .card .thumb{cursor:zoom-in}
 .h3ms .ve{border:1px solid #6a5a8a;border-radius:6px;margin-bottom:8px;background:#222}
.h3ms .ve>summary{cursor:pointer;padding:7px 10px;font-weight:600;background:#2f2a3a;border-radius:6px;list-style:none;display:flex;gap:8px;align-items:center}
.h3ms .ve>summary::before{content:"▸";color:#a9b}
.h3ms .ve[open]>summary::before{content:"▾"}
.h3ms .ve video{width:100%;max-height:260px;background:#000;border-radius:4px;display:block}
.h3ms .vebar{position:relative;height:44px;margin:8px 0 2px;background:#111;border:1px solid #444;border-radius:4px;user-select:none;cursor:pointer;overflow:hidden}
.h3ms .vebar .blk{position:absolute;top:0;bottom:0;display:flex;align-items:center;justify-content:center;font-size:12px;color:#fff;border-right:1px solid #0008;overflow:hidden;white-space:nowrap}
.h3ms .vebar .blk.on{outline:2px solid #fff;outline-offset:-2px;z-index:1}
.h3ms .vebar .cut{position:absolute;top:0;bottom:0;width:9px;margin-left:-4px;cursor:ew-resize;z-index:3;display:flex;justify-content:center}
.h3ms .vebar .cut::after{content:"";width:3px;background:#ffd24a;height:100%}
.h3ms .vebar .head{position:absolute;top:0;bottom:0;width:2px;background:#ff5a5a;z-index:2;pointer-events:none}
.h3ms .vegrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:6px 10px;margin:6px 0}
.h3ms .vegrid label{display:flex;flex-direction:column;align-items:flex-start;gap:2px;color:#9ab;font-size:12px}
.h3ms .vegrid input,.h3ms .vegrid select{width:100%;background:#141414;color:#eee;border:1px solid #444;border-radius:4px;padding:3px 5px}
.h3ms .cutrow{display:flex;gap:6px;align-items:center;margin:2px 0}
.h3ms .cutrow input{width:84px;background:#141414;color:#eee;border:1px solid #444;border-radius:4px;padding:2px 5px}
.h3ms .plan{margin-top:6px;padding:6px 8px;background:#161616;border:1px dashed #444;border-radius:4px;font-size:12px;color:#9bd;white-space:pre-wrap}
.h3ms .plan .w{color:#ffb86b}
.h3ms-lb{position:fixed;inset:0;z-index:100000;background:rgba(0,0,0,.88);display:flex;align-items:center;justify-content:center;overflow:hidden;font:13px/1.4 sans-serif;color:#ddd;user-select:none}
.h3ms-lb .stage{position:absolute;inset:0;display:flex;align-items:center;justify-content:center}
.h3ms-lb img{max-width:92vw;max-height:88vh;cursor:grab;transform-origin:center center;will-change:transform}
.h3ms-lb img.drag{cursor:grabbing}
.h3ms-lb video{max-width:92vw;max-height:88vh;background:#000}
.h3ms-lb .au{padding:30px 40px;background:#222;border:1px solid #555;border-radius:8px;text-align:center}
.h3ms-lb .au audio{width:min(80vw,520px);margin-top:10px}
.h3ms-lb .top{position:absolute;top:0;left:0;right:0;display:flex;justify-content:space-between;align-items:center;padding:10px 16px;background:linear-gradient(#000a,#0000);z-index:1;pointer-events:none}
.h3ms-lb .top *{pointer-events:auto}
.h3ms-lb button{background:#3a3a3a;color:#ddd;border:1px solid #666;border-radius:4px;padding:4px 12px;cursor:pointer;font-size:13px;margin-left:6px}
.h3ms-lb .tip{color:#aaa;font-size:12px}
`;
let cssDone = false;
export function injectCss() {
    if (cssDone) return;
    cssDone = true;
    const s = document.createElement("style");
    s.textContent = CSS;
    document.head.appendChild(s);
}

export function hashText(str) {
    let h = 5381;
    for (let i = 0; i < str.length; i++) h = ((h * 33) ^ str.charCodeAt(i)) >>> 0;
    return h.toString(36);
}

/** 点缩略图弹出的放大预览：图片（滚轮缩放、拖动、双击切换适应/原始大小）、视频播放、音频试听。Esc 或点空白处关闭。 */
export function openViewer(kind, url, title) {
    injectCss();
    const lb = document.createElement("div");
    lb.className = "h3ms-lb";
    const stage = document.createElement("div");
    stage.className = "stage";
    const top = document.createElement("div");
    top.className = "top";
    const name = document.createElement("span");
    name.textContent = title;
    const right = document.createElement("span");
    const tip = document.createElement("span");
    tip.className = "tip";
    const closeBtn = document.createElement("button");
    closeBtn.textContent = "✕ 关闭";
    right.append(tip, closeBtn);
    top.append(name, right);
    lb.append(stage, top);

    let vEl = null; // 视频预览里的 video（键盘用）
    const onKey = (e) => {
        if (e.key === "Escape") { close(); return; }
        if (!vEl || e.ctrlKey || e.metaKey || e.altKey) return;
        const t = e.target;
        if (t && t.tagName === "INPUT" && t.type !== "range") return; // 帧号输入框里打字
        const FPS = 24;
        const f = Math.max(0, Math.floor(vEl.currentTime * FPS + 1e-3));
        const total = Number.isFinite(vEl.duration) ? Math.floor(vEl.duration * FPS + 1e-3) : 0;
        const go = (n) => {
            vEl.pause();
            const hi = total > 0 ? total - 1 : n;
            vEl.currentTime = (Math.max(0, Math.min(hi, n)) + 0.5) / FPS;
        };
        let handled = true;
        if (e.key === "ArrowLeft") go(f - (e.shiftKey ? 10 : 1));
        else if (e.key === "ArrowRight") go(f + (e.shiftKey ? 10 : 1));
        else if (e.key === "Home") go(0);
        else if (e.key === "End") go(total - 1);
        else if (e.key === " " && !(t && (t.tagName === "BUTTON"))) { if (vEl.paused) vEl.play().catch(() => {}); else vEl.pause(); }
        else handled = false;
        if (handled) { e.preventDefault(); e.stopPropagation(); }
    };
    const close = () => {
        document.removeEventListener("keydown", onKey, true);
        lb.querySelectorAll("video,audio").forEach((m) => m.pause?.());
        lb.remove();
    };
    document.addEventListener("keydown", onKey, true);
    closeBtn.addEventListener("click", close);
    lb.addEventListener("mousedown", (e) => {
        if (e.target === lb || e.target === stage) close();
    });

    if (kind === "img") {
        const img = document.createElement("img");
        img.src = url;
        img.draggable = false;
        let scale = 1, tx = 0, ty = 0;
        const apply = () => {
            img.style.transform = `translate(${tx}px,${ty}px) scale(${scale})`;
            tip.textContent = `${Math.round(scale * 100)}%　滚轮缩放 · 拖动平移 · 双击还原`;
        };
        const reset = () => {
            scale = 1; tx = 0; ty = 0; apply();
        };
        img.addEventListener("load", () => {
            tip.dataset.size = `${img.naturalWidth}×${img.naturalHeight}`;
            name.textContent = `${title}　${img.naturalWidth}×${img.naturalHeight}`;
        });
        stage.addEventListener("wheel", (e) => {
            e.preventDefault();
            const r = img.getBoundingClientRect();
            const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
            const f = e.deltaY < 0 ? 1.15 : 1 / 1.15;
            const ns = Math.max(0.1, Math.min(40, scale * f));
            const k = ns / scale;
            tx += (e.clientX - cx) * (1 - k);
            ty += (e.clientY - cy) * (1 - k);
            scale = ns;
            apply();
        }, { passive: false });
        let drag = null;
        img.addEventListener("pointerdown", (e) => {
            drag = { x: e.clientX - tx, y: e.clientY - ty };
            img.classList.add("drag");
            img.setPointerCapture?.(e.pointerId);
        });
        img.addEventListener("pointermove", (e) => {
            if (!drag) return;
            tx = e.clientX - drag.x;
            ty = e.clientY - drag.y;
            apply();
        });
        const end = () => {
            drag = null;
            img.classList.remove("drag");
        };
        img.addEventListener("pointerup", end);
        img.addEventListener("pointercancel", end);
        img.addEventListener("dblclick", () => {
            if (Math.abs(scale - 1) > 0.01) reset();
            else {
                const fit = Math.min(img.clientWidth ? img.naturalWidth / img.clientWidth : 1, 8);
                scale = fit > 1 ? fit : 2;
                tx = 0; ty = 0; apply();
            }
        });
        stage.appendChild(img);
        apply();
    } else if (kind === "vid") {
        const v = document.createElement("video");
        v.src = url;
        v.controls = true;
        v.autoplay = true;
        v.loop = true;
        stage.appendChild(v);
        vEl = v;
        const FPS = 24;
        const fmtTime = (sec) => {
            const m = Math.floor(sec / 60);
            return `${m}:${(sec - m * 60).toFixed(2).padStart(5, "0")}`;
        };
        const jump = document.createElement("input");
        jump.type = "number";
        jump.min = "0";
        jump.placeholder = "跳到帧";
        jump.title = "输入帧号，按回车跳转（24 fps，从 0 数）";
        jump.style.width = "76px";
        right.insertBefore(jump, tip);
        const refresh = () => {
            tip.textContent = `第 ${Math.max(0, Math.floor(v.currentTime * FPS + 1e-3))} 帧 · ${fmtTime(v.currentTime)}　← → 逐帧（Shift 10 帧）· 空格 播放/暂停 · Home/End`;
        };
        v.addEventListener("timeupdate", refresh);
        v.addEventListener("seeked", refresh);
        v.addEventListener("loadedmetadata", refresh);
        if (typeof v.requestVideoFrameCallback === "function") {
            const tick = () => { refresh(); v.requestVideoFrameCallback(tick); };
            v.requestVideoFrameCallback(tick);
        }
        jump.addEventListener("keydown", (e) => {
            if (e.key !== "Enter") return;
            const n = parseInt(jump.value, 10);
            if (!Number.isFinite(n)) return;
            v.pause();
            v.currentTime = (Math.max(0, n) + 0.5) / FPS;
        });
        refresh();
    } else {
        const box = document.createElement("div");
        box.className = "au";
        const a = document.createElement("audio");
        a.src = url;
        a.controls = true;
        a.autoplay = true;
        box.append(document.createTextNode("🎵 " + title), document.createElement("br"), a);
        stage.appendChild(box);
    }
    document.body.appendChild(lb);
    return lb;
}

export function el(tag, props = {}, children = []) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
        if (k === "class") e.className = v;
        else if (k === "text") e.textContent = v;
        else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
        else if (v !== undefined && v !== null) e.setAttribute(k, v);
    }
    for (const c of [].concat(children)) if (c) e.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    return e;
}

export function splitPath(rel) {
    const i = rel.lastIndexOf("/");
    return i < 0 ? { subfolder: "", filename: rel } : { subfolder: rel.slice(0, i), filename: rel.slice(i + 1) };
}
export function viewUrl(rel) {
    const { subfolder, filename } = splitPath(rel);
    const p = new URLSearchParams({ filename, type: "input" });
    if (subfolder) p.set("subfolder", subfolder);
    return api.apiURL(`/view?${p.toString()}`);
}
export async function uploadFile(file) {
    const body = new FormData();
    body.append("image", file);
    body.append("type", "input");
    body.append("overwrite", "false");
    const resp = await api.fetchApi("/upload/image", { method: "POST", body });
    if (resp.status !== 200) throw new Error(`上传失败：HTTP ${resp.status}`);
    const j = await resp.json();
    return (j.subfolder ? `${j.subfolder}/` : "") + j.name;
}

/* ----------------------------------------------------------------- 输出口按需增减 */
function syncOutputs(node, state, mode) {
    if (!node || !Array.isArray(node.outputs) || !node.addOutput || !node.removeOutput) return;
    // 载入工作流时节点先按默认空数据创建、之后才读入存档；此时裁掉输出口会让存档里接在后面的连线恢复不了。等 onConfigure 读完数据再整理。
    if (app.configuringGraph && !node.__h3Configured) return;
    const want = lastNeededOutput(state, mode) + 1;
    let touched = false;
    while (node.outputs.length > want) {
        node.removeOutput(node.outputs.length - 1);
        touched = true;
    }
    while (node.outputs.length < want) {
        const d = OUT_LAYOUT[node.outputs.length];
        node.addOutput(d.name, d.type);
        touched = true;
    }
    for (let i = 0; i < node.outputs.length; i++) {
        const nm = outputName(i, mode);
        const o = node.outputs[i];
        // 新版前端显示名的优先级：label > localized_name > name，三个都要改
        if (o.name !== nm || o.label !== nm || (o.localized_name !== undefined && o.localized_name !== nm)) {
            o.name = nm;
            o.label = nm;
            if (o.localized_name !== undefined) o.localized_name = nm;
            touched = true;
        }
    }
    if (touched) app.graph?.setDirtyCanvas?.(true, true);
}


/* ----------------------------------------------------------------- 视频编辑：遮罩生成（排队运行「H3 视频遮罩生成」节点） */
let samModelsCache = null;
async function loadSamModels() {
    if (samModelsCache) return samModelsCache;
    try {
        const r = await api.fetchApi("/object_info/H3VideoMaskGen");
        const info = await r.json();
        const list = info?.H3VideoMaskGen?.input?.required?.["SAM3模型"]?.[0];
        if (Array.isArray(list)) samModelsCache = list.filter((x) => typeof x === "string" && !x.includes("没有"));
    } catch (_) {}
    return samModelsCache || [];
}
async function runMaskJob(video, p, onText) {
    const prompt = { 1: { class_type: "H3VideoMaskGen", inputs: {
        "视频": video, "遮罩对象": p.text, "SAM3模型": p.model, "检测阈值": p.threshold, "细化迭代": p.refine } } };
    const r = await api.fetchApi("/prompt", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt, client_id: api.clientId }) });
    if (!r.ok) {
        let msg = `排队失败：HTTP ${r.status}`;
        try { const j = await r.json(); msg = j?.error?.message || msg; const ne = j?.node_errors; if (ne) msg += "；" + JSON.stringify(ne).slice(0, 300); } catch (_) {}
        throw new Error(msg);
    }
    const { prompt_id: pid } = await r.json();
    onText?.("已排队，等待 ComfyUI 运行（可在队列里看进度）…");
    for (let i = 0; i < 6 * 60 * 24; i++) {
        await sleep(2000);
        let h = null;
        try { h = (await (await api.fetchApi(`/history/${pid}`)).json())[pid]; } catch (_) {}
        if (!h) continue;
        const st = h.status?.status_str;
        if (st === "success") {
            const rel = h.outputs?.["1"]?.text?.[0];
            if (!rel) throw new Error("遮罩生成结束了，但没有拿到文件名。");
            return String(rel);
        }
        let why = "未知原因";
        for (const [kind, info] of h.status?.messages || []) {
            if (kind === "execution_error") why = info?.exception_message || why;
            if (kind === "execution_interrupted") why = "被手动停止";
        }
        throw new Error("遮罩生成失败：" + String(why).trim());
    }
    throw new Error("等待遮罩生成超时。");
}

/* ----------------------------------------------------------------- 编辑器 */
function buildEditor(node, dataWidget, modeWidget) {
    injectCss();
    let state = defaultState();
    let active = 0; // 当前分段标签页（0 起）
    let statusBox = null;
    let runText = "";
    let lastFocus = null; // {ta}
    let segTA = null;
    let commonTA = null;
    const root = el("div", { class: "h3ms" });
    let tabEls = [];
    let errBox = null;
    const mode = () => (modeWidget?.value === MODE_FL ? MODE_FL : modeWidget?.value === MODE_VE ? MODE_VE : MODE_REF);
    // 全局提示词输入口有没有接线（三种模式都有）
    const commonLinked = () => inputLinked(node, GLOBAL_INPUT);
    const isVE = () => mode() === MODE_VE;

    const save = () => {
        const text = JSON.stringify(state);
        if (dataWidget.value !== text) {
            dataWidget.value = text;
            try {
                dataWidget.callback?.(text);
            } catch (_) {}
            app.graph?.setDirtyCanvas?.(true, true);
        }
    };

    const refreshLive = () => {
        // 当前段预览 + 错误；各标签上的 ⚠
        if (errBox) {
            const r = resolveSegment(state, active, mode(), commonLinked());
            const errs = [...r.errors];
            if (isVE()) {
                if (!state.video.file) errs.push("还没有上传源视频");
                else {
                    const pl = curPlan();
                    if (pl?.error) errs.push(pl.error);
                }
            }
            errBox.textContent = errs.length ? "⚠ " + errs.join("；") : "";
        }
        tabEls.forEach(({ i, warn }) => {
            warn.textContent = resolveSegment(state, i, mode(), commonLinked()).errors.length ? "⚠" : "";
        });
        syncOutputs(node, state, mode());
    };
    const fp = (idx) => {
        if (!isVE()) return hashText(JSON.stringify([state.common, state.segments[idx]]));
        const v = state.video, ve = state.ve;  // 视频编辑：视频/分割点/尺寸/遮罩变了，已完成的段也要作废
        return hashText(JSON.stringify([state.common, state.segments[idx], v.file, v.start, v.end, state.cuts,
            ve.head, ve.long_edge, ve.width, ve.height, ve.mask_file, ve.mask_shape, ve.mask_grow, ve.mask_threshold]));
    };
    // 已完成且内容没被改过的段数
    const validDone = () => {
        const pr = state.progress || { done: 0, fps: [] };
        let n = 0;
        while (n < pr.done && n < state.segments.length && pr.fps[n] === fp(n)) n++;
        return n;
    };
    const setProgress = (done, task) => {
        const fps = [];
        for (let j = 0; j < done && j < state.segments.length; j++) fps.push(fp(j));
        state.progress = { done: fps.length, fps, task: task !== undefined ? task : (state.progress?.task || "") };
        save();
        refreshStatus();
    };
    const refreshStatus = () => {
        if (!statusBox) return;
        statusBox.textContent = "";
        const total = state.segments.length;
        const done = validDone();
        if (runText) {
            statusBox.appendChild(el("span", { text: runText }));
        } else if (done > 0 && done < total) {
            statusBox.appendChild(el("span", { text: `进度：已完成 ${done}/${total} 段（要继续：把「输出第几段」设为 ${done + 1}，打开「到最后一段」）` }));
        }
    };

    const changed = (rebuild) => {
        save();
        if (rebuild) render();
        else refreshLive();
    };

    const insertToken = (token) => {
        let ta = lastFocus?.ta;
        if (!ta || !ta.isConnected) ta = segTA?.isConnected ? segTA : commonTA;
        if (!ta) return;
        const s = ta.selectionStart ?? ta.value.length;
        const e = ta.selectionEnd ?? s;
        ta.value = ta.value.slice(0, s) + token + ta.value.slice(e);
        ta.selectionStart = ta.selectionEnd = s + token.length;
        ta.dispatchEvent(new Event("input"));
        ta.focus();
    };

    const promptBox = (getter, setter, placeholder) => {
        const ta = el("textarea", { placeholder });
        ta.value = getter();
        ta.addEventListener("focus", () => (lastFocus = { ta }));
        ta.addEventListener("input", () => {
            setter(ta.value);
            changed(false);
        });
        return ta;
    };

    /** 一张素材卡片：大缩略图 + 编号 + 文件名 + @ / × */
    const card = (k, item, label, onAt, onRemove) => {
        const url = viewUrl(item.file);
        let thumb;
        if (k === "img") thumb = el("img", { src: url, alt: "" });
        else if (k === "vid") thumb = el("video", { src: `${url}#t=0.1`, muted: "muted", preload: "metadata" });
        else thumb = el("span", { text: "🎵" });
        const acts = el("div", { class: "acts" });
        if (onAt) {
            acts.appendChild(
                el("button", {
                    text: "@",
                    title: "把这个素材的引用插入到提示词光标处",
                    onmousedown: (ev) => ev.preventDefault(),
                    onclick: onAt,
                }),
            );
        }
        if (onRemove) acts.appendChild(el("button", { class: "danger", text: "×", title: "移除", onclick: onRemove }));
        return el("div", { class: "card" + (onRemove ? "" : " ro"), title: item.file }, [
            el("div", { class: "thumb", title: "点击放大预览", onclick: () => openViewer(k, url, splitPath(item.file).filename) }, [thumb, el("span", { class: "no", text: label })]),
            el("div", { class: "nm", text: splitPath(item.file).filename }),
            acts,
        ]);
    };

    /** 三类素材（图/视频/音频）的卡片区。editable=false 时只读，只能点 @。 */
    const mediaSection = (holder, scope, editable) => {
        const wrap = el("div");
        for (const k of KIND_ORDER) {
            const K = KINDS[k];
            const list = holder[K.key];
            const base = scope === "c" ? 0 : state.common[K.key].length;
            if (!editable && !list.length) continue;
            const cards = el("div", { class: "cards" });
            list.forEach((item, i) => {
                cards.appendChild(
                    card(
                        k,
                        item,
                        `${K.short}${base + i + 1}`,
                        () => insertToken(`[[${scope}:${k}:${i + 1}]]`),
                        editable
                            ? () => {
                                  list.splice(i, 1);
                                  changed(true);
                              }
                            : null,
                    ),
                );
            });
            if (editable) {
                const input = el("input", { type: "file", accept: K.accept, style: "display:none", multiple: "multiple" });
                input.addEventListener("change", async () => {
                    for (const f of input.files) {
                        try {
                            list.push({ file: await uploadFile(f) });
                        } catch (err) {
                            alert(`${f.name}：${err.message}`);
                        }
                    }
                    input.value = "";
                    changed(true);
                });
                cards.appendChild(
                    el("button", { class: "addbtn" + (list.length ? " big" : ""), text: `＋ 添加${K.cn}`, onclick: () => input.click() }),
                );
                cards.appendChild(input);
            }
            wrap.appendChild(el("div", { class: "kind" }, [el("div", { class: "kt", text: `${K.cn}（最多 ${K.max}）` }), cards]));
        }
        return wrap;
    };

    /** 选文件并上传，返回 [{file}...]（max 限制张数） */
    const pickImages = (multiple, onDone) => {
        const input = el("input", { type: "file", accept: "image/*", style: "display:none" });
        if (multiple) input.setAttribute("multiple", "multiple");
        input.addEventListener("change", async () => {
            const out = [];
            for (const f of input.files) {
                try {
                    out.push({ file: await uploadFile(f) });
                } catch (err) {
                    alert(`${f.name}：${err.message}`);
                }
            }
            input.value = "";
            if (out.length) onDone(out);
        });
        return input;
    };

    /** 单张图（首帧 / 尾帧） */
    const singleSlot = (holder, key, title, hint) => {
        const item = holder[key];
        const cards = el("div", { class: "cards" });
        if (item) {
            cards.appendChild(
                card("img", item, title, null, () => {
                    holder[key] = null;
                    changed(true);
                }),
            );
        } else {
            const input = pickImages(false, (list) => {
                holder[key] = list[0];
                changed(true);
            });
            cards.appendChild(el("button", { class: "addbtn big", text: `＋ 添加${title}`, onclick: () => input.click() }));
            cards.appendChild(input);
        }
        return el("div", { class: "kind" }, [el("div", { class: "kt", text: hint ? `${title}（${hint}）` : title }), cards]);
    };

    /** 引导图片列表（给官方「添加引导」用，最多 GUIDE_MAX 张） */
    const guideSection = (seg) => {
        const cards = el("div", { class: "cards" });
        seg.guides.forEach((item, i) => {
            cards.appendChild(
                card("img", item, `引导${i + 1}`, null, () => {
                    seg.guides.splice(i, 1);
                    changed(true);
                }),
            );
        });
        if (seg.guides.length < GUIDE_MAX) {
            const input = pickImages(true, (list) => {
                for (const it of list) if (seg.guides.length < GUIDE_MAX) seg.guides.push(it);
                changed(true);
            });
            cards.appendChild(
                el("button", { class: "addbtn" + (seg.guides.length ? " big" : ""), text: "＋ 添加引导图片", onclick: () => input.click() }),
            );
            cards.appendChild(input);
        }
        return el("div", { class: "kind" }, [
            el("div", { class: "kt", text: `引导图片（最多 ${GUIDE_MAX}；输出口「引导图片N」，接官方「添加引导」）` }),
            cards,
        ]);
    };


    /* ---- 视频编辑：源视频 / 分割 / 遮罩 / 设置 ---- */
    let maskBusy = false;
    let maskMsg = "";
    let headEl = null;
    let dragRaf = 0;
    const round2 = (x) => Math.round(x * 100) / 100;
    const fmtT = (s) => {
        const m = Math.floor(s / 60);
        return `${m}:${(s - m * 60).toFixed(2).padStart(5, "0")}`;
    };
    const rangeDur = () => {
        const v = state.video;
        const end = v.end > 0 ? Math.min(v.end, v.duration || v.end) : v.duration;
        return Math.max(0, end - v.start);
    };
    const curPlan = () => (rangeDur() > 0 ? vePlan(state, veTotalFrames(state)) : null);
    const cloneSeg = (s) => {
        const c = JSON.parse(JSON.stringify(s));
        c.join = null;
        return c;
    };
    const equalSplit = (n) => {
        const dur = rangeDur();
        if (!dur) return alert("先上传源视频。");
        n = Math.max(1, Math.min(40, parseInt(n, 10) || 1));
        state.cuts = Array.from({ length: n - 1 }, (_, k) => round2((dur * (k + 1)) / n));
        while (state.segments.length < n) state.segments.push(cloneSeg(state.segments[state.segments.length - 1]));
        state.segments.length = n;
        active = Math.min(active, n - 1);
        changed(true);
    };
    const addCut = (t) => {
        const dur = rangeDur();
        if (!dur) return alert("先上传源视频。");
        t = round2(t);
        if (!(t > 0.1 && t < dur - 0.1)) return alert(`分割点必须在 0.1 ~ ${round2(dur - 0.1)} 秒之间（相对起始秒）。`);
        if (state.cuts.some((c) => Math.abs(c - t) < 0.1)) return alert("这里已经有分割点了。");
        const k = state.cuts.filter((c) => c < t).length;
        state.cuts.splice(k, 0, t);
        state.segments.splice(k + 1, 0, cloneSeg(state.segments[k]));
        active = k + 1;
        changed(true);
    };
    const removeCut = (k) => {
        state.cuts.splice(k, 1);
        state.segments.splice(k + 1, 1);
        active = Math.min(active, state.segments.length - 1);
        changed(true);
    };
    const setCut = (k, t) => {
        const lo = k > 0 ? state.cuts[k - 1] + 0.1 : 0.1;
        const hi = k < state.cuts.length - 1 ? state.cuts[k + 1] - 0.1 : rangeDur() - 0.1;
        state.cuts[k] = round2(Math.min(hi, Math.max(lo, t)));
    };

    const numField = (label, get, set, attrs = {}) => {
        const input = el("input", { type: "number", ...attrs });
        input.value = get();
        input.addEventListener("change", () => {
            set(Number(input.value));
            changed(true);
        });
        return el("label", {}, [label, input]);
    };
    const selField = (label, options, get, set) => {
        const sel = el("select", {}, options.map((o) => el("option", { value: String(o), text: String(o) })));
        sel.value = String(get());
        sel.addEventListener("change", () => {
            set(sel.value);
            changed(true);
        });
        return el("label", {}, [label, sel]);
    };

    const timelineBar = (pl) => {
        const dur = rangeDur();
        const bar = el("div", { class: "vebar", title: "点击选择分段并定位视频；拖动黄线调整分割点" });
        const colors = ["#3d6b4f", "#4a5f8a", "#8a5a3d", "#6a4a8a", "#3d7a80", "#8a3d5a", "#6a7a3d", "#5a5a5a"];
        const okp = pl && pl.plan.length ? pl.plan : null;
        const bounds = [0];
        (okp ? okp.slice(0, -1).map((e) => e.story_end / VE_FPS) : state.cuts).forEach((x) => bounds.push(x));
        bounds.push(okp ? veTotalFrames(state) / VE_FPS : dur);
        state.segments.forEach((_s, i) => {
            const a = bounds[i], b = bounds[i + 1];
            const blk = el("div", { class: "blk" + (i === active ? " on" : ""), text: `第${i + 1}段` });
            blk.style.left = `${(a / dur) * 100}%`;
            blk.style.width = `${Math.max(0, ((b - a) / dur) * 100)}%`;
            blk.style.background = colors[i % colors.length];
            bar.appendChild(blk);
        });
        state.cuts.forEach((_c, k) => {
            const h = el("div", { class: "cut", title: `分割点 ${k + 1}：${fmtT(state.cuts[k])}` });
            h.style.left = `${(bounds[k + 1] / dur) * 100}%`;
            h.addEventListener("mousedown", (ev) => {
                ev.preventDefault();
                ev.stopPropagation();
                const rect = bar.getBoundingClientRect();
                const move = (e2) => {
                    setCut(k, ((e2.clientX - rect.left) / rect.width) * dur);
                    if (!dragRaf) dragRaf = requestAnimationFrame(() => { dragRaf = 0; render(); });
                };
                const up = () => {
                    document.removeEventListener("mousemove", move);
                    document.removeEventListener("mouseup", up);
                    changed(true);
                };
                document.addEventListener("mousemove", move);
                document.addEventListener("mouseup", up);
            });
            bar.appendChild(h);
        });
        headEl = el("div", { class: "head" });
        bar.appendChild(headEl);
        bar.addEventListener("click", (ev) => {
            const rect = bar.getBoundingClientRect();
            const t = ((ev.clientX - rect.left) / rect.width) * dur;
            let idx = 0;
            for (let i = 0; i < state.segments.length; i++) if (t >= bounds[i]) idx = i;
            if (videoEl) videoEl.currentTime = state.video.start + t;
            if (idx !== active) {
                active = idx;
                render();
            }
        });
        return bar;
    };

    const veSection = () => {
        const v = state.video, ve = state.ve;
        const kids = [];
        const pickVideo = el("input", { type: "file", accept: "video/*", style: "display:none" });
        pickVideo.addEventListener("change", async () => {
            const f = pickVideo.files[0];
            pickVideo.value = "";
            if (!f) return;
            if (state.cuts.length && !confirm("更换源视频会重置所有分割点，继续吗？")) return;
            try {
                const rel = await uploadFile(f);
                state.video = { file: rel, start: 0, end: 0, duration: 0 };
                state.cuts = [];
                state.segments.length = 1;
                active = 0;
                changed(true);
            } catch (err) {
                alert(`${f.name}：${err.message}`);
            }
        });
        kids.push(
            el("div", { class: "bar" }, [
                el("button", { class: "addbtn", text: v.file ? "更换源视频" : "＋ 上传源视频", onclick: () => pickVideo.click() }),
                pickVideo,
                el("span", { class: "hint", text: v.file ? `${splitPath(v.file).filename}　${v.duration ? "时长 " + fmtT(v.duration) : "读取中…"}` : "先上传要换人的视频" }),
            ]),
        );
        if (!v.file) return kids;
        videoEl = el("video", { src: viewUrl(v.file), controls: "controls", preload: "metadata" });
        videoEl.addEventListener("loadedmetadata", () => {
            const d = round2(videoEl.duration || 0);
            if (d && Math.abs(d - (state.video.duration || 0)) > 0.01) {
                state.video.duration = d;
                changed(true);
            }
        });
        videoEl.addEventListener("timeupdate", () => {
            const dur = rangeDur();
            if (headEl && dur) headEl.style.left = `${Math.min(100, Math.max(0, ((videoEl.currentTime - state.video.start) / dur) * 100))}%`;
        });
        kids.push(videoEl);
        const pl = curPlan();
        if (rangeDur() > 0) {
            kids.push(timelineBar(pl));
            const splitN = el("input", { type: "number", min: "1", max: "40", value: String(state.segments.length), style: "width:60px" });
            kids.push(
                el("div", { class: "bar" }, [
                    el("span", { text: "平均分成" }),
                    splitN,
                    el("span", { text: "段" }),
                    el("button", { text: "均分", onclick: () => equalSplit(splitN.value) }),
                    el("button", {
                        text: "在播放位置分割",
                        title: "先在上面的视频里拖到想分割的地方，再点这个",
                        onclick: () => addCut((videoEl?.currentTime || 0) - state.video.start),
                    }),
                    el("button", { class: "danger", text: "清除全部分割", onclick: () => {
                        state.cuts = [];
                        state.segments.length = 1;
                        active = 0;
                        changed(true);
                    } }),
                ]),
            );
            state.cuts.forEach((c, k) => {
                const inp = el("input", { type: "number", step: "0.01", min: "0.1" });
                inp.value = c;
                inp.addEventListener("change", () => {
                    setCut(k, Number(inp.value));
                    changed(true);
                });
                kids.push(
                    el("div", { class: "cutrow" }, [
                        el("span", { text: `分割点 ${k + 1}：` }),
                        inp,
                        el("span", { class: "hint", text: "秒（相对起始秒）" }),
                        el("button", { class: "danger", text: "×", onclick: () => removeCut(k) }),
                    ]),
                );
            });
            const lines = [];
            if (pl?.error) lines.push(el("div", { class: "w", text: "⚠ " + pl.error }));
            else if (pl) {
                for (const e of pl.plan) {
                    lines.push(el("div", { text: `第${e.index + 1}段　${fmtT(v.start + e.story_start / VE_FPS)} → ${fmtT(v.start + (e.story_end - e.tail_cut) / VE_FPS)}　采样 ${e.sampled} 帧（${(e.sampled / VE_FPS).toFixed(1)} 秒）　${e.join}${e.head ? `（衔接头 ${e.head} 帧）` : ""}` }));
                }
                for (const w of pl.warns) lines.push(el("div", { class: "w", text: "⚠ " + w }));
                lines.push(el("div", { class: "hint", text: `成片固定 24 帧/秒；分割点会吸附到模型能用的帧数（17k+5），上面显示的是实际分法。` }));
            }
            kids.push(el("div", { class: "plan" }, lines));
        }
        kids.push(el("div", { class: "sec", text: "范围与输出设置" }));
        kids.push(
            el("div", { class: "vegrid" }, [
                numField("起始秒", () => v.start, (x) => (v.start = Math.max(0, x || 0)), { step: "0.01", min: "0" }),
                numField("结束秒（0=到结尾）", () => v.end, (x) => (v.end = Math.max(0, x || 0)), { step: "0.01", min: "0" }),
                numField("最长边（宽高都填0时用）", () => ve.long_edge, (x) => (ve.long_edge = Math.max(64, Math.round(x) || 1344)), { step: "32", min: "64" }),
                numField("宽度（0=自动）", () => ve.width, (x) => (ve.width = Math.max(0, Math.round(x) || 0)), { step: "32", min: "0" }),
                numField("高度（0=自动）", () => ve.height, (x) => (ve.height = Math.max(0, Math.round(x) || 0)), { step: "32", min: "0" }),
                selField("音频", VE_AUDIO, () => ve.audio, (x) => (ve.audio = x)),
            ]),
        );
        kids.push(el("div", { class: "hint", text: "音频：静音（默认）/ 保留原音频（成片声音取自源视频）/ 模型生成（需把音频 VAE 接到「H3 视频联合解码」）。" }));

        // ---- 遮罩
        kids.push(el("div", { class: "sec", text: "遮罩（白色 = 要换掉的人/物）" }));
        const pickMask = el("input", { type: "file", accept: "video/*", style: "display:none" });
        pickMask.addEventListener("change", async () => {
            const f = pickMask.files[0];
            pickMask.value = "";
            if (!f) return;
            try {
                ve.mask_file = await uploadFile(f);
                changed(true);
            } catch (err) {
                alert(`${f.name}：${err.message}`);
            }
        });
        kids.push(
            el("div", { class: "bar" }, [
                el("button", { text: "上传已有遮罩视频", onclick: () => pickMask.click() }),
                pickMask,
                ve.mask_file ? el("button", { class: "danger", text: "清除遮罩", onclick: () => { ve.mask_file = ""; changed(true); } }) : null,
                el("span", { class: "hint", text: ve.mask_file ? `当前遮罩：${ve.mask_file}` : "没有遮罩：「遮罩」口输出空，需要自己接遮罩" }),
            ]),
        );
        if (ve.mask_file) kids.push(el("video", { src: viewUrl(ve.mask_file), controls: "controls", muted: "muted", preload: "metadata", style: "max-height:160px" }));
        const samSel = el("select", {}, [el("option", { value: "", text: "读取模型列表中…" })]);
        loadSamModels().then((list) => {
            samSel.textContent = "";
            if (!list.length) samSel.appendChild(el("option", { value: "", text: "checkpoints 里没有 SAM3 模型" }));
            for (const m of list) samSel.appendChild(el("option", { value: m, text: m }));
            if (list.includes(ve.sam_model)) samSel.value = ve.sam_model;
            else if (list.length) {
                ve.sam_model = list[0];
                samSel.value = list[0];
                save();
            }
        });
        samSel.addEventListener("change", () => { ve.sam_model = samSel.value; save(); });
        const msgBox = el("span", { class: "hint", text: maskMsg });
        const genBtn = el("button", { text: maskBusy ? "生成中…" : "生成遮罩（SAM3）" });
        genBtn.disabled = maskBusy;
        genBtn.addEventListener("click", async () => {
            if (maskBusy) return;
            if (!ve.sam_text.trim()) return alert("请填写要遮住什么，例如 person。");
            if (!ve.sam_model) return alert("没有可用的 SAM3 模型，请先把模型放进 models/checkpoints。");
            maskBusy = true;
            genBtn.disabled = true;
            genBtn.textContent = "生成中…";
            const say = (t) => { maskMsg = t; msgBox.textContent = t; };
            say("提交中…");
            try {
                const rel = await runMaskJob(state.video.file, { text: ve.sam_text.trim(), model: ve.sam_model, threshold: ve.sam_threshold, refine: ve.sam_refine }, say);
                ve.mask_file = rel;
                maskMsg = `已生成：${rel}`;
                changed(true);
            } catch (err) {
                maskMsg = err.message;
                alert(err.message);
            } finally {
                maskBusy = false;
                render();
            }
        });
        kids.push(
            el("div", { class: "vegrid" }, [
                (() => {
                    const inp = el("input", { type: "text", value: ve.sam_text });
                    inp.addEventListener("change", () => { ve.sam_text = inp.value; save(); });
                    return el("label", {}, ["遮罩对象（英文，如 person）", inp]);
                })(),
                numField("检测阈值", () => ve.sam_threshold, (x) => (ve.sam_threshold = Math.min(0.99, Math.max(0.01, x || 0.5))), { step: "0.01", min: "0.01", max: "0.99" }),
                numField("细化迭代（0~5）", () => ve.sam_refine, (x) => (ve.sam_refine = Math.min(5, Math.max(0, Math.round(x) || 0))), { step: "1", min: "0", max: "5" }),
                el("label", {}, ["SAM3 模型", samSel]),
            ]),
        );
        kids.push(el("div", { class: "bar" }, [genBtn, msgBox]));
        kids.push(el("div", { class: "hint", text: "逐帧检测，一次只处理一帧，内存占用不随视频变长。生成一次存成视频，之后一直复用；视频有多个符合的人时都会被遮住。" }));
        kids.push(
            el("div", { class: "vegrid" }, [
                selField("遮罩形状", ["边界框", "轮廓"], () => ve.mask_shape, (x) => (ve.mask_shape = x)),
                numField("向外扩（像素）", () => ve.mask_grow, (x) => (ve.mask_grow = Math.max(0, Math.round(x) || 0)), { step: "1", min: "0" }),
                numField("遮罩阈值", () => ve.mask_threshold, (x) => (ve.mask_threshold = Math.min(0.99, Math.max(0.01, x || 0.5))), { step: "0.01", min: "0.01", max: "0.99" }),
            ]),
        );
        kids.push(el("div", { class: "hint", text: "边界框：每帧取遮罩的外接矩形再外扩，新人物体型不同也放得下，不留灰边（推荐）；轮廓：只取遮罩本身。改这里不用重新生成遮罩。" }));
        return kids;
    };

    let videoEl = null;
    const render = () => {
        const scrollTop = root.scrollTop;
        root.textContent = "";
        lastFocus = null;
        tabEls = [];
        if (active >= state.segments.length) active = state.segments.length - 1;
        if (active < 0) active = 0;

        /* ---- 公共区域（可折叠） ---- */
        const c = state.common;
        const isFL = mode() === MODE_FL;
        const sum = el("span", {
            class: "sum",
            text: isFL ? "" : `图 ${c.images.length}　视频 ${c.videos.length}　音频 ${c.audios.length}`,
        });
        commonTA = promptBox(() => c.prompt, (v) => (c.prompt = v), "全局提示词：固定的人物外貌、画风等，会放在每段提示词前面");
        const padKids = [commonTA];
        const linked = commonLinked();
        if (linked) {
            commonTA.disabled = true;
            padKids.push(el("div", { class: "hint", text: "全局提示词已由连线「全局提示词」提供，这里写的内容不使用。" }));
        }
        if (isFL) {
            padKids.push(el("div", { class: "hint", text: "首尾帧模式不使用参考图 / 视频 / 音频，全局区域只有提示词。" }));
        } else {
            padKids.push(mediaSection(c, "c", true));
            padKids.push(el("div", { class: "hint", text: "全局素材在每一段里都占最前面的编号。点素材上的 @ 可把引用插到光标所在的提示词里。" }));
        }
        const details = el("details", { class: "common" }, [
            el("summary", {}, [el("span", { text: "全局区域（所有段通用：人物 / 音色 / 画风）" }), sum]),
            el("div", { class: "pad" }, padKids),
        ]);
        if (state.ui?.common_open !== false) details.setAttribute("open", "open");
        details.addEventListener("toggle", () => {
            if (!state.ui) state.ui = { common_open: true };
            if (state.ui.common_open !== details.open) {
                state.ui.common_open = details.open; // 记进工作流数据：切换工作流、重开后保持折叠状态
                save();
            }
        });
        root.appendChild(details);

        if (isVE()) {
            const vd = el("details", { class: "ve" }, [
                el("summary", {}, [el("span", { text: "视频编辑：源视频 · 分割 · 遮罩 · 输出设置" }), el("span", { class: "sum", text: `${state.segments.length} 段` })]),
                el("div", { class: "pad" }, veSection()),
            ]);
            if (state.ui?.ve_open !== false) vd.setAttribute("open", "open");
            vd.addEventListener("toggle", () => {
                if (!state.ui) state.ui = { common_open: true };
                if (state.ui.ve_open !== vd.open) {
                    state.ui.ve_open = vd.open;
                    save();
                }
            });
            root.appendChild(vd);
        }

        /* ---- 分段标签页 ---- */
        const tabs = el("div", { class: "tabs" });
        state.segments.forEach((_seg, i) => {
            const warn = el("span", { class: "warn" });
            let label = `第${i + 1}段`;
            if (isVE()) {
                const pl = curPlan();
                if (pl?.plan[i]) label += ` ${fmtT(state.video.start + pl.plan[i].story_start / VE_FPS)}`;
            }
            const t = el("button", { class: "tab" + (i === active ? " on" : ""), onclick: () => {
                active = i;
                render();
            } }, [label, warn]);
            tabs.appendChild(t);
            tabEls.push({ i, warn });
        });
        if (!isVE()) tabs.appendChild(
            el("button", {
                class: "tab add",
                text: "＋ 添加一段",
                onclick: () => {
                    state.segments.push(newSegment());
                    active = state.segments.length - 1;
                    changed(true);
                },
            }),
        );
        root.appendChild(tabs);
        statusBox = el("div", { class: "status" });
        root.appendChild(statusBox);
        refreshStatus();

        /* ---- 当前段 ---- */
        const i = active;
        const seg = state.segments[i];
        const move = (d) => {
            const j = i + d;
            if (j < 0 || j >= state.segments.length) return;
            [state.segments[i], state.segments[j]] = [state.segments[j], state.segments[i]];
            active = j;
            changed(true);
        };
        const useCommon = el("input", { type: "checkbox" });
        useCommon.checked = seg.use_common;
        useCommon.addEventListener("change", () => {
            seg.use_common = useCommon.checked;
            changed(false);
        });
        root.appendChild(
            el("div", { class: "bar" }, [
                isVE() ? null : el("button", { text: "◀ 前移", onclick: () => move(-1) }),
                isVE() ? null : el("button", { text: "后移 ▶", onclick: () => move(1) }),
                isVE() ? null : el("button", {
                    text: "复制本段",
                    onclick: () => {
                        state.segments.splice(i + 1, 0, JSON.parse(JSON.stringify(seg)));
                        active = i + 1;
                        changed(true);
                    },
                }),
                isVE()
                    ? (state.segments.length > 1
                        ? el("button", {
                            class: "danger",
                            text: i > 0 ? "并入上一段" : "并入下一段",
                            title: "删掉分割点，把这一段和相邻段合成一段（保留相邻段的提示词和素材）",
                            onclick: () => (i > 0 ? removeCut(i - 1) : (state.cuts.shift(), state.segments.shift(), changed(true))),
                        })
                        : null)
                    : el("button", {
                        class: "danger",
                        text: "删除本段",
                        onclick: () => {
                            if (state.segments.length <= 1) return alert("至少保留一段。");
                            state.segments.splice(i, 1);
                            changed(true);
                        },
                    }),
                el("span", { class: "sp" }),
                el("label", {}, [useCommon, "使用全局提示词"]),
            ]),
        );
        root.appendChild((segTA = promptBox(() => seg.prompt, (v) => (seg.prompt = v), `第 ${i + 1} 段提示词：只写这一段的动作、场景`)));

        if (isFL) {
            root.appendChild(el("div", { class: "sec", text: "首帧 / 尾帧（可以只填一个；输出口「首帧」「尾帧」，接官方图生视频）" }));
            root.appendChild(singleSlot(seg, "first", "首帧"));
            root.appendChild(singleSlot(seg, "last", "尾帧"));
        } else {
            const commonCount = c.images.length + c.videos.length + c.audios.length;
            if (commonCount) {
                root.appendChild(el("div", { class: "sec", text: "全局素材（点 @ 引用；要增删请到上面的全局区域）" }));
                root.appendChild(mediaSection(c, "c", false));
            }
            root.appendChild(el("div", { class: "sec", text: "本段额外素材（只给这一段用，编号接在公共素材后面）" }));
            root.appendChild(mediaSection(seg, "s", true));
        }
        if (!isVE()) root.appendChild(guideSection(seg));

        errBox = el("div", { class: "err" });
        root.appendChild(errBox);

        refreshLive();
        root.scrollTop = scrollTop;
    };

    const load = () => {
        let data = null;
        try {
            data = dataWidget.value ? JSON.parse(dataWidget.value) : null;
        } catch (_) {
            data = null; // 手改坏了：用默认状态，不覆盖文本，直到用户在编辑器里操作
        }
        state = normalizeState(data);
        active = 0;
        render();
    };

    return {
        root,
        load,
        getState: () => state,
        validDone,
        setProgress,
        getTask: () => state.progress?.task || "",
        rerender: () => render(),
        refreshLive: () => refreshLive(),
        setRunText: (t) => {
            runText = t || "";
            refreshStatus();
        },
    };
}

/* ================================================================= 视频编辑：时间轴编辑器（原 h3_videoedit.js，现已并入本文件） */
const MAX_GUIDE = 30; // 最多 30 个引导点（与后端 MAX_GUIDE_POINTS 一致）
/** 视频编辑的输出口名字（与后端 ve_layout 一致）：缩放视频 / 原视频 / 音频 / 提示词 / 长度 / 参考图1…N / 引导（永远最后） */
const veOutNames = (maxImg) => {
    const out = ["缩放视频", "原视频", "音频", "提示词", "长度"];
    for (let r = 1; r <= Math.min(9, maxImg); r++) out.push(`参考图${r}`);
    out.push("引导");
    return out;
};
const MAX_TRACKS = 9;

const VE_CSS = `
.h3ve .vtop{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-bottom:6px}
.h3ve .vtop .nm{color:#9ab;font-size:12px}
.h3ve video{width:100%;max-height:240px;background:#000;border-radius:4px;display:block}
.h3ve .tool{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin:8px 0 6px}
.h3ve .tool input[type=number]{width:54px;background:#141414;color:#eee;border:1px solid #444;border-radius:4px;padding:2px 4px}
.h3ve .tool .sep{width:1px;height:18px;background:#444;margin:0 2px}
.h3ve .tl{position:relative;border:1px solid #3a3a3a;border-radius:4px;background:#151515;padding:4px 0;user-select:none}
.h3ve .row{display:flex;align-items:stretch}
.h3ve .lab{flex:0 0 74px;display:flex;align-items:center;justify-content:space-between;gap:2px;padding:0 4px 0 8px;color:#9ab;font-size:12px}
.h3ve .lab button{padding:0 6px!important;line-height:18px}
.h3ve .lane{position:relative;flex:1;margin-right:8px}
.h3ve .ruler{height:20px;border-bottom:1px solid #333;cursor:pointer}
.h3ve .ruler .tk{position:absolute;top:8px;bottom:0;width:1px;background:#555}
.h3ve .ruler .tt{position:absolute;top:-1px;font-size:10px;color:#888;transform:translateX(3px);pointer-events:none}
.h3ve .vlane{height:42px;margin-top:4px}
.h3ve .vlane .blk{position:absolute;top:0;bottom:0;border-radius:4px;display:flex;flex-direction:column;align-items:center;justify-content:center;color:#fff;font-size:12px;line-height:1.2;cursor:pointer;overflow:hidden;white-space:nowrap}
.h3ve .vlane .blk small{opacity:.8;font-size:10px}
.h3ve .vlane .blk.on{outline:2px solid #fff;outline-offset:-2px}
.h3ve .vlane .cut{position:absolute;top:-2px;bottom:-2px;width:11px;margin-left:-5px;cursor:ew-resize;z-index:4}
.h3ve .vlane .cut::after{content:"";position:absolute;left:4px;width:3px;top:0;bottom:0;background:#ffd24a;border-radius:2px}
.h3ve .ilane{height:38px;margin-top:4px;background:repeating-linear-gradient(90deg,#1f1f1f 0 1px,transparent 1px 100%);border-radius:4px;background-color:#1c1c1c}
.h3ve .ilane .gap{position:absolute;top:0;bottom:0;width:0;border-left:1px dashed #333}
.h3ve .clip.sel{outline:2px solid #fff;outline-offset:-2px;z-index:2}
.h3ve .clip{position:absolute;top:2px;bottom:2px;border:1px solid #6aa;border-radius:4px;background:#27424a;display:flex;align-items:center;gap:4px;overflow:hidden;cursor:grab;padding:0 14px}
.h3ve .clip.drag{cursor:grabbing;opacity:.9}
.h3ve .clip.bad{outline:2px solid #e05a4a;outline-offset:-1px;opacity:.8}
.h3ve .clip img{height:100%;aspect-ratio:1/1;object-fit:cover;border-radius:3px;pointer-events:none}
.h3ve .clip .cn{font-size:11px;color:#dff;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;pointer-events:none}
.h3ve .clip .x{position:absolute;right:14px;top:1px;font-size:11px;padding:0 4px;background:#0008;border-radius:3px;cursor:pointer;color:#fcc;z-index:2}
.h3ve .clip .rh{position:absolute;top:0;bottom:0;width:10px;cursor:ew-resize;z-index:3;background:linear-gradient(90deg,#8cc8 0 3px,transparent 3px)}
.h3ve .clip .rh.l{left:0}
.h3ve .clip .rh.r{right:0;transform:scaleX(-1)}
.h3ve .glane{height:30px;margin-top:4px;background-color:#1c1c1c;border-radius:4px;cursor:crosshair}
.h3ve .gmark{position:absolute;top:2px;bottom:2px;width:22px;margin-left:-11px;border-radius:5px;background:#6a5420;border:1px solid #e0b24a;cursor:ew-resize;z-index:3;display:flex;align-items:center;justify-content:center;color:#ffd24a;font-size:14px;line-height:1;user-select:none}
.h3ve .gmark.on{background:#e0b24a;color:#000;outline:2px solid #fff}
.h3ve .gmark.empty{background:transparent}
.h3ve .gmark.es::after{left:-1px}
.h3ve .gmark.ee::after{left:auto;right:-1px}
.h3ve .vlane .blk.lk{box-shadow:0 0 0 2px #ffd24a inset;filter:brightness(1.15)}
.h3ve .vlane .blk.hl{outline:2px solid #ffd24a;outline-offset:-2px;filter:brightness(1.25)}
.h3ve .tool button.tgl.on{background:#3d6b4f!important;color:#fff!important}
.h3ve .gmark::after{content:"";position:absolute;left:50%;top:-3px;bottom:-3px;width:1px;background:#ffd24a;pointer-events:none;opacity:.6}
.h3ve .gbox{margin-top:8px;border:1px solid #6a5a2a;border-radius:6px;padding:8px;background:#242018}
.h3ve .gbox .chips{display:flex;flex-wrap:wrap;gap:4px;margin:6px 0}
.h3ve .gbox .chip{padding:2px 8px!important;border:1px solid #6a5a2a!important;background:#2c271a!important;color:#dcb!important;border-radius:12px!important;font-size:12px}
.h3ve .gbox .chip.on{background:#e0b24a!important;color:#000!important}
.h3ve .gbox .gc{display:flex;flex-wrap:wrap;align-items:center;gap:6px;font-size:12px;color:#dcb;margin-top:4px}
.h3ve .gbox .gc input[type=number]{width:70px;background:#141414;color:#eee;border:1px solid #444;border-radius:4px;padding:2px 4px}
.h3ve .vwrap{position:relative}
.h3ve .cmp{position:absolute;left:0;right:0;top:0;bottom:44px;display:none;background:#000;z-index:3}
.h3ve .cmp .half{flex:1;position:relative;overflow:hidden;display:flex;align-items:center;justify-content:center;min-width:0}
.h3ve .cmp .half.r{cursor:ew-resize;border-left:2px solid #ffd24a;user-select:none}
.h3ve .cmp canvas,.h3ve .cmp img{max-width:100%;max-height:100%;object-fit:contain;pointer-events:none}
.h3ve .cmp .ctip{position:absolute;left:4px;top:4px;background:#000b;color:#ffd24a;font-size:12px;padding:1px 6px;border-radius:3px;pointer-events:none}
.h3ve .cmp .cl{position:absolute;left:4px;bottom:2px;background:#000b;color:#9bd;font-size:11px;padding:0 4px;border-radius:3px;pointer-events:none}
.h3ve .segbar{position:relative;height:16px;margin-top:4px;background:#111;border-radius:3px;cursor:pointer;overflow:hidden}
.h3ve .segbar .sg{position:absolute;top:0;bottom:0;font-size:10px;color:#fff;line-height:16px;text-align:center;overflow:hidden;white-space:nowrap}
.h3ve .segbar .sc{position:absolute;top:0;bottom:0;width:4px;margin-left:-2px;background:#fff;z-index:2}
.h3ve .segbar .shead{position:absolute;top:0;bottom:0;width:2px;margin-left:-1px;background:#ff5a5a;z-index:3}
.h3ve .tlwrap{overflow-x:auto;overflow-y:hidden}
.h3ve .tlin{min-width:100%}
.h3ve .lab{position:sticky;left:0;z-index:7;background:#151515}
.h3ve .ruler{height:30px}
.h3ve .ruler .fine{position:absolute;left:0;right:0;bottom:0;height:5px;background-image:linear-gradient(90deg,#777 1px,transparent 1px);pointer-events:none}
.h3ve .ruler .tk{top:12px;background:#aaa}
.h3ve .ruler .tt{top:0;color:#bbb}
.h3ve .ruler .ft{position:absolute;bottom:5px;font-size:9px;color:#8a8;transform:translateX(2px);pointer-events:none}
.h3-gtip{position:fixed;z-index:99999;width:104px;height:104px;background:#000e;border:2px solid #e0b24a;border-radius:6px;padding:2px;pointer-events:auto;cursor:zoom-in;box-shadow:0 4px 14px #000a;display:flex;align-items:center;justify-content:center}
.h3-gtip img{max-width:100%;max-height:100%;object-fit:contain;display:block}
.h3-gtip b{position:absolute;left:3px;bottom:2px;font-size:10px;color:#ffd24a;background:#000b;padding:0 3px;border-radius:2px}
.h3-rdo{position:fixed;z-index:99999;background:#000d;color:#ffd24a;font-size:12px;padding:1px 7px;border-radius:3px;pointer-events:none;white-space:nowrap;box-shadow:0 2px 8px #000a}
.h3ve .head .hf{position:absolute;top:0;left:3px;background:#c33;color:#fff;font-size:10px;padding:0 3px;border-radius:2px;white-space:nowrap}
.h3ve .head{position:absolute;top:0;bottom:0;width:2px;margin-left:-1px;background:#ff5a5a;z-index:6;pointer-events:none}
.h3ve .info{margin-top:8px;padding:6px 8px;background:#161616;border:1px dashed #444;border-radius:4px;font-size:12px;color:#9bd;white-space:pre-wrap}
.h3ve .info .w{color:#ffb86b}.h3ve .info .e{color:#ff7b7b}
.h3ve .pbox{margin-top:8px;border:1px solid #3a3a3a;border-radius:6px;padding:8px;background:#202020}
.h3ve .pmode{display:inline-flex;border:1px solid #555;border-radius:5px;overflow:hidden;margin-right:8px}
.h3ve .pmode button{border:none!important;border-radius:0!important;padding:3px 12px!important;background:#2a2a2a!important}
.h3ve .pmode button.on{background:#3d6b4f!important;color:#fff!important}
.h3ve .refs{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px;align-items:center;font-size:12px;color:#9ab}
.h3ve .refs .rc{position:relative;width:46px;height:46px;border:1px solid #4a4a4a;border-radius:4px;overflow:hidden;cursor:zoom-in}
.h3ve .refs .rc img{width:100%;height:100%;object-fit:cover}
.h3ve .refs .rc b{position:absolute;left:0;top:0;background:#000a;color:#8fd;font-size:10px;padding:0 4px}
`;
let veCssDone = false;

function buildVideoEditEditor(node, dataWidget, D) {
    const { app, el, viewUrl, uploadFile, splitPath, openViewer, hashText, vePlan, veJoins, veTotalFrames, JOINS, VE_FPS, injectCss } = D;
    injectCss();
    if (!veCssDone) {
        veCssDone = true;
        const s = document.createElement("style");
        s.textContent = VE_CSS;
        document.head.appendChild(s);
    }
    const COLORS = ["#3d6b4f", "#4a5f8a", "#8a5a3d", "#6a4a8a", "#3d7a80", "#8a3d5a", "#6a7a3d", "#5a5a5a"];

    const mkSeg = () => ({ prompt: "", use_common: false, join: null, first: null, last: null, guides: [], images: [], videos: [], audios: [] });
    const blank = () => ({
        version: 1, common: { prompt: "", images: [], videos: [], audios: [] }, segments: [mkSeg()],
        video: { file: "", start: 0, end: 0, duration: 0 }, cuts: [],
        ve: { head: 22, join: "硬切" },
        vtracks: [[]], pmode: "global", gprompt: "", lprompts: [""], gpoints: [],
        progress: { done: 0, fps: [], task: "" }, ui: {},
    });
    let st = blank();
    let active = 0;
    let runText = "";
    let videoEl = null;
    let videoFile = "";
    let statusBox = null;
    let headEl = null;
    let stopAt = null;
    let raf = 0;
    let dragRaf = 0;
    let gsel = null; // 选中的引导点（对象引用）
    let selKind = "seg"; // 最后点的是什么："seg" 片段 / "guide" 引导点 / "clip" 图片轨上的图片（Del 删除的就是它）
    // 选中的图片轨图片块：记「第几条轨 + 文件 + 开始段」。fixTrack 每次都会复制对象，所以不能只记对象本身
    let selClip = null;
    const clipIs = (ti, c) => !!selClip && selClip.ti === ti && selClip.file === c.file && selClip.a === c.a;
    const findSelClip = () => {
        const tr = selClip && st.vtracks[selClip.ti];
        return tr ? tr.find((c) => c.file === selClip.file && c.a === selClip.a) || null : null;
    };
    let zoom = 1;
    let zoomCenter = null;
    let cmpOn = false;
    let headFrame = null;
    let ovl = null, ovlImg = null, ovlCvs = null, ovlTip = null, ovlSrc = "";
    let rate = 1;            // 播放速度
    let segLocked = false;   // 点了视频轨上的片段 = 选定：预览跳到这一段开头，播放只播这一段；点别处自动取消
    let lockIdx = -1;
    let loopSeg = false;     // 循环播放当前这一段
    let hlSeg = -1;          // 拖引导点时，高亮它所属的那一段
    let restoring = false;   // 撤销/重做过程中不记录
    let lastSnap = null;     // 上一次记录的结构（分割、图片轨、引导点）
    const undoStack = [], redoStack = [];

    const root = el("div", { class: "h3ms h3ve" });
    // 可以拿到键盘焦点：点引导点 / 图片块时把焦点交给编辑器，Del 才不会打进刚才的文本框
    root.tabIndex = -1;
    root.style.outline = "none";
    const focusRoot = () => {
        const a = document.activeElement;
        if (a && a !== root && (a.tagName === "TEXTAREA" || a.tagName === "INPUT") && a.blur) a.blur();
        try { root.focus({ preventScroll: true }); } catch (_) {}
    };
    const topBox = el("div");
    const tlBox = el("div");
    const toolHost = el("div");
    const tlHost = el("div");
    tlBox.append(toolHost, tlHost);
    const guideBox = el("div");
    const panelBox = el("div");
    const infoBox = el("div");
    const infoDetail = el("div");
    const segBar = el("div", { class: "segbar" });
    const segHead = el("div", { class: "shead" });
    statusBox = el("div", { class: "status" });
    infoBox.append(infoDetail, statusBox);
    root.append(topBox, tlBox, guideBox, panelBox, infoBox);

    /* ---------------------------------------------------------------- 数据 */
    const parse = (data) => {
        const s = blank();
        if (!data || typeof data !== "object") return s;
        const v = data.video && typeof data.video === "object" ? data.video : {};
        s.video = { file: String(v.file || "").replace(/\\/g, "/").trim(), start: 0, end: 0, duration: Math.max(0, Number(v.duration) || 0) };
        s.cuts = (Array.isArray(data.cuts) ? data.cuts : []).map(Number).filter(Number.isFinite).sort((a, b) => a - b);
        const n = s.cuts.length + 1;
        s.pmode = data.pmode === "local" ? "local" : "global";
        s.gprompt = String(data.gprompt ?? "");
        s.lprompts = Array.from({ length: n }, (_, i) => String(data.lprompts?.[i] ?? ""));
        s.segments = Array.from({ length: n }, (_, i) => {
            const g = mkSeg();
            g.join = "硬切"; // 视频编辑固定硬切
            return g;
        });
        const tracks = [];
        for (const tr of Array.isArray(data.vtracks) ? data.vtracks : []) {
            const clips = [];
            for (const c of Array.isArray(tr) ? tr : []) {
                const file = String(c?.file || "").replace(/\\/g, "/").trim();
                const a = parseInt(c?.a, 10);
                const b = parseInt(c?.b, 10);
                if (file && Number.isInteger(a) && Number.isInteger(b) && a >= 0 && b >= a && b < n) clips.push({ file, a, b });
            }
            tracks.push(fixTrack(clips));
        }
        s.vtracks = tracks.length ? tracks.slice(0, MAX_TRACKS) : [[]];
        const rawG = Array.isArray(data.gpoints) ? data.gpoints : Array.isArray(data.gtracks) ? data.gtracks : []; // 兼容 v7 的 gtracks
        s.gpoints = rawG.slice(0, MAX_GUIDE).map((g) => ({
            file: String(g?.file || "").replace(/\\/g, "/").trim(), f: Math.max(0, parseInt(g?.f, 10) || 0),
        }));
        const vd = data.ve && typeof data.ve === "object" ? data.ve : {};
        s.ve = { head: snapHead(vd.head), join: ["引导", "复制", "图片引导"].includes(vd.join) ? vd.join : "硬切" };
        const pr = data.progress || {};
        s.progress = { done: Math.max(0, parseInt(pr.done, 10) || 0), fps: Array.isArray(pr.fps) ? pr.fps.map(String) : [], task: typeof pr.task === "string" ? pr.task : "" };
        return s;
    };

    /** 同一条轨道里不能重叠：后面的和前面重叠就合并（同一张图）或丢掉 */
    function fixTrack(track) {
        const t = track.slice().sort((x, y) => x.a - y.a);
        const out = [];
        for (const c of t) {
            const p = out[out.length - 1];
            if (p && c.a <= p.b) {
                if (p.file === c.file) p.b = Math.max(p.b, c.b);
            } else out.push({ ...c });
        }
        return out;
    }
    const mapClips = (fn) => {
        st.vtracks = st.vtracks.map((tr) => fixTrack(tr.map((c) => {
            const [a, b] = fn(c.a, c.b);
            return { file: c.file, a, b };
        }).filter((c) => c.b >= c.a)));
    };

    /** 把图片轨 / 提示词换算成后端认识的 segments */
    const derive = () => {
        const n = st.segments.length;
        while (st.lprompts.length < n) st.lprompts.push("");
        st.lprompts.length = n;
        for (let i = 0; i < n; i++) {
            const seg = st.segments[i];
            seg.images = [];
            for (const tr of st.vtracks) for (const c of tr) if (c.a <= i && i <= c.b) seg.images.push({ file: c.file });
            seg.use_common = false;
            seg.join = "硬切";
            const loc = String(st.lprompts[i] || "");
            seg.prompt = st.pmode === "local" && loc.trim() ? loc : st.gprompt;
        }
    };

    const totalT = () => {
        const d = st.video.duration;
        return d > 0 ? veTotalFrames(st) / VE_FPS : 0;
    };
    const curPlan = () => (totalT() > 0 ? vePlan(st, veTotalFrames(st)) : null);
    /** 每个分段的起止时间点（n+1 个）：规划成功就用实际吸附后的位置 */
    const bounds = (pl) => {
        const T = totalT();
        const p = pl === undefined ? curPlan() : pl;
        const b = [0];
        if (p && p.plan.length === st.segments.length) p.plan.slice(0, -1).forEach((e) => b.push(e.story_end / VE_FPS));
        else st.cuts.forEach((c) => b.push(c));
        b.push(T);
        return b;
    };
    /** 分割点吸附到模型的帧网格（规划成功时，把分割点改成实际位置） */
    const snapCuts = () => {
        if (!(totalT() > 0)) return;
        const r = vePlan(st, veTotalFrames(st));
        if (!r.error && r.plan.length === st.segments.length) r.plan.slice(0, -1).forEach((e, i) => { st.cuts[i] = e.story_end / VE_FPS; });
    };

    const outNames = () => {
        let maxImg = 0; // 没有参考图就不显示参考图输出口
        for (const s of st.segments) maxImg = Math.max(maxImg, s.images.length);
        return veOutNames(maxImg);
    };
    const syncOutputs = () => {
        if (!Array.isArray(node.outputs) || !node.addOutput || !node.removeOutput) return;
        if (app.configuringGraph && !node.__h3Configured) return;  // 同上：载入工作流期间先不动输出口
        const names = outNames();
        const want = names.length;
        let touched = false;
        // 「引导」口永远在最后：参考图张数变化时它的位置会变，先记下它接着的线，改完再接回新的位置
        let moved = null;
        const oldIdx = node.outputs.length - 1;
        const oldLast = node.outputs[oldIdx];
        if (oldLast && oldLast.name === "引导" && want - 1 !== oldIdx && Array.isArray(oldLast.links) && oldLast.links.length) {
            try {
                moved = oldLast.links.map((id) => {
                    const l = app.graph?.links?.[id];
                    const tn = l ? app.graph.getNodeById(l.target_id) : null;
                    return tn ? { node: tn, slot: l.target_slot } : null;
                }).filter(Boolean);
                node.disconnectOutput?.(oldIdx);
            } catch (_) { moved = null; }
        }
        while (node.outputs.length > want) { node.removeOutput(node.outputs.length - 1); touched = true; }
        while (node.outputs.length < want) {
            node.addOutput(names[node.outputs.length], "*");
            touched = true;
        }
        for (let i = 0; i < node.outputs.length; i++) {
            const nm = names[i];
            const o = node.outputs[i];
            if (o.name !== nm || o.label !== nm || (o.localized_name !== undefined && o.localized_name !== nm)) {
                o.name = nm; o.label = nm;
                if (o.localized_name !== undefined) o.localized_name = nm;
                touched = true;
            }
        }
        if (moved && moved.length) {
            for (const m of moved) { try { node.connect(want - 1, m.node, m.slot); } catch (_) {} }
        }
        if (touched) app.graph?.setDirtyCanvas?.(true, true);
    };

    /** 重叠帧数不在本节点设置：读工作流里「H3 潜空间前缀复制」节点的「重叠帧数」（没有就 22）。和后端 h3t_overlap 同一规则。 */
    const graphHead = () => {
        try {
            for (const n of liveNodes(PREFIX_NODE)) {
                const v = getW(n, "重叠帧数")?.value;
                if (Number.isInteger(v) && v > 0) return snapHead(v);
            }
        } catch (_) {}
        return 22;
    };
    /** 衔接方式以「H3 潜空间前缀复制」节点上的下拉框为准（和后端 h3t_prefix_join 同一规则）；工作流里没有这个节点才用本节点的「段间衔接」 */
    const graphJoin = () => {
        try {
            for (const n of liveNodes(PREFIX_NODE)) {
                const v = getW(n, "衔接方式")?.value;
                if (JOIN_OPTIONS.includes(v)) return v;
            }
        } catch (_) {}
        return null;
    };
    /** 每次保存都同步进时间线数据（后端也会再覆盖一次） */
    const syncSettings = () => {
        const gj = graphJoin();
        st.ve.join = gj || "硬切"; // 没有前缀复制节点：没有衔接方式可选，每段独立
        st.ve.head = graphHead();
    };
    // 前缀复制节点的「重叠帧数」改了，时间轴规划要跟着变：每秒看一次
    const headTimer = setInterval(() => {
        try {
            if (node.graph === null) { clearInterval(headTimer); return; }
            if (getW(node, MODE_WIDGET)?.value !== MODE_VE) return;
            if (graphHead() !== st.ve.head || (graphJoin() && graphJoin() !== st.ve.join)) commit();
        } catch (_) {}
    }, 1000);
    const save = () => {
        syncSettings();
        derive();
        const text = JSON.stringify(st);
        if (dataWidget.value !== text) {
            dataWidget.value = text;
            try { dataWidget.callback?.(text); } catch (_) {}
            app.graph?.setDirtyCanvas?.(true, true);
        }
        syncOutputs();
    };
    const mpVal = () => Math.max(0, Math.round(Number(node.widgets?.find((w) => w.name === "参考视频最大边长")?.value) || 0));
    const evenR = (v) => Math.max(2, Math.round(v / 2) * 2);
    const genSize = () => {  // 和后端 fit_edge_size 同一规则：0=原样；否则长边=设定值，等比，宽高取偶数
        const w = videoEl?.videoWidth, h = videoEl?.videoHeight;
        if (!w || !h) return null;
        const k = mpVal() > 0 ? mpVal() / Math.max(w, h) : 1;
        return [evenR(w * k), evenR(h * k)];
    };
    /** 引导点（整条视频的第 f 帧）落在哪一段：返回规划里的那一项；规划失败或超出范围返回 null */
    const guideSeg = (f) => {
        const p = curPlan();
        if (!p || p.error) return null;
        const total = veTotalFrames(st);
        if (f < 0 || f >= total) return null;
        return p.plan.find((e) => e.story_start <= f && f < e.story_end) || null;
    };
    const fp = (i) => hashText(JSON.stringify([st.segments[i]?.images, st.segments[i]?.prompt, veJoins(st)[i], veJoins(st)[i] === "硬切" ? 0 : snapHead(st.ve.head), st.video.file, st.cuts,
        Number(mpVal()), i === st.segments.length - 1 ? "last" : "",
        st.gpoints.filter((g) => g.file && guideSeg(g.f)?.index === i).map((g) => [g.file, g.f])]));
    const validDone = () => {
        const pr = st.progress || { done: 0, fps: [] };
        let n = 0;
        while (n < pr.done && n < st.segments.length && pr.fps[n] === fp(n)) n++;
        return n;
    };
    const refreshStatus = () => {
        if (!statusBox) return;
        statusBox.textContent = "";
        const done = validDone();
        if (runText) statusBox.appendChild(el("span", { text: runText }));
        else if (done > 0 && done < st.segments.length) statusBox.appendChild(el("span", { text: `进度：已完成 ${done}/${st.segments.length} 段（要继续：把「输出第几段」设为 ${done + 1}，打开「到最后一段」）` }));
    };
    const setProgress = (done, task) => {
        derive();
        const fps = [];
        for (let j = 0; j < done && j < st.segments.length; j++) fps.push(fp(j));
        st.progress = { done: fps.length, fps, task: task !== undefined ? task : (st.progress?.task || "") };
        save();
        refreshStatus();
    };

    /** 任何结构变化都走这里：吸附分割点 → 写回数据 → 重画时间轴和下面的面板 */
    const commit = () => {
        derive();
        snapCuts();
        const tf = totalT() > 0 ? veTotalFrames(st) : 0;
        if (tf > 0) for (const g of st.gpoints) g.f = Math.min(Math.max(0, g.f), tf - 1);
        st.gpoints.sort((a, b) => a.f - b.f);
        if (gsel && !st.gpoints.includes(gsel)) gsel = null;
        const snap = snapNow();
        if (!restoring && lastSnap !== null && snap !== lastSnap) {
            undoStack.push(lastSnap);
            if (undoStack.length > 100) undoStack.shift();
            redoStack.length = 0;
        }
        lastSnap = snap;
        save();
        renderTimeline();
        renderPanel();
        renderSegBar();
    };

    /* ---------------------------------------------------------------- 撤销 / 重做（分割、图片轨、引导点） */
    const snapNow = () => JSON.stringify({
        cuts: st.cuts, n: st.segments.length, lp: st.lprompts, vt: st.vtracks,
        gp: st.gpoints.map((g) => ({ file: g.file, f: g.f })),
    });
    const applySnap = (text) => {
        const o = JSON.parse(text);
        st.cuts = o.cuts;
        st.segments = Array.from({ length: o.n }, () => { const g = mkSeg(); g.join = "硬切"; return g; });
        st.lprompts = o.lp;
        st.vtracks = o.vt;
        st.gpoints = o.gp;
        gsel = null;
        active = Math.min(active, o.n - 1);
        restoring = true;
        try { commit(); } finally { restoring = false; }
    };
    const undo = () => {
        if (!undoStack.length) return false;
        redoStack.push(snapNow());
        applySnap(undoStack.pop());
        return true;
    };
    const redo = () => {
        if (!redoStack.length) return false;
        undoStack.push(snapNow());
        applySnap(redoStack.pop());
        return true;
    };

    /* ---------------------------------------------------------------- 编辑动作 */
    const fmtT = (s) => {
        const m = Math.floor(s / 60);
        return `${m}:${(s - m * 60).toFixed(2).padStart(5, "0")}`;
    };
    const segAt = (t, bd) => {
        let k = 0;
        for (let i = 0; i < st.segments.length; i++) if (t >= bd[i] - 1e-6) k = i;
        return k;
    };
    const addCutAt = (t) => {
        const T = totalT();
        if (!T) return alert("先上传视频。");
        if (!(t > 0.1 && t < T - 0.1)) return alert(`分割位置必须在 0.1 ~ ${(T - 0.1).toFixed(1)} 秒之间。`);
        const bd = bounds();
        if (bd.some((x) => Math.abs(x - t) < 0.1)) return alert("这里已经有分割了。");
        const k = segAt(t, bd);
        st.cuts.splice(k, 0, t);
        st.segments.splice(k + 1, 0, mkSeg());
        st.lprompts.splice(k + 1, 0, st.lprompts[k] || "");
        mapClips((a, b) => [a > k ? a + 1 : a, b >= k ? b + 1 : b]);
        active = k + 1;
        commit();
    };
    const removeSplit = () => {
        const n = st.segments.length;
        if (n < 2) return;
        const b = active === 0 ? 0 : active - 1; // 第 1 段并入下一段，其余并入上一段
        st.cuts.splice(b, 1);
        st.segments.splice(b + 1, 1);
        st.lprompts[b] = String(st.lprompts[b] || "").trim() ? st.lprompts[b] : (st.lprompts[b + 1] || "");
        st.lprompts.splice(b + 1, 1);
        mapClips((a, c) => [a <= b ? a : a - 1, c <= b ? c : c - 1]);
        active = b;
        commit();
    };
    const clearSplits = () => {
        st.cuts = [];
        st.segments = [st.segments[0] || mkSeg()];
        st.lprompts = [st.lprompts[0] || ""];
        mapClips(() => [0, 0]);
        active = 0;
        commit();
    };
    const equalSplit = (nRaw) => {
        const T = totalT();
        if (!T) return alert("先上传视频。");
        const N = Math.max(1, Math.min(40, parseInt(nRaw, 10) || 1));
        const oldN = st.segments.length;
        st.cuts = Array.from({ length: N - 1 }, (_, k) => (T * (k + 1)) / N);
        st.segments = Array.from({ length: N }, (_, i) => {
            const g = mkSeg();
            g.join = "硬切";
            return g;
        });
        while (st.lprompts.length < N) st.lprompts.push("");
        st.lprompts.length = N;
        mapClips((a, b) => [Math.min(a, N - 1), b === oldN - 1 ? N - 1 : Math.min(b, N - 1)]);
        active = Math.min(active, N - 1);
        commit();
    };

    const pickFile = (accept, onFile) => {
        const input = el("input", { type: "file", accept, style: "display:none" });
        input.addEventListener("change", async () => {
            const f = input.files[0];
            input.value = "";
            if (f) await onFile(f);
        });
        return input;
    };
    const addImage = async (trackIdx, file) => {
        let rel;
        try { rel = await uploadFile(file); } catch (err) { return alert(`${file.name}：${err.message}`); }
        const tr = st.vtracks[trackIdx];
        const free = (i) => !tr.some((c) => c.a <= i && i <= c.b);
        let seg = free(active) ? active : -1;
        for (let i = 0; seg < 0 && i < st.segments.length; i++) if (free(i)) seg = i;
        if (seg < 0) return alert("这条图片轨已经被占满了：请换一条轨道，或点「＋ 图片轨」再加一条。");
        tr.push({ file: rel, a: seg, b: seg });
        st.vtracks[trackIdx] = fixTrack(tr);
        commit();
    };

    const curFrame = () => (videoEl ? Math.max(0, Math.floor(videoEl.currentTime * VE_FPS + 1e-3)) : 0);
    const frameTime = (f) => (f + 0.5) / VE_FPS; // 定位到帧的中间，避免浮点误差落到前一帧
    const totalFrames = () => (totalT() > 0 ? veTotalFrames(st) : 0);
    const addGuidePoint = async (file, frameOverride) => {
        if (st.gpoints.length >= MAX_GUIDE) return alert(`最多 ${MAX_GUIDE} 个引导点。`);
        if (frameOverride !== undefined && st.gpoints.some((g) => g.f === frameOverride)) return alert(`第 ${frameOverride} 帧已经有引导点了，可以选中它再「换图」。`);
        let rel;
        try { rel = await uploadFile(file); } catch (err) { return alert(`${file.name}：${err.message}`); }
        const p = curPlan();
        let f = frameOverride !== undefined ? frameOverride : (videoEl ? curFrame() : (p && !p.error && p.plan[active] ? p.plan[active].story_start : 0));
        const tf = totalFrames();
        if (tf > 0) f = Math.min(f, tf - 1);
        while (st.gpoints.some((g) => g.f === f) && f < (tf || 1e9) - 1) f++;
        const g = { file: rel, f };
        st.gpoints.push(g);
        gsel = g;
        commit();
        if (videoEl) seekTo(frameTime(g.f));
        updateCompare();
    };
    /** 双击引导点：对比画面（左边原视频这一帧、右边引导图）开/关；再双击同一个点就关 */
    const toggleCompare = (g) => {
        if (cmpOn && gsel === g) { setCmp(false); return; }
        if (gsel !== g) selectPoint(g);
        setCmp(true);
    };
    const selectPoint = (g) => {
        gsel = g;
        selKind = "guide";
        if (g && videoEl) seekTo(frameTime(g.f));
        renderTimeline(true);
        renderGuideBox();
        updateCompare();
    };
    /** 改引导点的帧号：只更新数据，重画时间轴和引导区（拖动时不卡）；seek=true 时视频跟着定位 */
    const setPointFrame = (g, f, seek) => {
        if (!g) return;
        const tf = totalFrames();
        g.f = tf > 0 ? Math.min(Math.max(0, Math.round(f)), tf - 1) : Math.max(0, Math.round(f));
        save();
        renderTimeline(true);
        renderGuideBox();
        if (seek && videoEl) seekTo(frameTime(g.f));
        updateCompare();
    };
    const setPointImage = async (g, file) => {
        let rel;
        try { rel = await uploadFile(file); } catch (err) { return alert(`${file.name}：${err.message}`); }
        g.file = rel;
        commit();
        updateCompare();
    };
    const setCmp = (v) => {
        cmpOn = !!v;
        if (cmpOn && gsel && videoEl) seekTo(frameTime(gsel.f));
        updateCompare();
    };
    /** 对比层：开着并且视频停在选中引导点的那一帧时，左边原视频这一帧、右边引导图；右边可以左右拖动改帧号 */
    const updateCompare = () => {
        if (!ovl) return;
        const g = gsel;
        const show = cmpOn && g && g.file && videoEl && curFrame() === g.f;
        ovl.style.display = show ? "flex" : "none";
        if (!show) return;
        const url = viewUrl(g.file);
        if (ovlSrc !== url) { ovlSrc = url; ovlImg.src = url; }
        try {
            if (videoEl.videoWidth) {
                ovlCvs.width = videoEl.videoWidth;
                ovlCvs.height = videoEl.videoHeight;
                ovlCvs.getContext("2d").drawImage(videoEl, 0, 0);
            }
        } catch (_) {}
        ovlTip.textContent = `第 ${g.f} 帧（左右拖动改变所在帧）`;
    };
    const buildOverlay = () => {
        ovlCvs = el("canvas");
        ovlImg = el("img", { alt: "" });
        ovlTip = el("div", { class: "ctip" });
        const left = el("div", { class: "half l" }, [ovlCvs, el("div", { class: "cl", text: "原视频这一帧" })]);
        const right = el("div", { class: "half r", title: "左右拖动：每 8 像素移动一帧" }, [ovlImg, ovlTip, el("div", { class: "cl", text: "引导图" })]);
        right.addEventListener("mousedown", (ev) => {
            if (!gsel) return;
            ev.preventDefault();
            const g = gsel, x0 = ev.clientX, f0 = g.f;
            const move = (e) => setPointFrame(g, f0 + Math.round((e.clientX - x0) / 8), true);
            const up = () => {
                document.removeEventListener("mousemove", move);
                document.removeEventListener("mouseup", up);
                commit();
                updateCompare();
            };
            document.addEventListener("mousemove", move);
            document.addEventListener("mouseup", up);
        });
        ovl = el("div", { class: "cmp" }, [left, right]);
    };

    /* ---------------------------------------------------------------- 视频区（只在换视频时重建，保证播放不被打断） */
    const renderTop = () => {
        topBox.textContent = "";
        const v = st.video;
        const pick = pickFile("video/*", async (f) => {
            if ((st.cuts.length || st.vtracks.some((t) => t.length)) && v.file && !confirm("更换源视频会重置所有分割点，继续吗？图片轨上的图片会保留。")) return;
            try {
                const rel = await uploadFile(f);
                st.video = { file: rel, start: 0, end: 0, duration: 0 };
                st.cuts = [];
                st.segments = [st.segments[0] || mkSeg()];
                st.lprompts = [st.lprompts[0] || ""];
                mapClips(() => [0, 0]);
                active = 0;
                videoFile = "";
                save();
                renderTop();
                renderTimeline();
                renderPanel();
            } catch (err) { alert(`${f.name}：${err.message}`); }
        });
        topBox.appendChild(el("div", { class: "vtop" }, [
            el("button", { class: "addbtn", text: v.file ? "更换视频" : "＋ 上传视频", onclick: () => pick.click() }),
            pick,
            el("span", { class: "nm", text: v.file ? `${splitPath(v.file).filename}${v.duration ? "　时长 " + fmtT(v.duration) : "　读取中…"}` : "先上传要换人的视频" }),
        ]));
        if (!v.file) { videoEl = null; videoFile = ""; return; }
        if (!videoEl || videoFile !== v.file) {
            videoFile = v.file;
            videoEl = el("video", { src: viewUrl(v.file), controls: "controls", preload: "metadata" });
            videoEl.addEventListener("loadedmetadata", () => {
                const d = Math.round((videoEl.duration || 0) * 100) / 100;
                if (d && Math.abs(d - (st.video.duration || 0)) > 0.01) {
                    st.video.duration = d;
                    commit();
                    renderTop();
                } else renderTimeline(); // 读到宽高后刷新「生成尺寸」
            });
            videoEl.playbackRate = rate;
            videoEl.addEventListener("play", () => { videoEl.playbackRate = rate; onPlay(); });
            videoEl.addEventListener("ended", () => {
                if (!loopSeg) return;
                const k = segAt(Math.max(0, videoEl.currentTime - 0.05), bounds());
                videoEl.currentTime = frameTime(Math.round(bounds()[k] * VE_FPS));
                videoEl.play().catch(() => {});
            });
            videoEl.addEventListener("pause", () => { cancelAnimationFrame(raf); movePlayhead(); });
            videoEl.addEventListener("seeked", () => { movePlayhead(); updateCompare(); });
            videoEl.addEventListener("timeupdate", movePlayhead);
        }
        if (!ovl) buildOverlay();
        topBox.appendChild(el("div", { class: "vwrap" }, [videoEl, ovl]));
        renderSegBar();
    };

    /** 视频下面的分段条：每段一种颜色，分割处是粗白线，红线是播放位置，点一下定位 */
    const renderSegBar = () => {
        segBar.textContent = "";
        const T = totalT();
        if (!(T > 0)) return;
        const bd = bounds();
        const n = st.segments.length;
        for (let i = 0; i < n; i++) {
            const d = el("div", { class: "sg", text: `第${i + 1}段` });
            d.style.left = `${(bd[i] / T) * 100}%`;
            d.style.width = `${((bd[i + 1] - bd[i]) / T) * 100}%`;
            d.style.background = COLORS[i % COLORS.length];
            if (i === active) d.style.outline = "1px solid #fff";
            segBar.appendChild(d);
        }
        for (let k = 1; k < n; k++) {
            const c = el("div", { class: "sc" });
            c.style.left = `${(bd[k] / T) * 100}%`;
            segBar.appendChild(c);
        }
        segBar.appendChild(segHead);
        movePlayhead();
    };
    segBar.addEventListener("click", (ev) => {
        const T = totalT();
        if (!(T > 0)) return;
        const r = segBar.getBoundingClientRect();
        const t = Math.min(T - 0.5 / VE_FPS, Math.max(0, ((ev.clientX - r.left) / r.width) * T));
        const k = segAt(t, bounds());
        seekTo(t);
        selKind = "seg";
        if (k !== active) { active = k; renderTimeline(); renderPanel(); renderSegBar(); }
    });

    /** 刻度尺读数：浮层挂在 body 上，位置按屏幕坐标算（不被时间轴的滚动容器裁掉；靠近屏幕右边时翻到鼠标左边） */
    let rdoEl = null;
    const showRdo = (x, y, text) => {
        if (!rdoEl) { rdoEl = document.createElement("div"); rdoEl.className = "h3-rdo"; document.body.appendChild(rdoEl); }
        rdoEl.textContent = text;
        rdoEl.style.display = "block";
        const w = rdoEl.offsetWidth || 150;
        const left = x + 14 + w > window.innerWidth - 4 ? x - 14 - w : x + 14;
        rdoEl.style.left = `${Math.max(4, left)}px`;
        rdoEl.style.top = `${Math.max(4, y - 32)}px`;
    };
    const hideRdo = () => { if (rdoEl) rdoEl.style.display = "none"; };
    /** 引导点缩略图浮窗（鼠标放在标记上才出现，在标记上方） */
    let gtipEl = null, gtipTimer = 0;
    const hideGtipNow = () => { clearTimeout(gtipTimer); if (gtipEl) { gtipEl.remove(); gtipEl = null; } };
    const hideGtip = () => { clearTimeout(gtipTimer); gtipTimer = setTimeout(hideGtipNow, 220); }; // 稍等一下：给鼠标从标记移到缩略图的时间
    /** 缩略图浮窗：引导点和图片轨上的图片块共用。label = 左下角小标签；cx = 浮窗对准的横坐标（图片块很宽，对准鼠标；不给就对准标记中心） */
    const showGtip = (mk, g, label, cx) => {
        clearTimeout(gtipTimer);
        if (gtipEl && gtipEl.__g === g) return;
        hideGtipNow();
        if (!g.file) return;
        gtipEl = document.createElement("div");
        gtipEl.className = "h3-gtip";
        gtipEl.__g = g;
        gtipEl.title = "点击放大预览";
        gtipEl.append(Object.assign(document.createElement("img"), { src: viewUrl(g.file), alt: "" }), Object.assign(document.createElement("b"), { textContent: label ?? `第 ${g.f} 帧` }));
        gtipEl.addEventListener("mouseenter", () => clearTimeout(gtipTimer));
        gtipEl.addEventListener("mouseleave", hideGtip);
        gtipEl.addEventListener("click", () => { hideGtipNow(); openViewer("img", viewUrl(g.file), splitPath(g.file).filename); });
        document.body.appendChild(gtipEl);
        const r = mk.getBoundingClientRect();
        const h = gtipEl.offsetHeight || 108;
        const left = Math.min(window.innerWidth - 112, Math.max(4, (cx ?? (r.left + r.width / 2)) - 52));
        const top = r.top - h + 2 < 4 ? r.bottom - 2 : r.top - h + 2; // 和标记贴在一起（重叠 2 像素），鼠标往上移不会掉出去
        gtipEl.style.left = `${left}px`;
        gtipEl.style.top = `${top}px`;
    };

    /** 播放：没选定片段 = 从当前位置一直播到结束，不在分割处停；选定了片段（点了视频轨上的片段）= 只播这一段，播完暂停；「循环本段」= 在这一段里循环 */
    const onPlay = () => {
        const bd = bounds();
        const t = videoEl.currentTime;
        let k = null;
        if (segLocked && lockIdx >= 0 && lockIdx < bd.length - 1) k = lockIdx;
        else if (loopSeg) k = segAt(t + 0.03, bd);
        stopAt = null;
        if (k != null) {
            if (t < bd[k] - 0.02 || t >= bd[k + 1] - 0.02) videoEl.currentTime = frameTime(Math.round(bd[k] * VE_FPS)); // 不在这一段里：先回到这一段开头
            stopAt = bd[k + 1];
            if (k !== active) { active = k; renderTimeline(); renderPanel(); }
        }
        const loop = () => {
            if (!videoEl || videoEl.paused) return;
            if (stopAt != null && videoEl.currentTime >= stopAt - 0.02 && (loopSeg || stopAt < totalT() - 0.02)) {
                if (loopSeg) videoEl.currentTime = frameTime(Math.round(bd[k] * VE_FPS)); // 循环：回到本段第一帧继续放
                else {
                    videoEl.pause();
                    videoEl.currentTime = stopAt;
                    return;
                }
            }
            movePlayhead();
            raf = requestAnimationFrame(loop);
        };
        cancelAnimationFrame(raf);
        raf = requestAnimationFrame(loop);
    };
    const movePlayhead = () => {
        // 红线画在「当前帧的起点」（和刻度、帧号一致）；定位视频时仍然落在帧中间，只是画的位置取整
        const tf = totalFrames();
        const pos = videoEl && tf > 0 ? `${Math.min(100, Math.max(0, (curFrame() / tf) * 100))}%` : null;
        if (headEl && headEl.isConnected && pos) headEl.style.left = pos;
        if (headFrame && videoEl) headFrame.textContent = String(curFrame());
        if (segHead.isConnected && pos) segHead.style.left = pos;
        updateCompare();
    };
    const seekTo = (t) => {
        if (videoEl) videoEl.currentTime = Math.max(0, t);
        movePlayhead();
    };

    /* ---------------------------------------------------------------- 时间轴 */
    function renderTimeline(bodyOnly) {
        hideGtipNow();
        const oldWrap = tlHost.querySelector(".tlwrap");
        const oldScroll = oldWrap ? oldWrap.scrollLeft : 0;
        if (!bodyOnly) toolHost.textContent = "";
        tlHost.textContent = "";
        const T = totalT();
        if (!st.video.file) return;
        const pl = curPlan();
        const bd = bounds(pl);
        const n = st.segments.length;
        if (lockIdx >= n) { segLocked = false; lockIdx = -1; }
        const pct = (t) => `${(t / T) * 100}%`;

        // ---- 工具栏
        const splitN = el("input", { type: "number", min: "1", max: "40", value: String(n), title: "平均分成几段" });
        const zoomInp = el("input", { type: "range", min: "1", max: "10", step: "0.5", value: String(zoom), style: "width:110px", title: "时间轴缩放：放大后左右滚动查看，可以精确到一帧" });
        const zoomLbl = el("span", { text: `${zoom}×` });
        // 速度滑块：对数刻度，中间 = 1×，往左慢放、往右快放；0.25× 0.5× 1× 2× 4× 处有刻度，靠近自动吸附
        const SPEED_TICKS = [-2, -1, 0, 1, 2];
        const speedList = el("datalist", { id: `h3spd${Math.random().toString(36).slice(2, 8)}` }, SPEED_TICKS.map((v) => el("option", { value: String(v) })));
        const speedInp = el("input", { type: "range", min: "-2", max: "2", step: "0.01", value: String(Math.log2(rate)), style: "width:150px", title: "播放速度：中间 1×，往左慢放（0.5× / 0.25×），往右快放（2× / 4×），靠近刻度自动吸附" });
        speedInp.setAttribute("list", speedList.id);
        const speedLbl = el("span", { text: `${rate}×`, style: "min-width:34px;display:inline-block" });
        speedInp.addEventListener("input", () => {
            let v = Number(speedInp.value);
            const near = Math.round(v);
            if (Math.abs(v - near) < 0.12) { v = near; speedInp.value = String(v); }
            rate = Math.round(Math.pow(2, v) * 100) / 100;
            speedLbl.textContent = `${rate}×`;
            if (videoEl) videoEl.playbackRate = rate;
        });
        zoomInp.addEventListener("input", () => applyZoom(Number(zoomInp.value), zoomLbl));
        if (!bodyOnly) toolHost.appendChild(el("div", { class: "tool" }, [
            el("button", { text: "✂ 在播放位置分割", title: "先在上面的视频里拖到想分割的地方，再点这个", onclick: () => addCutAt(videoEl?.currentTime || 0) }),
            el("span", { text: "均分" }), splitN, el("span", { text: "段" }),
            el("button", { text: "均分", onclick: () => equalSplit(splitN.value) }),
            el("span", { class: "sep" }),
            el("button", { class: "danger", text: "删除本段分割", title: "把选中的这一段和相邻的一段合并（第 1 段并入下一段，其余并入上一段）", onclick: removeSplit }),
            el("button", { class: "danger", text: "清除全部分割", onclick: clearSplits }),
            el("span", { class: "sep" }),
            el("span", { text: "缩放" }), zoomInp, zoomLbl,
        ]));
        if (!bodyOnly) toolHost.appendChild(el("div", { class: "tool" }, [
            el("button", { text: "↶ 撤销", title: "撤销分割、图片轨、引导点的修改（Ctrl+Z；鼠标放在编辑器上时有效）", disabled: undoStack.length ? undefined : "disabled", onclick: () => { undo(); } }),
            el("button", { text: "↷ 重做", title: "重做（Ctrl+Shift+Z 或 Ctrl+Y）", disabled: redoStack.length ? undefined : "disabled", onclick: () => { redo(); } }),
            el("span", { class: "sep" }),
            el("span", { text: "速度" }), speedInp, speedList, speedLbl,
            el("button", { class: "tgl" + (loopSeg ? " on" : ""), text: "⟲ 循环本段", title: "播放时在当前这一段内循环，不往下一段走", onclick: () => { loopSeg = !loopSeg; renderTimeline(); } }),
            el("span", { class: "sep" }),
            el("button", { text: "提取", title: "把预览视频当前这一帧下载成 PNG 图片（快捷键 G）", onclick: saveFramePng }),
        ]));

        // ---- 时间轴本体
        const tl = el("div", { class: "tl" });
        const lanes = [];
        const mkRow = (label, cls, extra) => {
            const lane = el("div", { class: `lane ${cls}` });
            lanes.push(lane);
            tl.appendChild(el("div", { class: "row" }, [el("div", { class: "lab" }, [el("span", { text: label }), extra || null]), lane]));
            return lane;
        };
        // 刻度：每帧一条细线；每秒一条粗线 + 时间；放大后标出帧号
        const TF = Math.max(1, Math.round(T * VE_FPS));
        const ruler = mkRow("", "ruler");
        const laneW = Math.max(200, (tlHost.clientWidth || tlBox.clientWidth || 400) - 82) * zoom;
        const ppf = laneW / TF;
        const pps = ppf * VE_FPS;
        const kf = [1, 5, 10, 25, 50, 100, 250].find((k) => ppf * k >= 3);
        if (kf) ruler.appendChild(el("div", { class: "fine", style: `background-size:${((kf / TF) * 100).toFixed(5)}% 100%` }));
        const secStep = [1, 2, 5, 10, 30, 60, 300].find((k) => pps * k >= 6) || 600;
        const lblStep = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600].find((k) => pps * k >= 54) || 600;
        for (let sec = 0; sec <= T + 1e-6; sec += secStep) {
            ruler.appendChild(el("div", { class: "tk", style: `left:${pct(sec)}` }));
            if (sec % lblStep < 1e-6) ruler.appendChild(el("div", { class: "tt", style: `left:${pct(sec)}`, text: `${Math.floor(sec / 60)}:${String(Math.round(sec % 60)).padStart(2, "0")}` }));
        }
        if (ppf >= 6) {
            const fs = [1, 5, 10, 25, 50].find((k) => ppf * k >= 26) || 100;
            for (let f = 0; f < TF; f += fs) ruler.appendChild(el("div", { class: "ft", style: `left:${(f / TF) * 100}%`, text: String(f) }));
        }
        const frameAt = (clientX, r) => Math.min(TF - 1, Math.max(0, Math.floor(((clientX - r.left) / r.width) * TF)));
        /** 在某条轨道上按下并拖动：红线和预览视频跟着走，松开后选中红线所在的那一段 */
        const startScrub = (ev, laneEl, lock) => {
            ev.preventDefault();
            const r0 = laneEl.getBoundingClientRect(); // 按下时记下轨道位置（松开前不重画）
            let lastK = active;
            const x0 = ev.clientX;
            let moved = false;
            const go = (e) => {
                if (Math.abs(e.clientX - x0) > 3) moved = true;
                const f = frameAt(e.clientX, r0);
                if (videoEl) { if (!videoEl.paused) videoEl.pause(); seekTo(frameTime(f)); }
                lastK = segAt(frameTime(f), bd);
                showRdo(e.clientX, e.clientY, `第 ${f} 帧 · ${fmtT(f / VE_FPS)}`);
            };
            go(ev);
            const up = () => {
                document.removeEventListener("mousemove", go);
                document.removeEventListener("mouseup", up);
                hideRdo();
                selKind = "seg";
                if (lock) { // 点在视频轨上的片段 = 选定这一段；只是点一下（没拖动）就让预览跳到这一段开头
                    if (!moved && segLocked && lockIdx === lastK) { // 已经选定的这一段，再点一下 = 取消选定
                        segLocked = false;
                        lockIdx = -1;
                        active = lastK;
                        renderTimeline(); renderPanel(); renderSegBar();
                        return;
                    }
                    segLocked = true;
                    lockIdx = lastK;
                    if (!moved && videoEl) { if (!videoEl.paused) videoEl.pause(); seekTo(frameTime(Math.round(bd[lastK] * VE_FPS))); }
                    active = lastK;
                    renderTimeline(); renderPanel(); renderSegBar();
                    return;
                }
                if (lastK !== active) { active = lastK; renderTimeline(); renderPanel(); renderSegBar(); }
            };
            document.addEventListener("mousemove", go);
            document.addEventListener("mouseup", up);
        };
        ruler.addEventListener("mousemove", (ev) => {
            const f = frameAt(ev.clientX, ruler.getBoundingClientRect());
            showRdo(ev.clientX, ev.clientY, `第 ${f} 帧 · ${fmtT(f / VE_FPS)}`);
        });
        ruler.addEventListener("mouseleave", hideRdo);
        ruler.addEventListener("mousedown", (ev) => { if (ev.button === 0) startScrub(ev, ruler); });
        const xToT = (clientX, rect) => {
            const r = rect || lanes[0].getBoundingClientRect();
            return Math.min(T, Math.max(0, ((clientX - r.left) / r.width) * T));
        };

        // 视频轨
        const vlane = mkRow("视频", "vlane");
        const GAP = 1.5; // 分割处断开的缝（px，每侧）
        for (let i = 0; i < n; i++) {
            const a = bd[i], b = bd[i + 1];
            const blk = el("div", { class: "blk" + (i === active ? " on" : "") + (i === hlSeg ? " hl" : "") + (segLocked && i === lockIdx ? " lk" : ""), title: `第 ${i + 1} 段：点一下选定这一段（预览跳到这一段开头，播放只播这一段）；再点一下这一段、按 Esc、或点别处取消选定；左右拖动可以定位` }, [
                el("span", { text: `第${i + 1}段` }), el("small", { text: pl?.plan?.[i] ? `${pl.plan[i].delivered - pl.plan[i].tail_cut}帧 ${((pl.plan[i].delivered - pl.plan[i].tail_cut) / VE_FPS).toFixed(2)}s` : `${(b - a).toFixed(2)}s` }),
            ]);
            blk.style.left = `calc(${pct(a)} + ${i === 0 ? 0 : GAP}px)`;
            blk.style.width = `calc(${Math.max(0, ((b - a) / T) * 100)}% - ${(i === 0 ? 0 : GAP) + (i === n - 1 ? 0 : GAP)}px)`;
            blk.style.background = COLORS[i % COLORS.length];
            vlane.appendChild(blk);
        }
        vlane.addEventListener("mousedown", (ev) => { if (ev.button !== 0 || ev.target.closest(".cut")) return; startScrub(ev, vlane, true); });
        for (let k = 0; k < n - 1; k++) {
            const h = el("div", { class: "cut", title: `分割线 ${k + 1}：${fmtT(bd[k + 1])}，左右拖动调整` });
            h.style.left = pct(bd[k + 1]);
            h.addEventListener("mousedown", (ev) => {
                ev.preventDefault();
                ev.stopPropagation();
                const r0 = lanes[0].getBoundingClientRect(); // 拖动中会重画，所以按下时就把轨道的位置记下来
                const move = (e) => {
                    const lo = (k > 0 ? st.cuts[k - 1] : 0) + 0.1;
                    const hi = (k < st.cuts.length - 1 ? st.cuts[k + 1] : T) - 0.1;
                    st.cuts[k] = Math.min(hi, Math.max(lo, xToT(e.clientX, r0)));
                    snapCuts();
                    if (!dragRaf) dragRaf = requestAnimationFrame(() => { dragRaf = 0; renderTimeline(); });
                };
                const up = () => {
                    document.removeEventListener("mousemove", move);
                    document.removeEventListener("mouseup", up);
                    commit();
                };
                document.addEventListener("mousemove", move);
                document.addEventListener("mouseup", up);
            });
            vlane.appendChild(h);
        }

        // 图片轨
        st.vtracks.forEach((track, ti) => {
            const addBtn = el("button", { text: "＋", title: "给这条轨道添加一张图片（放在选中的那一段，再拖动边缘拉长）" });
            const input = pickFile("image/*", (f) => addImage(ti, f));
            addBtn.addEventListener("click", () => input.click());
            const lab = el("span", {}, [addBtn, input]);
            const lane = mkRow(`图片${ti + 1}`, "ilane", lab);
            for (let i = 1; i < n; i++) lane.appendChild(Object.assign(el("div", { class: "gap" }), { style: `left:${pct(bd[i])}` }));
            if (st.vtracks.length > 1 && !track.length) {
                lab.appendChild(el("button", { class: "danger", text: "×", title: "删除这条空轨道", onclick: () => { st.vtracks.splice(ti, 1); commit(); } }));
            }
            track.forEach((c, ci) => {
                const clip = el("div", { class: "clip" + (c._bad ? " bad" : "") + (clipIs(ti, c) ? " sel" : "") }, [
                    el("img", { src: viewUrl(c.file), alt: "" }),
                    el("span", { class: "cn", text: splitPath(c.file).filename }),
                    el("span", { class: "x", text: "×", title: "移除这张图", onmousedown: (e) => e.stopPropagation(), onclick: () => { track.splice(ci, 1); commit(); } }),
                    el("div", { class: "rh l" }), el("div", { class: "rh r" }),
                ]);
                clip.style.left = `calc(${pct(bd[c.a])} + 1px)`;
                clip.style.width = `calc(${((bd[c.b + 1] - bd[c.a]) / T) * 100}% - 2px)`;
                // 鼠标放上去：和引导点一样弹出缩略图，鼠标移到缩略图上点一下就放大预览
                clip.addEventListener("mouseenter", (e) => showGtip(clip, c, `图片${ti + 1} · 第${c.a + 1}${c.b > c.a ? `-${c.b + 1}` : ""}段`, e.clientX));
                clip.addEventListener("mouseleave", hideGtip);
                clip.addEventListener("mousedown", (ev) => {
                    if (ev.button !== 0 || ev.target.classList.contains("x")) return;
                    ev.preventDefault();
                    hideGtipNow();
                    focusRoot();
                    selClip = { ti, file: c.file, a: c.a };
                    selKind = "clip";
                    root.querySelectorAll(".clip.sel").forEach((x) => x.classList.remove("sel"));
                    clip.classList.add("sel");
                    const mode = ev.target.classList.contains("l") ? "l" : ev.target.classList.contains("r") ? "r" : "m";
                    const r0 = lanes[0].getBoundingClientRect();
                    const idxAt = (clientX) => segAt(xToT(clientX, r0), bd);
                    const grab = idxAt(ev.clientX);
                    const a0 = c.a, b0 = c.b, ti0 = st.vtracks.findIndex((t) => t.includes(c));
                    const trackOf = () => st.vtracks.findIndex((t) => t.includes(c));
                    const others = () => st.vtracks[trackOf()].filter((o) => o !== c);
                    const clash = (a, b) => others().some((o) => !(b < o.a || a > o.b));
                    // 竖着拖：按鼠标的高度判断落在哪条图片轨上（按下时记下各条图片轨的位置，拖动中会重画）
                    const irects = lanes.filter((l) => l.classList.contains("ilane")).map((l) => l.getBoundingClientRect());
                    const trackAtY = (y) => {
                        let best = 0, bestD = Infinity;
                        irects.forEach((r, k) => { const d = y < r.top ? r.top - y : y > r.bottom ? y - r.bottom : 0; if (d < bestD) { bestD = d; best = k; } });
                        return best;
                    };
                    const move = (e) => {
                        const idx = idxAt(e.clientX);
                        if (mode === "l") {
                            const prev = others().filter((o) => o.b < a0).pop();
                            c.a = Math.min(b0, Math.max(prev ? prev.b + 1 : 0, idx));
                        } else if (mode === "r") {
                            const next = others().find((o) => o.a > b0);
                            c.b = Math.max(a0, Math.min(next ? next.a - 1 : n - 1, idx));
                        } else {
                            const len = b0 - a0;
                            const tj = trackAtY(e.clientY), cj = trackOf();
                            if (tj !== cj && st.vtracks[tj]) { // 换轨道
                                st.vtracks[cj].splice(st.vtracks[cj].indexOf(c), 1);
                                st.vtracks[tj].push(c);
                            }
                            const na = Math.min(n - 1 - len, Math.max(0, a0 + (idx - grab)));
                            c.a = na;
                            c.b = na + len;
                            c._bad = clash(c.a, c.b); // 落点已经被这条轨道上的别的图片占了：红框，松开会回到原位
                            showRdo(e.clientX, e.clientY, c._bad ? "这里已经有图片了，松开会回到原位" : `图片${trackOf() + 1} · 第 ${c.a + 1}${c.b > c.a ? `-${c.b + 1}` : ""} 段`);
                        }
                        if (!dragRaf) dragRaf = requestAnimationFrame(() => { dragRaf = 0; renderTimeline(); });
                    };
                    const up = () => {
                        document.removeEventListener("mousemove", move);
                        document.removeEventListener("mouseup", up);
                        hideRdo();
                        if (c._bad) { // 回到原位（原来那条轨道）
                            const cj = trackOf();
                            if (cj !== ti0) { st.vtracks[cj].splice(st.vtracks[cj].indexOf(c), 1); st.vtracks[ti0].push(c); }
                            c.a = a0;
                            c.b = b0;
                        }
                        delete c._bad;
                        if (selClip && selClip.file === c.file) { // 拖动后：选中记到它的新位置
                            const tj = st.vtracks.findIndex((t) => t.includes(c));
                            if (tj >= 0) selClip = { ti: tj, file: c.file, a: c.a };
                        }
                        st.vtracks = st.vtracks.map((tr) => fixTrack(tr));
                        commit();
                    };
                    document.addEventListener("mousemove", move);
                    document.addEventListener("mouseup", up);
                });
                lane.appendChild(clip);
            });
        });
        // 引导：一条轨道，上面可以有多个引导点（一个点 = 一张图 = 一帧）；拖动点左右移动
        const gl = mkRow("引导", "glane", null);
        for (let i = 1; i < n; i++) gl.appendChild(Object.assign(el("div", { class: "gap" }), { style: `left:${pct(bd[i])}` }));
        const cutFrames = bd.slice(1, -1).map((t) => Math.round(t * VE_FPS)); // 每条分割线 = 下一段的第一帧
        const SNAP_PX = 8;
        const gLabel = (f) => {
            const e = guideSeg(f);
            if (!e) return `第 ${f} 帧`;
            const side = f === e.story_start ? " · 首帧" : f === e.story_end - e.tail_cut - 1 || f === e.story_end - 1 ? " · 末帧" : "";
            return `第 ${e.index + 1} 段${side}（第 ${f} 帧）`;
        };
        st.gpoints.forEach((g) => {
            const se = guideSeg(g.f);
            const isStart = !!se && se.index > 0 && g.f === se.story_start;       // 紧挨分割线右边的第一帧
            const isEnd = !!se && se.index < n - 1 && g.f === se.story_end - 1;   // 紧挨分割线左边的最后一帧
            const mk = el("div", {
                class: "gmark" + (g === gsel ? " on" : "") + (g.file ? "" : " empty") + (isStart ? " es" : "") + (isEnd ? " ee" : ""),
                text: g.file ? "▣" : "▢",
                title: `整条视频第 ${g.f} 帧（${fmtT(g.f / VE_FPS)}）${se ? `；第 ${se.index + 1} 段${isStart ? "首帧" : isEnd ? "末帧" : ""}` : ""}；左右拖动改帧号；点一下放大预览`,
            });
            mk.addEventListener("mouseenter", () => showGtip(mk, g));
            mk.addEventListener("mouseleave", hideGtip);
            // 标记画在这一帧的起点（和刻度、红线一致）；分割线两侧的首帧/末帧贴在各自那一边，一眼分得清
            mk.style.left = `${((isEnd ? g.f + 1 : g.f) / TF) * 100}%`;
            if (isStart) mk.style.marginLeft = "1px"; else if (isEnd) mk.style.marginLeft = "-23px";
            if (se) mk.style.borderBottom = `3px solid ${COLORS[se.index % COLORS.length]}`;
            mk.addEventListener("mousedown", (ev) => {
                ev.preventDefault();
                ev.stopPropagation();
                hideGtipNow();
                focusRoot();
                const r0 = gl.getBoundingClientRect();
                if (ev.detail >= 2) { toggleCompare(g); return; } // 双击：打开/关闭这个引导点的对比画面
                selKind = "guide";
                if (gsel !== g) selectPoint(g); else if (videoEl) seekTo(frameTime(g.f));
                const f0 = g.f;
                const pickFrame = (clientX) => {
                    const x = clientX - r0.left;
                    let f = Math.floor((x / r0.width) * TF);
                    for (const c of cutFrames) { // 靠近分割线（8 像素内）：按鼠标在线的哪一侧，吸到左边段的末帧或右边段的首帧
                        const xc = (c / TF) * r0.width;
                        if (Math.abs(x - xc) <= SNAP_PX) { f = x < xc ? c - 1 : c; break; }
                    }
                    return Math.max(0, Math.min(TF - 1, f));
                };
                const move = (e) => {
                    const f = pickFrame(e.clientX);
                    const se2 = guideSeg(f);
                    hlSeg = se2 ? se2.index : -1;
                    setPointFrame(g, f, true);
                    showRdo(e.clientX, e.clientY, gLabel(g.f));
                };
                const up = () => {
                    document.removeEventListener("mousemove", move);
                    document.removeEventListener("mouseup", up);
                    hideRdo();
                    hlSeg = -1;
                    commit();
                    updateCompare();
                };
                document.addEventListener("mousemove", move);
                document.addEventListener("mouseup", up);
            });
            gl.appendChild(mk);
        });
        gl.addEventListener("mousedown", (ev) => { if (ev.target === gl && ev.button === 0) startScrub(ev, gl); });
        // 播放头：盖在所有轨道上
        const overlay = el("div", { class: "row", style: "position:absolute;left:0;right:0;top:4px;bottom:4px;pointer-events:none" }, [
            el("div", { style: "flex:0 0 74px" }),
            el("div", { class: "lane", style: "pointer-events:none;margin-top:0" }, [(headEl = el("div", { class: "head" }, [(headFrame = el("span", { class: "hf", text: "0" }))]))]),
        ]);
        tl.appendChild(overlay);
        tl.appendChild(el("div", { class: "row", style: "margin-top:4px" }, [
            el("div", { class: "lab" }),
            el("div", { class: "lane", style: "margin-top:0" }, [
                el("button", { text: "＋ 图片轨", title: "参考图轨道：图片盖住哪几段，那几段就输出这张参考图", disabled: st.vtracks.length >= MAX_TRACKS ? "disabled" : undefined, onclick: () => { st.vtracks.push([]); commit(); } }),
            ]),
        ]));
        const wrap = el("div", { class: "tlwrap" }, [el("div", { class: "tlin", style: `width:${zoom * 100}%` }, [tl])]);
        tlHost.appendChild(wrap);
        if (zoomCenter != null) { wrap.scrollLeft = zoomCenter * wrap.scrollWidth - wrap.clientWidth / 2; zoomCenter = null; } else wrap.scrollLeft = oldScroll;
        movePlayhead();

        // ---- 规划摘要
        infoDetail.textContent = "";
        const info = el("div", { class: "info" });
        const lines = [];
        if (pl?.error) info.appendChild(el("div", { class: "e", text: `⚠ ${pl.error}` }));
        else if (pl?.plan.length) {
            const frames = pl.plan.reduce((s, e) => s + e.delivered, 0) - pl.plan[pl.plan.length - 1].tail_cut;
            const gs = genSize();
            lines.push(`共 ${n} 段，成片 ${frames} 帧（${(frames / VE_FPS).toFixed(2)} 秒，${VE_FPS} fps）${gs ? `，原视频 ${videoEl.videoWidth}×${videoEl.videoHeight} → 缩放视频 ${gs[0]}×${gs[1]}（${mpVal() > 0 ? "最大边长=" + mpVal() : "最大边长=0，不缩放，两个视频口是同一份"}）` : ""}。分割位置会自动吸附到模型能接受的帧数上，所以实际分割线可能和你点的位置差几帧。`);
        }
        const bare = [];
        for (let i = 0; i < n; i++) if (!st.segments[i].images.length) bare.push(i + 1);
        if (bare.length) info.appendChild(el("div", { class: "w", text: `第 ${bare.join("、")} 段还没有参考图：把图片轨上的图片拉到覆盖这些段。` }));
        for (const w of pl?.warns || []) info.appendChild(el("div", { class: "w", text: w }));
        if (lines.length) info.prepend(el("div", { text: lines[0] }));
        if (pl?.plan?.length && !pl.error) {  // 逐段明细：每段起止、输出长度（帧）、秒数
            const box = el("div", { class: "segtab" });
            for (const e of pl.plan) {
                const keep = e.delivered - e.tail_cut;
                const t = `第${e.index + 1}段　${fmtT(e.story_start / VE_FPS)} → ${fmtT((e.story_end - e.tail_cut) / VE_FPS)}　`
                    + `长度输出 ${e.sampled} 帧（${(e.sampled / VE_FPS).toFixed(2)} 秒）`
                    + (e.head ? `（含往前多取的 ${e.head} 帧，合并时自动裁掉）` : "")
                    + (e.tail_cut ? `；最后一段多出 ${e.tail_cut} 帧，合并时裁掉，成片保留 ${keep} 帧（${(keep / VE_FPS).toFixed(2)} 秒）` : "");
                box.appendChild(el("div", { text: t }));
            }
            info.insertBefore(box, info.children[1] || null);
        }
        infoDetail.appendChild(info);
        refreshStatus();
        renderSegBar();
    }
    /** 缩放时间轴：保持当前看的中心位置不变 */
    function applyZoom(z, lbl) {
        const nz = Math.min(10, Math.max(1, Math.round(z * 2) / 2));
        if (nz === zoom) return;
        const w = tlHost.querySelector(".tlwrap");
        zoomCenter = w && w.scrollWidth ? (w.scrollLeft + w.clientWidth / 2) / w.scrollWidth : 0.5;
        zoom = nz;
        if (lbl) lbl.textContent = `${zoom}×`;
        else renderTimeline();
        if (lbl) renderTimeline(true);
        const zi = toolHost.querySelector("input[type=range]");
        if (zi && Number(zi.value) !== zoom) zi.value = String(zoom);
        const zl = zi?.nextSibling;
        if (zl && !lbl) zl.textContent = `${zoom}×`;
    }

    /* ---------------------------------------------------------------- 选中段信息 + 提示词 */
    function renderPanel() {
        panelBox.textContent = "";
        if (!st.video.file) return;
        const n = st.segments.length;
        if (active >= n) active = n - 1;
        const pl = curPlan();
        const refs = (i) => st.segments[i].images;
        let curTa = null;
        /** 在提示词光标处插入标签 */
        const insertTag = (tag) => {
            const ta = curTa;
            if (!ta) return;
            const a = ta.selectionStart ?? ta.value.length, b = ta.selectionEnd ?? a;
            ta.value = ta.value.slice(0, a) + tag + ta.value.slice(b);
            ta.focus();
            ta.selectionStart = ta.selectionEnd = a + tag.length;
            ta.dispatchEvent(new Event("input"));
        };
        const refRow = (i) => {
            const row = el("div", { class: "refs" }, [el("span", { text: `第 ${i + 1} 段的参考图（点一下在提示词光标处插入标签）：` })]);
            row.appendChild(el("button", { text: "源视频 <Video 1>", title: "在光标处插入 <Video 1>（源视频片段）", onclick: () => insertTag("<Video 1>") }));
            if (!refs(i).length) row.appendChild(el("span", { text: "（无）" }));
            refs(i).forEach((it, j) => {
                const rc = el("div", { class: "rc", title: `${splitPath(it.file).filename}\n点击：插入 <Picture ${j + 1}>` , style: "cursor:pointer", onclick: () => insertTag(`<Picture ${j + 1}>`) }, [
                    el("img", { src: viewUrl(it.file), alt: "" }), el("b", { text: `图${j + 1}` }),
                    el("i", { text: "🔍", title: "查看大图", style: "position:absolute;right:0;bottom:0;font-size:11px;font-style:normal;background:#000a;padding:0 2px;cursor:zoom-in",
                        onclick: (ev) => { ev.stopPropagation(); openViewer("img", viewUrl(it.file), splitPath(it.file).filename); } }),
                ]);
                row.appendChild(rc);
            });
            return row;
        };

        // 选中段信息
        const e = pl?.plan?.[active];
        const head = el("div", { class: "bar" }, [
            el("b", { text: `第 ${active + 1} 段` }),
            el("span", { class: "hint", text: e ? `${fmtT(e.story_start / VE_FPS)} – ${fmtT(e.story_end / VE_FPS)}　长度输出 ${e.sampled} 帧（${(e.sampled / VE_FPS).toFixed(2)} 秒）` : "" }),
        ]);

        // 提示词
        const modeBtn = (v, text) => el("button", { class: st.pmode === v ? "on" : "", text, onclick: () => { st.pmode = v; save(); renderPanel(); } });
        const box = el("div", { class: "pbox" });
        box.appendChild(el("div", { class: "bar" }, [
            el("span", { class: "pmode" }, [modeBtn("global", "全局提示词"), modeBtn("local", "局部提示词")]),
            el("span", { class: "hint", text: st.pmode === "global" ? "同一条提示词用于所有片段" : "每段单独写；某一段留空就用全局提示词" }),
        ]));
        const ta = el("textarea", { placeholder: "" });
        if (st.pmode === "global") {
            ta.placeholder = "写下要对所有片段生效的提示词，例如：把 <Video 1> 里的人换成 <Picture 1> 里的人。点下面的缩略图可以直接插入标签。";
            ta.value = st.gprompt;
            curTa = ta;
            const gLinked = inputLinked(node, GLOBAL_INPUT);
            ta.addEventListener("input", () => { st.gprompt = ta.value; save(); });
            ta.disabled = gLinked;
            box.appendChild(ta);
            if (gLinked) box.appendChild(el("div", { class: "hint", text: "已由连线「全局提示词」提供，这里写的内容不使用。" }));
            box.appendChild(refRow(active));
        } else {
            const tabs = el("div", { class: "tabs" });
            for (let i = 0; i < n; i++) {
                tabs.appendChild(el("button", {
                    class: "tab" + (i === active ? " on" : ""),
                    text: `第${i + 1}段${String(st.lprompts[i] || "").trim() ? "" : "（全局）"}`,
                    onclick: () => { active = i; renderTimeline(); renderPanel(); },
                }));
            }
            box.appendChild(tabs);
            ta.placeholder = `第 ${active + 1} 段的提示词；留空则使用全局提示词`;
            ta.value = st.lprompts[active] || "";
            curTa = ta;
            const lLinked = inputLinked(node, LOCAL_INPUT);
            ta.addEventListener("input", () => { st.lprompts[active] = ta.value; lastSnap = snapNow(); save(); });
            ta.disabled = lLinked;
            box.appendChild(ta);
            if (lLinked) box.appendChild(el("div", { class: "hint", text: "已由连线「局部提示词」提供（用一行「---」分段），这里写的内容不使用。" }));
            box.appendChild(refRow(active));
            if (!String(st.lprompts[active] || "").trim()) box.appendChild(el("div", { class: "hint", text: `这一段留空，实际使用全局提示词：${st.gprompt || "（全局提示词也是空的）"}` }));
        }
        panelBox.append(head, box);
        renderGuideBox();
    }

    /** 引导区：点的列表（标签式）+ 选中点的控制（帧号 / ◀ ▶ / 换图 / 删除 / 对比） */
    function renderGuideBox() {
        guideBox.textContent = "";
        if (!st.video.file) return;
        const box = el("div", { class: "gbox" });
        const total = totalFrames();
        const addInp = pickFile("image/*", (f) => addGuidePoint(f));
        box.appendChild(el("div", { class: "bar" }, [
            el("b", { text: "引导" }),
            el("button", { text: "＋ 添加引导点", title: "选一张图，插到当前播放位置那一帧（只做引导，不当参考图）", disabled: st.gpoints.length >= MAX_GUIDE ? "disabled" : undefined, onclick: () => addInp.click() }), addInp,
            el("span", { class: "hint", text: "一个点 = 一张图 = 一帧。右边最后一个输出口「引导」接「H3 批量添加引导」。" }),
        ]));
        if (!st.gpoints.length) { guideBox.appendChild(box); return; }
        const chips = el("div", { class: "chips" });
        st.gpoints.forEach((g, i) => chips.appendChild(el("button", {
            class: "chip" + (g === gsel ? " on" : ""), text: `${i + 1} · 第${g.f}帧`,
            title: g.file ? splitPath(g.file).filename : "（还没有图片）", onclick: () => selectPoint(g),
        })));
        box.appendChild(chips);
        const g = gsel;
        if (!g) box.appendChild(el("div", { class: "hint", text: "点上面的标签（或时间轴上的黄色标记）选中一个引导点。" }));
        else {
            const num = el("input", { type: "number", min: "0", max: String(Math.max(0, total - 1)), value: String(g.f), title: "整条视频里的第几帧（从 0 数，24 fps）" });
            num.addEventListener("change", () => setPointFrame(g, parseInt(num.value, 10) || 0, true));
            const e = guideSeg(g.f);
            const pickImg = pickFile("image/*", (f) => setPointImage(g, f));
            box.appendChild(el("div", { class: "gc" }, [
                el("span", { text: "第" }), num, el("span", { text: "帧" }),
                el("button", { text: "◀", title: "上一帧", onclick: () => setPointFrame(g, g.f - 1, true) }),
                el("button", { text: "▶", title: "下一帧", onclick: () => setPointFrame(g, g.f + 1, true) }),
                el("span", { text: `${fmtT(g.f / VE_FPS)}　${e ? `落在第 ${e.index + 1} 段，段内第 ${g.f - e.story_start + (e.head || 0)} 帧（从 0 数${e.head ? `，已含本段往前多取的 ${e.head} 帧` : ""}）` : "超出视频范围，或分段规划有错"}` }),
            ]));
            box.appendChild(el("div", { class: "gc" }, [
                el("span", { text: g.file ? `图片：${splitPath(g.file).filename}` : "还没有图片" }),
                el("button", { text: g.file ? "换图" : "选择图片", onclick: () => pickImg.click() }), pickImg,
                el("button", { class: "danger", text: "删除", onclick: () => { st.gpoints.splice(st.gpoints.indexOf(g), 1); gsel = null; commit(); updateCompare(); } }),
            ]));
        }
        guideBox.appendChild(box);
    }

    /* ---------------------------------------------------------------- 抓取当前帧 */
    /** 把预览视频当前这一帧画到画布上，返回 PNG（原视频分辨率） */
    const frameBlob = () => new Promise((resolve, reject) => {
        if (!videoEl || !videoEl.videoWidth) return reject(new Error("视频还没有加载好。"));
        const go = () => {
            try {
                const c = document.createElement("canvas");
                c.width = videoEl.videoWidth;
                c.height = videoEl.videoHeight;
                c.getContext("2d").drawImage(videoEl, 0, 0);
                c.toBlob((b) => (b ? resolve(b) : reject(new Error("截取画面失败。"))), "image/png");
            } catch (err) { reject(err); }
        };
        if (videoEl.seeking) videoEl.addEventListener("seeked", go, { once: true }); else go();
    });
    const saveFramePng = async () => {
        try {
            const f = curFrame();
            const b = await frameBlob();
            // 默认文件名：分秒_整段帧号，例如整段第 25 帧（第 1 秒）→ 001_25.png（分钟 1 位 + 秒 2 位，按 24 fps 换算）
            const sec = Math.floor(f / VE_FPS);
            const a = document.createElement("a");
            a.href = URL.createObjectURL(b);
            a.download = `${Math.floor(sec / 60)}${String(sec % 60).padStart(2, "0")}_${f}.png`;
            document.body.appendChild(a);
            a.click();
            a.remove();
            setTimeout(() => URL.revokeObjectURL(a.href), 4000);
        } catch (err) { alert(`保存当前帧失败：${err?.message || err}`); }
    };

    /* ---------------------------------------------------------------- 键盘（鼠标放在编辑器上才生效） */
    /** 跳到整条视频的第 f 帧（暂停），并选中它所在的那一段 */
    const gotoFrame = (f) => {
        const tf = totalFrames();
        if (!videoEl || !(tf > 0)) return;
        f = Math.max(0, Math.min(tf - 1, Math.round(f)));
        if (!videoEl.paused) videoEl.pause();
        seekTo(frameTime(f));
        const k = segAt(frameTime(f), bounds());
        selKind = "seg";
        if (k !== active) { active = k; renderTimeline(); renderPanel(); renderSegBar(); }
    };
    /** 跳到上一个（dir<0）/ 下一个（dir>0）分割点（每段第一帧）或引导点 */
    const jumpPoint = (dir) => {
        const cur = curFrame();
        const pts = new Set([0]);
        const p = curPlan();
        if (p && !p.error) p.plan.forEach((e) => pts.add(e.story_start));
        st.gpoints.forEach((g) => pts.add(g.f));
        const arr = [...pts].sort((a, b) => a - b);
        const tgt = dir < 0 ? [...arr].reverse().find((x) => x < cur) : arr.find((x) => x > cur);
        if (tgt !== undefined) gotoFrame(tgt);
    };
    let hover = false;
    let nudgeTimer = null;
    root.addEventListener("mouseenter", () => { hover = true; });
    root.addEventListener("mouseleave", () => { hover = false; });
    const onKey = (e) => {
        if (!root.isConnected) { window.removeEventListener("keydown", onKey, true); return; }
        if (e.altKey || !(hover || root.contains(document.activeElement))) return;
        if (document.querySelector(".h3ms-lb")) return; // 放大预览开着：键盘归预览
        const t = e.target, tag = t && t.tagName;
        const typing = tag === "TEXTAREA" || tag === "SELECT" || !!(t && t.isContentEditable)
            || (tag === "INPUT" && !["range", "checkbox", "radio", "button", "file"].includes(t.type));
        if (typing) return; // 在输入框里打字：方向键、空格照常
        const k = e.key;
        if (k === "Escape" && segLocked) { // Esc：取消片段选定
            segLocked = false;
            lockIdx = -1;
            renderTimeline();
            e.preventDefault(); e.stopPropagation();
            return;
        }
        const mod = e.ctrlKey || e.metaKey;
        if (mod) {
            if (k === "z" || k === "Z" || k === "y" || k === "Y") {
                const wantRedo = k === "y" || k === "Y" || e.shiftKey;
                if (wantRedo ? redo() : undo()) { e.preventDefault(); e.stopPropagation(); }
            }
            return;
        }
        const guideOn = selKind === "guide" && gsel && st.gpoints.includes(gsel);
        const clipOn = selKind === "clip" && !!findSelClip();
        if (clipOn && k === "Escape") { // Esc：取消选中图片块
            selClip = null; selKind = "seg";
            renderTimeline(true);
            e.preventDefault(); e.stopImmediatePropagation();
            return;
        }
        if (k === "Delete" || k === "Backspace") {
            // 删除最后点的那个：图片块删图片块；引导点删引导点；片段就并入上一段（第 1 段并入下一段）
            if (clipOn) {
                const cc = findSelClip(), tr = st.vtracks[selClip.ti];
                tr.splice(tr.indexOf(cc), 1);
                selClip = null; selKind = "seg";
                commit();
            }
            else if (guideOn) { st.gpoints.splice(st.gpoints.indexOf(gsel), 1); gsel = null; selKind = "seg"; commit(); updateCompare(); }
            else if (st.segments.length > 1) removeSplit();
            else return;
            e.preventDefault(); e.stopImmediatePropagation();
            return;
        }
        if (guideOn && (k === "ArrowLeft" || k === "ArrowRight")) {
            // 选中了引导点：左右键挪它的帧号（Shift 一次 10 帧），预览跟着走；停手后再记一次撤销
            const g = gsel;
            setPointFrame(g, g.f + (k === "ArrowLeft" ? -1 : 1) * (e.shiftKey ? 10 : 1), false);
            if (videoEl) { if (!videoEl.paused) videoEl.pause(); seekTo(frameTime(g.f)); }
            clearTimeout(nudgeTimer);
            nudgeTimer = setTimeout(() => { if (root.isConnected) { commit(); updateCompare(); } }, 400);
            e.preventDefault(); e.stopImmediatePropagation();
            return;
        }
        if (guideOn && k === "Escape") { // Esc：取消选中引导点，方向键回到移动播放头
            gsel = null; selKind = "seg";
            renderTimeline(true); renderGuideBox(); updateCompare();
            e.preventDefault(); e.stopImmediatePropagation();
            return;
        }
        if (!videoEl || !(totalFrames() > 0)) return;
        let handled = true;
        if (k === "g" || k === "G") saveFramePng();
        else if (k === "ArrowLeft") gotoFrame(curFrame() - (e.shiftKey ? 10 : 1));
        else if (k === "ArrowRight") gotoFrame(curFrame() + (e.shiftKey ? 10 : 1));
        else if (k === "ArrowUp") jumpPoint(-1);
        else if (k === "ArrowDown") jumpPoint(1);
        else if (k === "Home") gotoFrame(0);
        else if (k === "End") gotoFrame(totalFrames() - 1);
        else if (k === " " && tag !== "BUTTON" && !(tag === "INPUT" && ["checkbox", "radio", "file", "button"].includes(t.type))) {
            if (videoEl.paused) videoEl.play().catch(() => {}); else videoEl.pause();
        } else handled = false;
        if (handled) { e.preventDefault(); e.stopPropagation(); }
    };
    // 挂在 window 的捕获阶段：比 ComfyUI 自己的快捷键先收到，Del 不会被当成「删除画布上选中的节点」
    window.addEventListener("keydown", onKey, true);
    /** 选定的片段：点了视频轨上的片段、预览视频、工具栏以外的地方，就自动取消选定 */
    const onDown = (e) => {
        if (!root.isConnected) { document.removeEventListener("mousedown", onDown, true); document.removeEventListener("pointerdown", onDown, true); return; }
        if (!segLocked) return;
        const t = e.target;
        if (t && t.closest && root.contains(t) && (t.closest(".vlane") || t.closest(".vwrap") || t.closest(".tool"))) return;
        if (t && t.closest && t.closest(".h3-gtip,.h3-rdo")) return;
        segLocked = false;
        lockIdx = -1;
        root.querySelectorAll(".blk.lk").forEach((x) => x.classList.remove("lk")); // 只去掉高亮，不重画（重画会让这一下点击落空）
    };
    document.addEventListener("mousedown", onDown, true);
    document.addEventListener("pointerdown", onDown, true); // 点画布空白处时 ComfyUI 可能不再发 mousedown，所以两种事件都听

    /* ---------------------------------------------------------------- 对外接口（和 H3 多段输入的编辑器一致） */
    const load = () => {
        let data = null;
        try { data = dataWidget.value ? JSON.parse(dataWidget.value) : null; } catch (_) { data = null; }
        st = parse(data);
        derive();
        active = 0;
        videoFile = "";
        undoStack.length = 0;
        redoStack.length = 0;
        lastSnap = snapNow();
        renderTop();
        renderTimeline();
        renderPanel();
        syncOutputs();
    };
    return {
        root, load, getState: () => st, validDone, setProgress,
        getTask: () => st.progress?.task || "",
        rerender: () => { renderTop(); renderTimeline(); renderPanel(); },
        settingsChanged: () => commit(),
        refreshLive: () => { syncOutputs(); refreshStatus(); },
        setRunText: (t) => { runText = t || ""; refreshStatus(); },
    };
}


/* ----------------------------------------------------------------- 挂到节点上 */
function hideWidget(w) {
    if (!w) return;
    try {
        w.computeSize = () => [0, -4];
        w.hidden = true;
        if (w.element) w.element.style.display = "none";
        if (w.inputEl) w.inputEl.style.display = "none";
    } catch (_) {}
}

function veDeps() {
    return { app, el, viewUrl, uploadFile, splitPath, openViewer, hashText, vePlan, veJoins, veTotalFrames, JOINS, VE_AUDIO, VE_FPS, injectCss };
}

const EDGE_WIDGET = "参考视频最大边长";
const JOIN_WIDGET = "段间衔接";
const HEAD_WIDGET = "重叠帧数";
const FPS_WIDGET = "参考视频帧率";
const JOIN_OPTIONS = ["硬切", "复制", "引导", "图片引导"];
const PREFIX_NODE = "MiniMaxH3LatentPrefixCopy";

/** 显示/隐藏一个控件（隐藏时不占位置） */
function showWidget(w, shown) {
    if (!w) return;
    if (shown) {
        w.hidden = false;
        if (w.__h3Compute !== undefined) {
            if (w.__h3Compute) w.computeSize = w.__h3Compute; else delete w.computeSize;
            w.__h3Compute = undefined;
        }
    } else {
        w.hidden = true;
        if (w.__h3Compute === undefined) w.__h3Compute = Object.prototype.hasOwnProperty.call(w, "computeSize") ? w.computeSize : null;
        w.computeSize = () => [0, -4];
    }
}

function setup(node) {
    const dataWidget = node.widgets?.find((w) => w.name === DATA_WIDGET);
    if (!dataWidget) return;
    const modeWidget = node.widgets?.find((w) => w.name === MODE_WIDGET);
    // 「到最后一段」放在「输出第几段」正下方（显示顺序；旧工作流的值在 onConfigure 里按名字重新对应）
    try {
        const ws = node.widgets, i0 = ws.findIndex((w) => w.name === SEG_WIDGET), j0 = ws.findIndex((w) => w.name === TOEND_WIDGET);
        if (i0 >= 0 && j0 >= 0 && j0 !== i0 + 1) { const [tw] = ws.splice(j0, 1); ws.splice(i0 + (j0 < i0 ? 0 : 1), 0, tw); }
        // 「参考视频帧率」放在「参考视频最大边长」正下方
        const e0 = ws.findIndex((w) => w.name === EDGE_WIDGET), f0 = ws.findIndex((w) => w.name === FPS_WIDGET);
        if (e0 >= 0 && f0 >= 0 && f0 !== e0 + 1) { const [fw] = ws.splice(f0, 1); ws.splice(e0 + (f0 < e0 ? 0 : 1), 0, fw); }
    } catch (_) {}
    const std = buildEditor(node, dataWidget, modeWidget);
    const vid = buildVideoEditEditor(node, dataWidget, veDeps());
    const isVE = () => modeWidget?.value === MODE_VE;
    const cur = () => (isVE() ? vid : std);
    hideWidget(dataWidget);
    hideWidget(node.widgets?.find((w) => w.name === TASK_WIDGET));
    const wrap = el("div", { class: "h3wrap" }, [std.root, vid.root]);
    const applyMode = () => {
        const ve = isVE();
        std.root.style.display = ve ? "none" : "";
        vid.root.style.display = ve ? "" : "none";
        showWidget(getW(node, EDGE_WIDGET), ve || modeWidget?.value !== MODE_FL);  // 首尾帧模式不起作用，隐藏
        showWidget(getW(node, GLOBAL_INPUT), true);
        showWidget(getW(node, LOCAL_INPUT), true);
        showWidget(getW(node, JOIN_WIDGET), false);  // 段间衔接已取消（衔接方式在前缀复制节点上选）；控件只为兼容旧工作流而保留
        showWidget(getW(node, FPS_WIDGET), !ve && modeWidget?.value !== MODE_FL);  // 只对「参考生视频」模式的参考视频起作用；视频编辑固定 24 帧、首尾帧没有参考视频
        showWidget(getW(node, HEAD_WIDGET), false);  // 重叠帧数已不在本节点设置（读前缀复制节点）；控件只为兼容旧工作流而保留
        app.graph?.setDirtyCanvas?.(true, true);
    };
    let wasVE = isVE();
    const editor = {
        root: wrap,
        load: () => { wasVE = isVE(); applyMode(); cur().load(); },
        rerender: () => cur().rerender(),
        getState: () => cur().getState(),
        validDone: () => cur().validDone(),
        setProgress: (...a) => cur().setProgress(...a),
        getTask: () => cur().getTask(),
        refreshLive: () => cur().refreshLive(),
        setRunText: (t) => cur().setRunText(t),
    };
    if (modeWidget) {
        const cb = modeWidget.callback;
        modeWidget.callback = function () {
            const r = cb?.apply(this, arguments);
            try {
                applyMode();
                if (isVE() !== wasVE) { wasVE = isVE(); editor.load(); } else cur().rerender();  // 在两种老模式之间切换只重画；进出视频编辑才重新读取数据
            } catch (err) { console.error("[H3 多段输入] 切换生成模式失败：", err); }
            return r;
        };
    }
    const mpw = getW(node, EDGE_WIDGET);
    if (mpw) {
        const cb = mpw.callback;
        mpw.callback = function () {
            const r = cb?.apply(this, arguments);
            try { if (isVE()) { vid.refreshLive(); vid.rerender(); } } catch (_) {}
            return r;
        };
    }
    const dom = node.addDOMWidget("h3ms_editor", "h3ms", wrap, {
        serialize: false,
        hideOnZoom: false,
        getMinHeight: () => (isVE() ? 560 : 480),
    });
    if (dom) dom.computeSize = (w) => [w, isVE() ? 720 : 600];
    node.__h3ms = editor;
    const curSz = node.size || [0, 0];
    node.setSize?.([Math.max(curSz[0], 760), Math.max(curSz[1], 1000)]);
    editor.load();
}

const SEG_WIDGET = "输出第几段";
const TASK_WIDGET = "任务编号";
const TOEND_WIDGET = "到最后一段";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let runningAll = false;
// 全部运行时，每一段要送给后端的「输出第几段」：直接改发出去的请求，不依赖界面控件的当前值。
const forcedSeg = new Map();
let forcedTask = "";   // 回退到界面逐段提交时，每一段都带上的任务编号
let captureJob = null; // 抓取模式：让官方 queuePrompt 照常序列化整张图，但不真的提交，只把要提交的内容截下来
let submitJob = null; // 正在提交的任务，用来记下每一段的 prompt_id
let partialCall = 0;  // 「执行到选定的输出节点」进行中：不接管，交给官方只跑选中的部分
function installApiForce() {
    if (!api || typeof api.queuePrompt !== "function" || api.__h3msForce) return;
    const origQ = api.queuePrompt.bind(api);
    api.queuePrompt = function (number, data, ...rest) {
        if (captureJob) {
            if (!captureJob.data) captureJob.data = data;
            return Promise.resolve({ prompt_id: "h3-capture", number: 0, node_errors: {} });
        }
        // 最底层拦截：不管界面是从哪个入口提交的，只要提交内容里有「输出第几段=0」的 H3 节点，就改成跑全部段。
        try {
            if (!runningAll && !submitJob && !partialCall && !forcedSeg.size && data?.output && !rest[0]?.h3skip && !(rest[0]?.partialExecutionTargets?.length)) {
                const zero = Object.entries(data.output)
                    .filter(([, e]) => e?.class_type === NODE_NAME && e.inputs && (Number(e.inputs[SEG_WIDGET]) === 0
                        || (e.inputs[TOEND_WIDGET] === true && Number(e.inputs[SEG_WIDGET]) >= 1 && !e.inputs[TASK_WIDGET])))
                    .map(([id]) => String(id));
                if (zero.length) {
                    let targets = [];
                    try { targets = runAllTargets().filter((t) => zero.includes(String(t.node.id))); } catch (err) { console.error("[H3 多段输入] 查找要全部运行的节点失败：", err); }
                    if (!targets.length) {
                        try {
                            const live = liveNodes(NODE_NAME);
                            console.warn(`[H3 多段输入] 提交里有 ${zero.length} 个「输出第几段=0」的节点（id ${zero.join(",")}），但界面这边找不到可接管的节点。画面里当前有 ${live.length} 个 H3 多段输入节点：`);
                            for (const n of live) {
                                let parsed = "ok";
                                try { normalizeState(JSON.parse(getW(n, DATA_WIDGET)?.value || "{}")); } catch (e) { parsed = "时间线数据解析失败：" + e; }
                                console.warn(`  节点 ${n.id}：输出第几段=${JSON.stringify(getW(n, SEG_WIDGET)?.value)}，到最后一段=${JSON.stringify(getW(n, TOEND_WIDGET)?.value)}，有时间线数据控件=${!!getW(n, DATA_WIDGET)}，${parsed}`);
                            }
                        } catch (e) { console.warn("[H3 多段输入] 诊断失败：", e); }
                    }
                    if (targets.length) {
                        console.log("[H3 多段输入] 点运行时由最底层接管，开始跑全部段。");
                        doRunAll(targets, async () => data, null, null);
                        return Promise.resolve({ prompt_id: "h3-runall", number: 0, node_errors: {} });
                    }
                }
            }
        } catch (err) { console.error("[H3 多段输入] 接管运行失败：", err); }
        try {
            if (forcedSeg.size && data?.output) {
                for (const [id, k] of forcedSeg) {
                    const e = data.output[id];
                    if (e?.inputs && SEG_WIDGET in e.inputs) {
                        if (e.inputs[SEG_WIDGET] !== k) console.warn(`[H3 多段输入] 提交的「输出第几段」是 ${e.inputs[SEG_WIDGET]}，已改正为 ${k}。`);
                        e.inputs[SEG_WIDGET] = k;
                        if (forcedTask && TASK_WIDGET in e.inputs) e.inputs[TASK_WIDGET] = forcedTask;
                    }
                }
            }
        } catch (err) { console.error("[H3 多段输入] 改写提交内容失败：", err); }
        const ret = origQ(number, data, ...rest);
        if (submitJob && ret && typeof ret.then === "function") {
            const job = submitJob;
            return ret.then((res) => {
                try {
                    if (res?.prompt_id) {
                        job.lastId = res.prompt_id;
                        job.ids.push(res.prompt_id);
                        job.segOf.set(res.prompt_id, job.cur);
                    }
                } catch (_) {}
                return res;
            });
        }
        return ret;
    };
    api.__h3msForce = true;
    console.log("[H3 多段输入] 前端已加载：「输出第几段=0」跑全部段已启用。");
}

/* 工作流存档里控件值是按位置存的，位置一旦和载入时的控件顺序不一致就会整体错位（最大边长变 0 等）。
 * 所以保存时再按名字把这几个控件的值写进「时间线数据」的 cfg 里，载入时按名字放回去，和控件排列顺序无关。 */
const CFG_WIDGETS = () => [SEG_WIDGET, TOEND_WIDGET, EDGE_WIDGET, FPS_WIDGET, TASK_WIDGET, JOIN_WIDGET, HEAD_WIDGET];
function stampCfg(node, o) {
    if (!o || !Array.isArray(o.widgets_values)) return;
    const ser = (node.widgets || []).filter((w) => w.options?.serialize !== false);
    const di = ser.findIndex((w) => w.name === DATA_WIDGET);
    if (di < 0 || typeof o.widgets_values[di] !== "string") return;
    let obj;
    try { obj = o.widgets_values[di].trim() ? JSON.parse(o.widgets_values[di]) : {}; } catch (_) { return; }
    if (!obj || typeof obj !== "object" || Array.isArray(obj)) return;
    const cfg = {};
    for (const nm of CFG_WIDGETS()) { const w = getW(node, nm); if (w) cfg[nm] = w.value; }
    obj.cfg = cfg;
    o.widgets_values[di] = JSON.stringify(obj);
}
function restoreCfg(node) {
    const dw = getW(node, DATA_WIDGET);
    if (!dw || typeof dw.value !== "string" || !dw.value.includes('"cfg"')) return false;
    let obj;
    try { obj = JSON.parse(dw.value); } catch (_) { return false; }
    const cfg = obj && obj.cfg;
    if (!cfg || typeof cfg !== "object") return false;
    const isInt = (v) => typeof v === "number" && Number.isFinite(v) && Math.floor(v) === v;
    const ok = {
        [SEG_WIDGET]: (v) => isInt(v) && v >= 0,
        [TOEND_WIDGET]: (v) => typeof v === "boolean",
        [EDGE_WIDGET]: (v) => typeof v === "number" && Number.isFinite(v) && v >= 0,
        [TASK_WIDGET]: (v) => typeof v === "string",
        [JOIN_WIDGET]: (v) => JOIN_OPTIONS.includes(v),
        [HEAD_WIDGET]: (v) => isInt(v),
        [FPS_WIDGET]: (v) => typeof v === "number" && Number.isFinite(v) && v >= 0,
    };
    for (const nm of CFG_WIDGETS()) {
        const w = getW(node, nm);
        if (w && nm in cfg && ok[nm](cfg[nm])) w.value = cfg[nm];
    }
    delete obj.cfg;           // cfg 只存在于保存的文件里，界面和后端用的数据里不带它
    dw.value = Object.keys(obj).length ? JSON.stringify(obj) : "";
    return true;
}

/** 按类型自修复控件值：旧版本/控件顺序变动造成的错位（例如「到最后一段」里是空文本、「任务编号」里是「复制」）一律纠正。 */
function healWidgets(node, oldFile) {
    const isInt = (v) => typeof v === "number" && Number.isFinite(v) && Math.floor(v) === v;
    const fpsW = getW(node, FPS_WIDGET);
    if (fpsW && !(typeof fpsW.value === "number" && Number.isFinite(fpsW.value) && fpsW.value >= 0)) fpsW.value = 0;  // 旧版本该位置是「延续提示词」(布尔)，一律纠正成 0
    const seg = getW(node, SEG_WIDGET), te = getW(node, TOEND_WIDGET), ed = getW(node, EDGE_WIDGET);
    const tk = getW(node, TASK_WIDGET), jn = getW(node, JOIN_WIDGET), hd = getW(node, HEAD_WIDGET);
    let rescued = null;  // 错位到别的控件里的「段间衔接」选项
    for (const w of [te, tk]) if (w && typeof w.value === "string" && JOIN_OPTIONS.includes(w.value)) rescued = w.value;
    if (seg && !(isInt(seg.value) && seg.value >= 0)) seg.value = 0;
    if (te && typeof te.value !== "boolean") te.value = false;
    if (ed && !(typeof ed.value === "number" && Number.isFinite(ed.value) && ed.value >= 0)) ed.value = 0;
    if (tk && !(typeof tk.value === "string" && /^\d{8}_\d{6}$/.test(tk.value))) tk.value = "";
    if (hd && !isInt(hd.value)) hd.value = 22;
    if (jn && !JOIN_OPTIONS.includes(jn.value)) jn.value = rescued || (oldFile ? "硬切" : "复制");
    else if (jn && oldFile) jn.value = "硬切";
}

const liveNodes = (type) => (app.graph?._nodes || []).filter((n) => n.type === type && n.mode !== 2 && n.mode !== 4);
const getW = (node, name) => node?.widgets?.find((w) => w.name === name);
function setW(node, name, value) {
    const w = getW(node, name);
    if (!w) return false;
    w.value = value;
    try { w.callback?.(value); } catch (_) {}
    return true;
}

/** 工作流里「H3 潜空间前缀复制」节点上选的衔接方式（没有这个节点返回 null） */
function graphPrefixJoin() {
    try {
        for (const n of liveNodes(PREFIX_NODE)) {
            const v = getW(n, "衔接方式")?.value;
            if (JOIN_OPTIONS.includes(v)) return v;
        }
    } catch (_) {}
    return null;
}

function runAllTargets() {
    const out = [];
    for (const n of liveNodes(NODE_NAME)) {
        const sw = getW(n, SEG_WIDGET);
        const dw = getW(n, DATA_WIDGET);
        const segv = Number(sw?.value);
        const toEnd = !!getW(n, TOEND_WIDGET)?.value;
        if (!sw || !dw || !(segv === 0 || (toEnd && segv >= 1))) continue;
        let st = null;
        try { st = normalizeState(JSON.parse(dw.value || "{}")); } catch (_) {}
        if (!st) continue;
        const pj = graphPrefixJoin();
        const dj = pj ? (pj === "图片引导" ? "硬切" : pj) : (getW(n, MODE_WIDGET)?.value === MODE_FL ? "硬切" : "复制");
        const isve = getW(n, MODE_WIDGET)?.value === MODE_VE;
        out.push({ node: n, sw, orig: segv, from: segv === 0 ? 1 : segv, count: st.segments.length, joins: isve ? veJoins(st) : st.segments.map((_x, k) => (k === 0 ? "硬切" : dj)) });
    }
    return out;
}

// 跑全部段时，自动让「保存/读取/修剪」节点的编号与段号一致（每种节点图里恰好一个才改，否则只提示）。
const evPid = (ev) => ev?.detail?.prompt_id ?? null;
const pad2 = (n) => String(n).padStart(2, "0");
function newTaskId() {
    const d = new Date();
    return `${d.getFullYear()}${pad2(d.getMonth() + 1)}${pad2(d.getDate())}_${pad2(d.getHours())}${pad2(d.getMinutes())}${pad2(d.getSeconds())}`;
}

// 界面上的事件也写到 ComfyUI 终端和任务日志里（失败也不影响运行）
function h3Log(task, msg, level) {
    try { console[level === "error" ? "error" : level === "warn" ? "warn" : "log"](`[H3 多段输入] ${msg}`); } catch (_) {}
    try {
        api.fetchApi("/h3/log", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ task, msg, level: level || "info" }),
        }).catch(() => {});
    } catch (_) {}
}

// 把已排队但还没跑的段从队列里删掉（只删这个任务自己的）
async function dropPending(ids) {
    if (!ids.length) return;
    try {
        await api.fetchApi("/queue", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ delete: ids }),
        });
    } catch (err) {
        console.warn("[H3 多段输入] 删除排队中的后续段失败：", err);
    }
}

// 盯着这个任务的各段：每段成功就记进度；任何一段出错/被停止，弹出明确提示（用后端提交时，后面没跑的段由后端删除）。
function watchJob(job, ed) {
    const finish = () => {
        api.removeEventListener("execution_success", onOk);
        api.removeEventListener("execution_error", onErr);
        api.removeEventListener("execution_interrupted", onInt);
        if (ed.__h3msJob === job) { ed.setRunText(""); ed.__h3msJob = null; }
    };
    const mine = (ev) => {
        const id = evPid(ev);
        return id == null ? job.ids.length > job.done : job.ids.includes(id);
    };
    const onOk = (ev) => {
        if (job.over || !mine(ev)) return;
        const id = evPid(ev);
        const seg = id != null ? job.segOf.get(id) : job.first + job.done;
        if (!seg) return;
        job.done += 1;
        h3Log(job.task, `第 ${seg}/${job.total} 段运行完成`);
        try { ed.setProgress(seg, job.task); } catch (err) { console.error("[H3 多段输入] 记录进度失败：", err); }
        if (job.submissionDone && job.done >= job.ids.length) {
            job.over = true; finish();
        } else if (ed.__h3msJob === job) {
            ed.setRunText(`正在运行……已完成 ${seg}/${job.total} 段`);
        }
    };
    const stop = async (ev, why) => {
        if (job.over || !mine(ev)) return;
        job.over = true;
        const id = evPid(ev);
        const seg = id != null ? job.segOf.get(id) : job.first + job.done;
        const rest = job.ids.filter((x) => x !== id && job.segOf.get(x) > (seg || 0));
        if (!job.viaBackend) await dropPending(rest);
        finish();
        const detail = ev?.detail || {};
        const reason = detail.exception_message ? `\n原因：${String(detail.exception_message).trim().slice(0, 400)}${detail.node_type ? `（节点 ${detail.node_type}）` : ""}` : "";
        const msg = `第 ${seg} 段${why}，已完成 ${(seg || job.first) - 1}/${job.total} 段，后面排队的 ${rest.length} 段已取消。下次把「输出第几段」设为 ${seg}、打开「到最后一段」，可从第 ${seg} 段继续。`;
        h3Log(job.task, msg + reason.replace(/\n/g, " "), "error");
        try { alert(`[H3 多段输入] ${msg}${reason}\n\n任务编号：${job.task}，日志在 output/h3_logs/${job.task}.log`); } catch (_) {}
    };
    const onErr = (ev) => stop(ev, "运行出错");
    const onInt = (ev) => stop(ev, "被停止");
    api.addEventListener("execution_success", onOk);
    api.addEventListener("execution_error", onErr);
    api.addEventListener("execution_interrupted", onInt);
    return { finish };
}

// 回退办法：后端接口不可用时，由界面把各段依次提交（不等上一段跑完）
async function submitFromBrowser(original, args, targets, main, ed, start, total, task) {
    const job = { task, ids: [], segOf: new Map(), total, done: 0, first: start + 1, cur: 0, submissionDone: false, over: false, lastId: null, viaBackend: false };
    const watcher = watchJob(job, ed);
    ed.__h3msJob = job;
    submitJob = job;
    forcedTask = task;
    try {
        for (let i = start + 1; i <= total; i++) {
            for (const t of targets) {
                const k = Math.min(i, t.count);
                t.sw.value = k;
                try { t.sw.callback?.(k); } catch (_) {}
                forcedSeg.set(String(t.node.id), k);
            }
            ed.setRunText(`正在提交第 ${i}/${total} 段……`);
            job.lastId = null;
            job.cur = i;
            h3Log(task, `（界面提交）提交第 ${i}/${total} 段`);
            await original(...args);
            if (!job.lastId) {
                h3Log(task, `第 ${i} 段没有成功提交，后面的段不再提交。`, "error");
                await dropPending(job.ids);
                job.over = true; watcher.finish();
                try { alert(`[H3 多段输入] 第 ${i} 段没有成功提交（请看界面上的错误提示）。\n已提交的后续段已取消。`); } catch (_) {}
                return;
            }
        }
        job.submissionDone = true;
        if (!job.over) {
            ed.setRunText(`已提交 ${job.ids.length} 段，运行中……`);
            if (job.done >= job.ids.length) { job.over = true; watcher.finish(); }
        }
    } catch (err) {
        h3Log(task, `提交出现异常：${err?.message || err}`, "error");
        try { await dropPending(job.ids); } catch (_) {}
        job.over = true; watcher.finish();
        try { alert(`[H3 多段输入] 提交时出现异常：${err?.message || err}\n已提交的后续段已取消。`); } catch (_) {}
    } finally {
        submitJob = null;
        forcedTask = "";
        forcedSeg.clear();
    }
}

// 「输出第几段」= 0：点一次运行，由后端把全部段依次放进队列（不依赖浏览器页面一直开着）；
// 后端接口不可用时回退成界面逐段提交。每一段出错/被停止，后面没跑的段自动取消；记录已完成的段数，下次可以继续。
// 「输出第几段」= 0 时跑全部段的主流程：getData() 返回要提交的内容；submitFallback 是后端不可用时的回退。
async function doRunAll(targets, getData, submitFallback, fallbackOriginal) {
        if (runningAll) {
            console.warn("[H3 多段输入] 正在提交上一次的任务，请稍等一两秒再点。");
            return;
        }
        const main = targets[0];
        const total = main.count;
        const ed = main.node.__h3ms;
        if (total < 1 || !ed) return fallbackOriginal ? fallbackOriginal() : undefined;

        // 「输出第几段」= 0：新开任务从第 1 段跑到最后。
        // 「输出第几段」= N 且打开「到最后一段」：沿用上次的任务，从第 N 段跑到最后（前面的段必须已经跑完、内容没改过）。
        let start = 0;
        let task = "";
        const from = Math.max(1, main.from || 1);
        if (from > 1) {
            const done = ed.validDone();
            const oldTask = ed.getTask();
            if (from > total) {
                alert(`[H3 多段输入] 「输出第几段」是 ${from}，但只有 ${total} 段。`);
                return;
            }
            if (!oldTask || done < from - 1) {
                alert(`[H3 多段输入] 要从第 ${from} 段开始，需要前面 ${from - 1} 段已经在同一个任务里跑完、内容没有改过。\n`
                    + `现在只有前 ${done} 段是完整可用的。\n`
                    + (done > 0 ? `请把「输出第几段」设为 ${done + 1} 再运行，或设为 0 从头跑。` : "请把「输出第几段」设为 0，从头跑。"));
                return;
            }
            start = from - 1;
            task = oldTask;
        }
        if (!task) task = newTaskId();
        ed.setProgress(start, task);

        runningAll = true;
        try {
            h3Log(task, `点击运行：任务 ${task}，第 ${start + 1}~${total} 段（共 ${total} 段）${start > 0 ? "（沿用上次的任务，前面的段已完成）" : ""}`);
            let res;
            try {
                // 拿到要提交的内容：从官方 queuePrompt 截取（Get/Set 之类的虚拟节点也会处理好），或直接用已经拿到的
                const p = await getData();
                if (!p?.output) throw new Error("没有截到要提交的内容");
                const body = {
                    output: p.output, workflow: p.workflow ?? p.extra_data?.extra_pnginfo?.workflow, client_id: api.clientId, task,
                    start: start + 1, total, nodes: targets.map((t) => ({ id: String(t.node.id), count: t.count })),
                };
                const resp = await api.fetchApi("/h3/run_all", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
                res = resp.ok ? await resp.json() : { ok: false, need_fallback: true, error: `HTTP ${resp.status}` };
            } catch (err) {
                res = { ok: false, need_fallback: true, error: String(err?.message || err) };
            }
            if (res.ok) {
                const job = { task, ids: res.ids || [], segOf: new Map(), total, done: 0, first: start + 1, cur: 0, submissionDone: true, over: false, viaBackend: true };
                (res.ids || []).forEach((id, k) => job.segOf.set(id, (res.segs || [])[k]));
                ed.__h3msJob = job;
                watchJob(job, ed);
                ed.setRunText(`已提交 ${job.ids.length} 段，运行中……`);
                h3Log(task, `已由后端提交 ${job.ids.length} 段。日志：${res.log || ""}`);
            } else if (res.need_fallback) {
                h3Log(task, `后端接口不可用（${res.error}），改用界面逐段提交。`, "warn");
                if (submitFallback) await submitFallback({ targets, main, ed, start, total, task });
                else {
                    ed.setRunText("");
                    try { alert(`[H3 多段输入] 后端接口不可用（${res.error}），没法自动跑全部段。请把「输出第几段」改成具体的段号，一段一段运行。`); } catch (_) {}
                }
            } else {
                h3Log(task, `提交失败：${res.error}`, "error");
                ed.setRunText("");
                try { alert(`[H3 多段输入] 提交失败：${res.error}\n任务编号：${task}，日志在 output/h3_logs/${task}.log`); } catch (_) {}
            }
        } catch (err) {
            h3Log(task, `运行出现异常：${err?.message || err}`, "error");
            ed.setRunText("");
            try { alert(`[H3 多段输入] 运行出现异常：${err?.message || err}`); } catch (_) {}
        } finally {
            runningAll = false;
            for (const t of targets) {
                const o = Number.isFinite(t.orig) ? t.orig : 0;
                t.sw.value = o;
                try { t.sw.callback?.(o); } catch (_) {}
            }
            app.graph?.setDirtyCanvas?.(true, true);
        }
    
}

function installRunAll() {
    installApiForce();  // 最底层的拦截（api.queuePrompt）：不管界面用哪个入口提交，都能接管「输出第几段=0」
    if (app.__h3msRunAll || typeof app.queuePrompt !== "function") return;
    const original = app.queuePrompt.bind(app);
    app.queuePrompt = async function (...args) {
        // 点的是「执行到选定的输出节点」：带着「只执行这些节点」的参数，直接交给官方，只跑选中的部分
        const only = args[2];
        if (only && (!Array.isArray(only) || only.length > 0)) {
            partialCall++;
            try { return await original(...args); } finally { partialCall--; }
        }
        let targets = [];
        try { targets = runAllTargets(); } catch (err) { console.error("[H3 多段输入] 查找要全部运行的节点失败：", err); }
        if (!targets.length) return original(...args);
        const capture = async () => {
            captureJob = {};
            try { await original(...args); } finally { var cap = captureJob; captureJob = null; }
            return cap?.data;
        };
        return doRunAll(targets, capture, (ctx) => submitFromBrowser(original, args, ctx.targets, ctx.main, ctx.ed, ctx.start, ctx.total, ctx.task), () => original(...args));
    };
    app.__h3msRunAll = true;
}

/* ----------------------------------------------------------------- 潜空间读取节点：「自动」时隐藏「读取编号」 */
const OLD_LOAD_TYPE = "MiniMaxH3LatentLoad";   // 已并进前缀复制节点；这里只用来迁移旧工作流
/** 前缀复制节点：读取方式选「自动」时，隐藏「读取文件夹或文件」「读取编号」 */
function patchLoadNode(nodeType) {
    const hideW = (w, hide) => {
        if (!w) return;
        if (!hide) {
            w.hidden = false;
            if (w.__h3Compute !== undefined) {
                if (w.__h3Compute) w.computeSize = w.__h3Compute; else delete w.computeSize;
                w.__h3Compute = undefined;
            }
        } else {
            w.hidden = true;
            if (w.__h3Compute === undefined) w.__h3Compute = Object.prototype.hasOwnProperty.call(w, "computeSize") ? w.computeSize : null;
            w.computeSize = () => [0, -4];
        }
    };
    const refresh = (node) => {
        const mw = getW(node, "读取方式");
        if (!mw) return;
        // 旧版保存在工作流里的取值
        if (typeof mw.value === "string") {
            if (mw.value.startsWith("自动") && mw.value !== "自动") mw.value = "自动";
            else if (mw.value.startsWith("手动") && mw.value !== "手动") mw.value = "手动";
        }
        const hide = mw.value !== "手动";
        hideW(getW(node, "读取编号"), hide);
        hideW(getW(node, "读取文件夹或文件"), hide);
        try {
            const sz = node.computeSize?.();
            if (sz && node.size) node.setSize?.([node.size[0], Math.max(sz[1], 30)]);
        } catch (_) {}
        app.graph?.setDirtyCanvas?.(true, true);
    };
    const onCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
        const r = onCreated?.apply(this, arguments);
        const mw = getW(this, "读取方式");
        if (mw) {
            const cb = mw.callback;
            mw.callback = (...a) => {
                const ret = cb?.apply(mw, a);
                try { refresh(this); } catch (_) {}
                return ret;
            };
        }
        try { refresh(this); } catch (_) {}
        return r;
    };
    const onConfigure = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function () {
        const r = onConfigure?.apply(this, arguments);
        try { refresh(this); } catch (_) {}
        return r;
    };
}


// 旧版「H3 潜空间前缀复制」有 conditioning 入口/出口和「音频上下文长度」「音频衔接」两个控件，已删除；
// 打开旧工作流时把控件值和连线顺序一并迁移，免得错位。
function migratePrefixCopyNodes(nodes, links) {
    if (!Array.isArray(nodes)) return;
    const MODES = ["复制", "引导", "图片引导", "硬切"];
    const L = (l) => Array.isArray(l)
        ? { id: l[0], o: l[1], os: l[2], t: l[3], ts: l[4] }
        : { id: l.id, o: l.origin_id, os: l.origin_slot, t: l.target_id, ts: l.target_slot };
    const setTs = (l, v) => { if (Array.isArray(l)) l[4] = v; else l.target_slot = v; };
    const setOs = (l, v) => { if (Array.isArray(l)) l[2] = v; else l.origin_slot = v; };
    for (const n of nodes) {
        if (!n || n.type !== PREFIX_NODE) continue;
        // 目标布局：[重叠帧数, 衔接方式, 音频衔接, 重绘幅度]；下面每一步都只处理旧布局，重复执行不会再改
        const wv = n.widgets_values;
        if (Array.isArray(wv) && wv.length === 5 && ["复制", "引导", "硬切"].includes(wv[2])
            && ["复制", "关键帧"].includes(wv[3])) {
            n.widgets_values = [wv[0], wv[2], "关键帧", wv[4]];       // v12 及更早：重叠帧数、音频上下文长度、衔接方式、音频衔接、重绘幅度
        } else if (Array.isArray(wv) && wv.length === 3 && MODES.includes(wv[1]) && typeof wv[2] === "number") {
            n.widgets_values = [wv[0], wv[1], "关键帧", wv[2]];       // v13~v16：重叠帧数、衔接方式、重绘幅度
        } else if (Array.isArray(wv) && wv.length === 2 && typeof wv[1] === "number") {
            n.widgets_values = [wv[0], "复制", "关键帧", wv[1]];      // v14：重叠帧数、重绘幅度
        }
        // conditioning 输入口：放在「衔接方式 / 音频衔接 / 重绘幅度 / 模型 / sigmas」之前
        if (Array.isArray(n.inputs) && !n.inputs.some((i) => i && i.name === "conditioning")) {
            let at = n.inputs.findIndex((i) => i && ["衔接方式", "音频衔接", "重绘幅度", "模型", "sigmas"].includes(i.name));
            if (at < 0) at = n.inputs.length;
            if (Array.isArray(links)) {
                for (const l of links) { const d = L(l); if (d.t === n.id && d.ts >= at) setTs(l, d.ts + 1); }
            }
            n.inputs.splice(at, 0, { name: "conditioning", type: "CONDITIONING", link: null });
        }
        // conditioning 输出口：第 2 个（latent, conditioning, model）
        if (Array.isArray(n.outputs) && !n.outputs.some((o) => o && o.name === "conditioning")) {
            const at = Math.min(1, n.outputs.length);
            if (Array.isArray(links)) {
                for (const l of links) { const d = L(l); if (d.o === n.id && d.os >= at) setOs(l, d.os + 1); }
            }
            n.outputs.splice(at, 0, { name: "conditioning", type: "CONDITIONING", links: null });
            n.outputs.forEach((o, k) => { if (o && o.slot_index !== undefined) o.slot_index = k; });
        }
    }
}

/** 「H3 潜空间读取」已并进前缀复制：取它的控件值，摘掉它和「上一段原始潜空间」口（重复执行不再变）。 */
function mergeLoadIntoPrefix(nodes, links) {
    if (!Array.isArray(nodes)) return;
    const L = (l) => Array.isArray(l)
        ? { id: l[0], o: l[1], os: l[2], t: l[3], ts: l[4] }
        : { id: l.id, o: l.origin_id, os: l.origin_slot, t: l.target_id, ts: l.target_slot };
    const setTs = (l, v) => { if (Array.isArray(l)) l[4] = v; else l.target_slot = v; };
    const dropLink = (k) => {
        const d = L(links[k]);
        const src = nodes.find((x) => x && x.id === d.o);
        const out = src?.outputs?.[d.os];
        if (out && Array.isArray(out.links)) out.links = out.links.filter((x) => x !== d.id);
        const dst = nodes.find((x) => x && x.id === d.t);
        const inp = dst?.inputs?.[d.ts];
        if (inp && inp.link === d.id) inp.link = null;
        links.splice(k, 1);
    };
    for (const n of nodes) {
        if (!n || n.type !== PREFIX_NODE) continue;
        let loaded = null;
        const idx = Array.isArray(n.inputs) ? n.inputs.findIndex((i) => i && i.name === "上一段原始潜空间") : -1;
        if (idx >= 0) {
            if (Array.isArray(links)) {
                for (let k = links.length - 1; k >= 0; k--) {
                    const d = L(links[k]);
                    if (d.t !== n.id) continue;
                    if (d.ts === idx) {
                        const src = nodes.find((x) => x && x.id === d.o);
                        if (src && src.type === OLD_LOAD_TYPE && Array.isArray(src.widgets_values)) loaded = src.widgets_values;
                        dropLink(k);
                    } else if (d.ts > idx) setTs(links[k], d.ts - 1);
                }
            }
            n.inputs.splice(idx, 1);
        }
        const wv = n.widgets_values;
        if (Array.isArray(wv) && wv.length === 4) {
            // 读取节点的控件值是 [文件夹, 编号, 编号后的自动递增, 读取方式]，和新增的四项顺序一致
            const ok = loaded && loaded.length >= 4 && typeof loaded[0] === "string" && typeof loaded[1] === "number";
            n.widgets_values = [...wv, ...(ok ? [loaded[0], loaded[1], loaded[2] || "fixed", loaded[3]] : ["h3_latent_chain", 0, "fixed", "自动"])];
        }
    }
    for (let i = nodes.length - 1; i >= 0; i--) {
        const n = nodes[i];
        if (!n || n.type !== OLD_LOAD_TYPE) continue;
        if (Array.isArray(links)) {
            for (let k = links.length - 1; k >= 0; k--) if (L(links[k]).o === n.id) dropLink(k);
        }
        nodes.splice(i, 1);
    }
}

function dropPrefixInputs(nodes, links) {
    if (!Array.isArray(nodes)) return;
    for (const n of nodes) {
        if (!n || n.type !== PREFIX_NODE || !Array.isArray(n.inputs)) continue;
        for (const name of ["衔接方式(来自多段输入)"]) {
            const idx = n.inputs.findIndex((i) => i && i.name === name);
            if (idx < 0) continue;
            if (Array.isArray(links)) {
                for (let k = links.length - 1; k >= 0; k--) {
                    const l = links[k];
                    const id = Array.isArray(l) ? l[0] : l.id, o = Array.isArray(l) ? l[1] : l.origin_id, os = Array.isArray(l) ? l[2] : l.origin_slot;
                    const t = Array.isArray(l) ? l[3] : l.target_id, ts = Array.isArray(l) ? l[4] : l.target_slot;
                    if (t !== n.id) continue;
                    if (ts === idx) {
                        const out = nodes.find((x) => x && x.id === o)?.outputs?.[os];
                        if (out && Array.isArray(out.links)) out.links = out.links.filter((x) => x !== id);
                        links.splice(k, 1);
                    } else if (ts > idx) {
                        if (Array.isArray(l)) l[4] = ts - 1; else l.target_slot = ts - 1;
                    }
                }
            }
            n.inputs.splice(idx, 1);
        }
    }
}

// 旧版「H3 多段输入」有「衔接方式」输出口，已删除（衔接方式改在「H3 潜空间前缀复制」的下拉框里选）；
// 打开旧工作流时把它去掉，后面输出口上的连线一起往前挪一位。
function dropMultiSegJoinOutput(nodes, links) {
    if (!Array.isArray(nodes)) return;
    for (const n of nodes) {
        if (!n || n.type !== "H3MultiSegmentInput" || !Array.isArray(n.outputs)) continue;
        const idx = n.outputs.findIndex((o) => o && o.name === "衔接方式");
        if (idx < 0) continue;
        if (Array.isArray(links)) {
            for (let k = links.length - 1; k >= 0; k--) {
                const l = links[k];
                const arr = Array.isArray(l);
                const id = arr ? l[0] : l.id, o = arr ? l[1] : l.origin_id, os = arr ? l[2] : l.origin_slot;
                const t = arr ? l[3] : l.target_id, ts = arr ? l[4] : l.target_slot;
                if (o !== n.id) continue;
                if (os === idx) {
                    const inp = nodes.find((x) => x && x.id === t)?.inputs?.[ts];
                    if (inp && inp.link === id) inp.link = null;
                    links.splice(k, 1);
                } else if (os > idx) {
                    if (arr) l[2] = os - 1; else l.origin_slot = os - 1;
                }
            }
        }
        n.outputs.splice(idx, 1);
    }
}

// 旧版「H3 视频合并」有已停用的「合并文件名」控件，现已删除并新增「手动修剪帧数」；旧版「H3 续接重叠帧修剪」
// 已并进合并节点：打开旧工作流时迁移控件值，并把修剪节点从连线中摘掉（图像/音频直通）。
function migrateMergeAndTrim(nodes, links) {
    if (!Array.isArray(nodes)) return;
    for (const n of nodes) {
        if (!n || n.type !== "H3VideoMerge") continue;
        let wv = n.widgets_values;
        // 每次切换工作流标签页都会重新加载图，所以每一步都只在「确认是那个旧布局」时才迁移，已经迁移好的不会被再挪一次。
        // ① 最旧布局（有「合并文件名」，第 13 个值是开关「合并后删除各段片段」）→ v27 布局
        if (Array.isArray(wv) && wv.length === 14 && typeof wv[12] === "boolean" && typeof wv[3] === "string"
            && ["解码音频", "原视频音频"].includes(wv[13])) {
            wv = n.widgets_values = [...wv.slice(0, 3), ...wv.slice(4, 13), 0, wv[13]];
        }
        // ② v27 布局（14 项，第 13 个值是数字「手动修剪帧数」）→ 现在的布局（16 项，「当前段号」后面带「运行后控制」，「总段数」后面是「衔接方式」）：
        //    帧率/编码格式/保存名称/音频来源/手动/当前段号/运行后控制/总段数/衔接方式/CRF/编码预设/像素格式/音频码率/保存元数据/合并后删除潜空间/合并后删除各段片段
        if (Array.isArray(wv) && wv.length === 14 && typeof wv[12] === "number" && typeof wv[3] === "string"
            && ["解码音频", "原视频音频"].includes(wv[13])) {
            const manual = Number(wv[9]) > 0 || Number(wv[12]) > 0;  // 以前手动填过总段数/修剪帧数的，保持手动开着
            wv = n.widgets_values = [wv[0], wv[3], wv[1], wv[13], manual, wv[2], "fixed", wv[9], "复制", wv[5], wv[6], wv[4], wv[7], wv[8], wv[10], wv[11]];
        }
        // ③ v30 布局（16 项，第 9 个值是「高级功能」开关）→ 现在的布局（16 项）：高级输入改用 ComfyUI 自带的「显示高级输入」，开关去掉
        if (Array.isArray(wv) && wv.length === 16 && typeof wv[4] === "boolean" && typeof wv[8] === "boolean"
            && ["解码音频", "原视频音频"].includes(wv[3])) {
            wv = n.widgets_values = [...wv.slice(0, 6), "fixed", wv[6], "复制", ...wv.slice(9)];
        }
        // ④ v31 布局（15 项，第 8 个值是数字「手动修剪帧数」）→ 现在的布局（16 项）：去掉「手动修剪帧数」（改成自动判断），
        //    「当前段号」后面补上自带的「运行后控制」（固定）
        if (Array.isArray(wv) && wv.length === 15 && typeof wv[4] === "boolean" && typeof wv[6] === "number" && typeof wv[7] === "number"
            && typeof wv[8] === "number" && ["解码音频", "原视频音频"].includes(wv[3])) {
            wv = n.widgets_values = [...wv.slice(0, 6), "fixed", wv[6], "复制", ...wv.slice(8)];
        }
        // ⑤ v32 布局（15 项，「当前段号」后面是运行后控制）→ 现在的布局（16 项）：「总段数」后面补上「衔接方式」（复制）
        if (Array.isArray(wv) && wv.length === 15 && typeof wv[4] === "boolean" && typeof wv[6] === "string" && typeof wv[7] === "number"
            && typeof wv[8] === "number" && ["解码音频", "原视频音频"].includes(wv[3])) {
            n.widgets_values = [...wv.slice(0, 8), "复制", ...wv.slice(8)];
        }
    }
    if (!Array.isArray(links)) return;
    const L = (l) => Array.isArray(l)
        ? { id: l[0], o: l[1], os: l[2], t: l[3], ts: l[4] }
        : { id: l.id, o: l.origin_id, os: l.origin_slot, t: l.target_id, ts: l.target_slot };
    for (const tn of nodes.filter((x) => x && x.type === "MiniMaxH3OverlapTrim")) {
        const inLink = (name) => {
            const i = (tn.inputs || []).findIndex((x) => x && x.name === name);
            if (i < 0) return null;
            const id = tn.inputs[i].link;
            const k = links.findIndex((l) => L(l).id === id);
            return k < 0 ? null : { k, d: L(links[k]) };
        };
        const src = { 0: inLink("图像"), 1: inLink("音频") };
        for (let k = links.length - 1; k >= 0; k--) {
            const d = L(links[k]);
            if (d.o !== tn.id) continue;
            const from = src[d.os];
            const dst = nodes.find((x) => x && x.id === d.t);
            if (from) {
                if (Array.isArray(links[k])) { links[k][1] = from.d.o; links[k][2] = from.d.os; }
                else { links[k].origin_id = from.d.o; links[k].origin_slot = from.d.os; }
                const so = nodes.find((x) => x && x.id === from.d.o)?.outputs?.[from.d.os];
                if (so && Array.isArray(so.links)) so.links.push(d.id);
            } else {
                if (dst?.inputs?.[d.ts]) dst.inputs[d.ts].link = null;
                links.splice(k, 1);
            }
        }
        for (const key of [0, 1]) {
            const f = src[key];
            if (!f) continue;
            const so = nodes.find((x) => x && x.id === f.d.o)?.outputs?.[f.d.os];
            if (so && Array.isArray(so.links)) so.links = so.links.filter((x) => x !== f.d.id);
            const kk = links.findIndex((l) => L(l).id === f.d.id);
            if (kk >= 0) links.splice(kk, 1);
        }
        nodes.splice(nodes.indexOf(tn), 1);
    }
}

function migrateGraphData(g) {
    if (!g || typeof g !== "object") return;
    migratePrefixCopyNodes(g.nodes, g.links); mergeLoadIntoPrefix(g.nodes, g.links); dropPrefixInputs(g.nodes, g.links); dropMultiSegJoinOutput(g.nodes, g.links); migrateMergeAndTrim(g.nodes, g.links);
    for (const sg of (g.definitions?.subgraphs || [])) { migratePrefixCopyNodes(sg.nodes, sg.links); mergeLoadIntoPrefix(sg.nodes, sg.links); dropPrefixInputs(sg.nodes, sg.links); dropMultiSegJoinOutput(sg.nodes, sg.links); migrateMergeAndTrim(sg.nodes, sg.links); }
}


/* ----------------------------------------------------------------- H3 视频合并：手动开关（高级输入由 ComfyUI 自带的「显示高级输入」折叠） */
const MERGE_NODE = "H3VideoMerge";
const MERGE_MANUAL = ["当前段号", "control_after_generate", "总段数", "衔接方式"];   // 「手动」关着时隐藏（后端也不使用）
function patchMergeNode(nodeType) {
    const apply = (node) => {
        const man = !!getW(node, "手动")?.value;
        for (const nm of MERGE_MANUAL) showWidget(getW(node, nm), man);
        try {
            const sz = node.computeSize?.();
            if (sz && node.size) node.setSize?.([Math.max(node.size[0], sz[0]), sz[1]]);
        } catch (_) {}
        app.graph?.setDirtyCanvas?.(true, true);
    };
    const hook = (node) => {
        for (const nm of ["手动"]) {
            const w = getW(node, nm);
            if (!w || w.__h3fold) continue;
            w.__h3fold = true;
            const cb = w.callback;
            w.callback = function () { const r = cb?.apply(this, arguments); try { apply(node); } catch (_) {} return r; };
        }
    };
    const onCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
        const r = onCreated?.apply(this, arguments);
        try {
            const c = getW(this, "control_after_generate");
            if (c && c.value === "randomize") c.value = "fixed";  // 「当前段号」的运行后控制默认固定（读取旧工作流时由保存的值覆盖）
            hook(this); apply(this);
        } catch (err) { console.error("[H3 视频合并] 折叠初始化失败：", err); }
        return r;
    };
    const onCfg = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function () {
        const r = onCfg?.apply(this, arguments);
        try { hook(this); apply(this); } catch (_) {}
        return r;
    };
}

app.registerExtension({
    name: "H3.MultiSegmentInput",
    async beforeConfigureGraph(graphData) {
        try { migrateGraphData(graphData); } catch (err) { console.error("[H3] 旧工作流迁移失败：", err); }
    },
    async setup() {
        try { installRunAll(); } catch (err) { console.error("[H3 多段输入] 「0=全部段」未能启用：", err); }
    },
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData?.name === PREFIX_NODE) {
            try { patchLoadNode(nodeType); } catch (err) { console.error("[H3] 前缀复制节点界面初始化失败：", err); }
            return;
        }
        if (nodeData?.name === MERGE_NODE) {
            try { patchMergeNode(nodeType); } catch (err) { console.error("[H3] 视频合并节点界面初始化失败：", err); }
            return;
        }
        if (nodeData?.name !== NODE_NAME) return;
        const onCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = onCreated?.apply(this, arguments);
            try {
                setup(this);
            } catch (err) {
                console.error("[H3 多段输入] 编辑器初始化失败，仍可直接编辑「时间线数据」：", err);
            }
            return r;
        };
        const onSer = nodeType.prototype.onSerialize;
        nodeType.prototype.onSerialize = function (o) {
            const r = onSer?.apply(this, arguments);
            try { stampCfg(this, o); } catch (err) { console.error("[H3 多段输入] 保存控件值失败：", err); }
            return r;
        };
        const onConn = nodeType.prototype.onConnectionsChange;
        nodeType.prototype.onConnectionsChange = function (type, index) {
            const r = onConn?.apply(this, arguments);
            try {
                if (type === 1 && [GLOBAL_INPUT, LOCAL_INPUT].includes(this.inputs?.[index]?.name)) this.__h3ms?.rerender();
            } catch (_) {}
            return r;
        };
        const onConfigure = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function (info) {
            const r = onConfigure?.apply(this, arguments);
            this.__h3Configured = true;
            try {
                // 旧版工作流（没有「生成模式」，widgets_values 只有 3 项）：把值挪回正确的位置
                const wv = info?.widgets_values;
                if (Array.isArray(wv) && wv.length === 3 && typeof wv[1] === "number") {
                    setW(this, MODE_WIDGET, MODE_REF);
                    setW(this, SEG_WIDGET, wv[1]);
                    setW(this, "参考视频最大边长", wv[2]);
                }
                // v8 及以前保存的工作流：控件顺序是 时间线数据/生成模式/输出第几段/最大边长/任务编号/到最后一段（6 项；更早是 5 项）
                // 判断依据是值的类型：旧顺序 [..., 最大边长(数字), 任务编号(文字), 到最后一段(布尔)]，新顺序 [..., 到最后一段(布尔), 最大边长(数字), 任务编号(文字), ...]
                if (Array.isArray(wv) && wv.length >= 5 && typeof wv[1] === "string" && typeof wv[3] === "number" && typeof wv[4] === "string") {
                    const old = ["时间线数据", "生成模式", SEG_WIDGET, "参考视频最大边长", TASK_WIDGET, TOEND_WIDGET];
                    old.forEach((nm, k) => { if (k < wv.length) setW(this, nm, wv[k]); });
                    if (wv.length < 6) setW(this, TOEND_WIDGET, false);
                }
                // 保存的顺序有两种：
                //   v35 起：时间线数据/生成模式/输出第几段/到最后一段/最大边长/参考视频帧率(数字)/任务编号/段间衔接/重叠帧数
                //   v34 及以前：时间线数据/生成模式/输出第几段/到最后一段/最大边长/任务编号(文字)/段间衔接/重叠帧数/延续提示词(已删除)
                // 判断依据是第 6 项的类型：数字 = 新顺序；文字或没有 = 旧顺序。
                // 这一步按名字把值放回对应控件，不依赖控件在节点上的实际排列顺序——
                // 切换工作流标签页重新加载时，如果控件当时还是后端定义的顺序（最大边长在到最后一段前面），
                // 按位置赋值会让最大边长拿到「到最后一段」的值而被纠正成 0。重复执行结果不变。
                if (Array.isArray(wv) && wv.length >= 5 && typeof wv[1] === "string" && typeof wv[3] === "boolean" && typeof wv[4] === "number") {
                    const cur = typeof wv[5] === "number"
                        ? ["时间线数据", "生成模式", SEG_WIDGET, TOEND_WIDGET, "参考视频最大边长", FPS_WIDGET, TASK_WIDGET, JOIN_WIDGET, HEAD_WIDGET]
                        : ["时间线数据", "生成模式", SEG_WIDGET, TOEND_WIDGET, "参考视频最大边长", TASK_WIDGET, JOIN_WIDGET, HEAD_WIDGET];
                    cur.forEach((nm, k) => { if (k < wv.length) { const w = getW(this, nm); if (w) w.value = wv[k]; } });
                }
                restoreCfg(this);  // 保存时按名字存下的控件值，优先于上面按位置猜的结果
                const mw = getW(this, MODE_WIDGET);
                if (mw && mw.value !== MODE_FL && mw.value !== MODE_REF && mw.value !== MODE_VE) mw.value = MODE_REF;
                // v9 之前保存的工作流（控件值不足 8 项，没有「段间衔接」）：保持硬切，不擅自改成新默认的「复制」
                const oldFile = Array.isArray(wv) && wv.length < 8;
                healWidgets(this, oldFile);
            } catch (_) {}
            try {
                this.__h3ms?.load();
            } catch (err) {
                console.error("[H3 多段输入] 载入失败：", err);
            }
            return r;
        };
    },
});
