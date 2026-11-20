/** H3 视频编辑 —— 时间轴编辑器（像 PR / AE 的时间轴）。
 *
 * 视频轨：上传视频，点「分割」后在分割处断开一道缝；图片轨：图片块拖到哪几段，那几段的参考图就是它（自动吸附到分割线）。
 * 本文件没有任何 import：需要的函数全部由 h3_multiseg.js 通过 D 传进来（避免循环引用）。
 * 数据只存一份：节点上的「时间线数据」。本编辑器每次修改后，把「图片轨 / 提示词」换算成后端认识的 segments 写回去。
 */

export const VE_OUT = [
    ["提示词", "STRING"], ["衔接方式", "STRING"], ["源视频片段", "IMAGE"], ["长度", "INT"],
    ["重叠帧数", "INT"], ["宽度", "INT"], ["高度", "INT"],
    ...Array.from({ length: 9 }, (_, i) => [`参考图${i + 1}`, "IMAGE"]),
];
const VE_FIXED = 7; // 前 7 个口固定，其后是参考图1..9
const MAX_TRACKS = 9;

const CSS = `
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
.h3ve .clip{position:absolute;top:2px;bottom:2px;border:1px solid #6aa;border-radius:4px;background:#27424a;display:flex;align-items:center;gap:4px;overflow:hidden;cursor:grab;padding:0 14px}
.h3ve .clip.drag{cursor:grabbing;opacity:.9}
.h3ve .clip img{height:100%;aspect-ratio:1/1;object-fit:cover;border-radius:3px;pointer-events:none}
.h3ve .clip .cn{font-size:11px;color:#dff;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;pointer-events:none}
.h3ve .clip .x{position:absolute;right:14px;top:1px;font-size:11px;padding:0 4px;background:#0008;border-radius:3px;cursor:pointer;color:#fcc;z-index:2}
.h3ve .clip .rh{position:absolute;top:0;bottom:0;width:10px;cursor:ew-resize;z-index:3;background:linear-gradient(90deg,#8cc8 0 3px,transparent 3px)}
.h3ve .clip .rh.l{left:0}
.h3ve .clip .rh.r{right:0;transform:scaleX(-1)}
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
let cssDone = false;

export function buildVideoEditEditor(node, dataWidget, D) {
    const { app, el, viewUrl, uploadFile, splitPath, openViewer, hashText, vePlan, veJoins, veTotalFrames, JOINS, VE_AUDIO, VE_FPS, injectCss } = D;
    injectCss();
    if (!cssDone) {
        cssDone = true;
        const s = document.createElement("style");
        s.textContent = CSS;
        document.head.appendChild(s);
    }
    const COLORS = ["#3d6b4f", "#4a5f8a", "#8a5a3d", "#6a4a8a", "#3d7a80", "#8a3d5a", "#6a7a3d", "#5a5a5a"];

    const mkSeg = () => ({ prompt: "", use_common: false, join: null, first: null, last: null, guides: [], images: [], videos: [], audios: [] });
    const blank = () => ({
        version: 1, common: { prompt: "", images: [], videos: [], audios: [] }, segments: [mkSeg()],
        video: { file: "", start: 0, end: 0, duration: 0 }, cuts: [],
        ve: { head: 22, long_edge: 1280, width: 0, height: 0, audio: "静音" },
        vtracks: [[]], pmode: "global", gprompt: "", lprompts: [""],
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

    const root = el("div", { class: "h3ms h3ve" });
    const topBox = el("div");
    const tlBox = el("div");
    const panelBox = el("div");
    root.append(topBox, tlBox, panelBox);

    /* ---------------------------------------------------------------- 数据 */
    const parse = (data) => {
        const s = blank();
        if (!data || typeof data !== "object") return s;
        const v = data.video && typeof data.video === "object" ? data.video : {};
        s.video = { file: String(v.file || "").replace(/\\/g, "/").trim(), start: 0, end: 0, duration: Math.max(0, Number(v.duration) || 0) };
        s.cuts = (Array.isArray(data.cuts) ? data.cuts : []).map(Number).filter(Number.isFinite).sort((a, b) => a - b);
        const n = s.cuts.length + 1;
        s.ve.audio = VE_AUDIO.includes(data.ve?.audio) ? data.ve.audio : "静音";
        s.pmode = data.pmode === "local" ? "local" : "global";
        s.gprompt = String(data.gprompt ?? "");
        s.lprompts = Array.from({ length: n }, (_, i) => String(data.lprompts?.[i] ?? ""));
        s.segments = Array.from({ length: n }, (_, i) => {
            const g = mkSeg();
            const j = data.segments?.[i]?.join;
            g.join = JOINS.includes(j) ? j : null;
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

    const nameOfOut = () => {
        let maxImg = 1;
        for (const s of st.segments) maxImg = Math.max(maxImg, s.images.length);
        return VE_FIXED + Math.min(9, maxImg);
    };
    const syncOutputs = () => {
        if (!Array.isArray(node.outputs) || !node.addOutput || !node.removeOutput) return;
        const want = nameOfOut();
        let touched = false;
        while (node.outputs.length > want) { node.removeOutput(node.outputs.length - 1); touched = true; }
        while (node.outputs.length < want) {
            const d = VE_OUT[node.outputs.length];
            node.addOutput(d[0], d[1]);
            touched = true;
        }
        if (touched) app.graph?.setDirtyCanvas?.(true, true);
    };

    const save = () => {
        derive();
        const text = JSON.stringify(st);
        if (dataWidget.value !== text) {
            dataWidget.value = text;
            try { dataWidget.callback?.(text); } catch (_) {}
            app.graph?.setDirtyCanvas?.(true, true);
        }
        syncOutputs();
    };
    const longEdge = () => node.widgets?.find((w) => w.name === "最长边")?.value ?? 1280;
    const fp = (i) => hashText(JSON.stringify([st.segments[i]?.images, st.segments[i]?.prompt, veJoins(st)[i], st.video.file, st.cuts,
        st.ve.head, Number(longEdge()), i === st.segments.length - 1 ? "last" : ""]));
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
        else if (done > 0 && done < st.segments.length) statusBox.appendChild(el("span", { text: `进度：已完成 ${done}/${st.segments.length} 段（下次「输出第几段=0」可继续）` }));
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
        save();
        renderTimeline();
        renderPanel();
    };

    /* ---------------------------------------------------------------- 编辑动作 */
    const fmtT = (s) => {
        const m = Math.floor(s / 60);
        return `${m}:${(s - m * 60).toFixed(1).padStart(4, "0")}`;
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
            g.join = st.segments[i]?.join ?? null;
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
                }
            });
            videoEl.addEventListener("play", onPlay);
            videoEl.addEventListener("pause", () => { cancelAnimationFrame(raf); movePlayhead(); });
            videoEl.addEventListener("seeked", movePlayhead);
            videoEl.addEventListener("timeupdate", movePlayhead);
        }
        topBox.appendChild(videoEl);
    };

    /** 播放只到当前这一段的末尾就停 */
    const onPlay = () => {
        const bd = bounds();
        const t = videoEl.currentTime;
        const k = segAt(t + 0.03, bd);
        stopAt = bd[k + 1];
        if (k !== active) { active = k; renderTimeline(); renderPanel(); }
        const loop = () => {
            if (!videoEl || videoEl.paused) return;
            if (stopAt != null && videoEl.currentTime >= stopAt - 0.02 && stopAt < totalT() - 0.02) {
                videoEl.pause();
                videoEl.currentTime = stopAt;
                return;
            }
            movePlayhead();
            raf = requestAnimationFrame(loop);
        };
        cancelAnimationFrame(raf);
        raf = requestAnimationFrame(loop);
    };
    const movePlayhead = () => {
        const T = totalT();
        if (headEl && headEl.isConnected && videoEl && T) headEl.style.left = `${Math.min(100, Math.max(0, (videoEl.currentTime / T) * 100))}%`;
    };
    const seekTo = (t) => {
        if (videoEl) videoEl.currentTime = Math.max(0, t);
        movePlayhead();
    };

    /* ---------------------------------------------------------------- 时间轴 */
    function renderTimeline() {
        tlBox.textContent = "";
        const T = totalT();
        if (!st.video.file) return;
        const pl = curPlan();
        const bd = bounds(pl);
        const n = st.segments.length;
        const pct = (t) => `${(t / T) * 100}%`;

        // ---- 工具栏
        const splitN = el("input", { type: "number", min: "1", max: "40", value: String(n), title: "平均分成几段" });
        const audioSel = el("select", { title: "成片的声音" }, VE_AUDIO.map((o) => el("option", { value: o, text: o })));
        audioSel.value = st.ve.audio;
        audioSel.addEventListener("change", () => { st.ve.audio = audioSel.value; save(); });
        tlBox.appendChild(el("div", { class: "tool" }, [
            el("button", { text: "✂ 在播放位置分割", title: "先在上面的视频里拖到想分割的地方，再点这个", onclick: () => addCutAt(videoEl?.currentTime || 0) }),
            el("span", { text: "均分" }), splitN, el("span", { text: "段" }),
            el("button", { text: "均分", onclick: () => equalSplit(splitN.value) }),
            el("span", { class: "sep" }),
            el("button", { class: "danger", text: "删除本段分割", title: "把选中的这一段和相邻的一段合并（第 1 段并入下一段，其余并入上一段）", onclick: removeSplit }),
            el("button", { class: "danger", text: "清除全部分割", onclick: clearSplits }),
            el("span", { class: "sep" }),
            el("span", { text: "音频" }), audioSel,
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
        // 刻度
        const ruler = mkRow("", "ruler");
        const steps = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600];
        const step = steps.find((s) => T / s <= 12) || 600;
        for (let t = 0; t <= T + 1e-6; t += step) {
            ruler.appendChild(Object.assign(el("div", { class: "tk" }), { style: `left:${pct(t)}` }));
            ruler.appendChild(el("div", { class: "tt", style: `left:${pct(t)}`, text: `${Math.floor(t / 60)}:${String(Math.round(t % 60)).padStart(2, "0")}` }));
        }
        ruler.addEventListener("click", (ev) => {
            const r = ruler.getBoundingClientRect();
            const t = ((ev.clientX - r.left) / r.width) * T;
            const k = segAt(t, bd);
            seekTo(t);
            if (k !== active) { active = k; renderTimeline(); renderPanel(); }
        });
        const xToT = (clientX, rect) => {
            const r = rect || lanes[0].getBoundingClientRect();
            return Math.min(T, Math.max(0, ((clientX - r.left) / r.width) * T));
        };

        // 视频轨
        const vlane = mkRow("视频", "vlane");
        const GAP = 1.5; // 分割处断开的缝（px，每侧）
        for (let i = 0; i < n; i++) {
            const a = bd[i], b = bd[i + 1];
            const blk = el("div", { class: "blk" + (i === active ? " on" : ""), title: `第 ${i + 1} 段：点击选中并定位到这一段开头` }, [
                el("span", { text: `第${i + 1}段` }), el("small", { text: `${(b - a).toFixed(1)}s` }),
            ]);
            blk.style.left = `calc(${pct(a)} + ${i === 0 ? 0 : GAP}px)`;
            blk.style.width = `calc(${Math.max(0, ((b - a) / T) * 100)}% - ${(i === 0 ? 0 : GAP) + (i === n - 1 ? 0 : GAP)}px)`;
            blk.style.background = COLORS[i % COLORS.length];
            blk.addEventListener("click", () => {
                seekTo(a + 0.001);
                if (active !== i) { active = i; renderTimeline(); renderPanel(); }
            });
            vlane.appendChild(blk);
        }
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
                const clip = el("div", { class: "clip", title: `${splitPath(c.file).filename}\n拖动中间移动、拖左右边缘改变覆盖的段数（自动吸附到分割线）` }, [
                    el("img", { src: viewUrl(c.file), alt: "" }),
                    el("span", { class: "cn", text: splitPath(c.file).filename }),
                    el("span", { class: "x", text: "×", title: "移除这张图", onmousedown: (e) => e.stopPropagation(), onclick: () => { track.splice(ci, 1); commit(); } }),
                    el("div", { class: "rh l" }), el("div", { class: "rh r" }),
                ]);
                clip.style.left = `calc(${pct(bd[c.a])} + 1px)`;
                clip.style.width = `calc(${((bd[c.b + 1] - bd[c.a]) / T) * 100}% - 2px)`;
                clip.addEventListener("mousedown", (ev) => {
                    if (ev.target.classList.contains("x")) return;
                    ev.preventDefault();
                    const mode = ev.target.classList.contains("l") ? "l" : ev.target.classList.contains("r") ? "r" : "m";
                    const r0 = lanes[0].getBoundingClientRect();
                    const idxAt = (clientX) => segAt(xToT(clientX, r0), bd);
                    const grab = idxAt(ev.clientX);
                    const prev = track[ci - 1], next = track[ci + 1];
                    const loLim = prev ? prev.b + 1 : 0;
                    const hiLim = next ? next.a - 1 : n - 1;
                    const a0 = c.a, b0 = c.b;
                    const move = (e) => {
                        const idx = idxAt(e.clientX);
                        if (mode === "l") c.a = Math.min(b0, Math.max(loLim, idx));
                        else if (mode === "r") c.b = Math.max(a0, Math.min(hiLim, idx));
                        else {
                            const len = b0 - a0;
                            const na = Math.min(hiLim - len, Math.max(loLim, a0 + (idx - grab)));
                            c.a = na;
                            c.b = na + len;
                        }
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
                lane.appendChild(clip);
            });
        });
        // 播放头：盖在所有轨道上
        const overlay = el("div", { class: "row", style: "position:absolute;left:0;right:0;top:4px;bottom:4px;pointer-events:none" }, [
            el("div", { style: "flex:0 0 74px" }),
            el("div", { class: "lane", style: "pointer-events:none;margin-top:0" }, [(headEl = el("div", { class: "head" }))]),
        ]);
        tl.appendChild(overlay);
        tl.appendChild(el("div", { class: "row", style: "margin-top:4px" }, [
            el("div", { class: "lab" }),
            el("div", { class: "lane", style: "margin-top:0" }, [
                el("button", { text: "＋ 图片轨", disabled: st.vtracks.length >= MAX_TRACKS ? "disabled" : undefined, onclick: () => { st.vtracks.push([]); commit(); } }),
            ]),
        ]));
        tlBox.appendChild(tl);
        movePlayhead();

        // ---- 规划摘要
        const info = el("div", { class: "info" });
        const lines = [];
        if (pl?.error) info.appendChild(el("div", { class: "e", text: `⚠ ${pl.error}` }));
        else if (pl?.plan.length) {
            const frames = pl.plan.reduce((s, e) => s + e.delivered, 0) - pl.plan[pl.plan.length - 1].tail_cut;
            lines.push(`共 ${n} 段，成片 ${frames} 帧（${(frames / VE_FPS).toFixed(1)} 秒，${VE_FPS} fps）。分割位置会自动吸附到模型能接受的帧数上，所以实际分割线可能和你点的位置差几帧。`);
        }
        const bare = [];
        for (let i = 0; i < n; i++) if (!st.segments[i].images.length) bare.push(i + 1);
        if (bare.length) info.appendChild(el("div", { class: "w", text: `第 ${bare.join("、")} 段还没有参考图：把图片轨上的图片拉到覆盖这些段。` }));
        for (const w of pl?.warns || []) info.appendChild(el("div", { class: "w", text: w }));
        if (lines.length) info.prepend(el("div", { text: lines[0] }));
        statusBox = el("div", { class: "status" });
        tlBox.append(info, statusBox);
        refreshStatus();
    }

    /* ---------------------------------------------------------------- 选中段信息 + 提示词 */
    function renderPanel() {
        panelBox.textContent = "";
        if (!st.video.file) return;
        const n = st.segments.length;
        if (active >= n) active = n - 1;
        const pl = curPlan();
        const refs = (i) => st.segments[i].images;
        const refRow = (i) => {
            const row = el("div", { class: "refs" }, [el("span", { text: `第 ${i + 1} 段的参考图：` })]);
            if (!refs(i).length) row.appendChild(el("span", { text: "（无）" }));
            refs(i).forEach((it, j) => row.appendChild(el("div", { class: "rc", title: `${splitPath(it.file).filename}（提示词里写 <Picture ${j + 1}> 就是这张）`, onclick: () => openViewer("img", viewUrl(it.file), splitPath(it.file).filename) }, [
                el("img", { src: viewUrl(it.file), alt: "" }), el("b", { text: `图${j + 1}` }),
            ])));
            return row;
        };

        // 选中段信息
        const e = pl?.plan?.[active];
        const joins = veJoins(st);
        const joinSel = el("select", { title: "和上一段怎么接：自动 = 参考图相同就「复制」（同一个人接着生成），换了人就「硬切」" },
            ["自动", ...JOINS].map((j) => el("option", { value: j, text: j === "自动" ? `自动（${joins[active]}）` : j })));
        joinSel.value = st.segments[active].join || "自动";
        joinSel.disabled = active === 0;
        joinSel.addEventListener("change", () => { st.segments[active].join = joinSel.value === "自动" ? null : joinSel.value; commit(); });
        const head = el("div", { class: "bar" }, [
            el("b", { text: `第 ${active + 1} 段` }),
            el("span", { class: "hint", text: e ? `${fmtT(e.story_start / VE_FPS)} – ${fmtT(e.story_end / VE_FPS)}　成片 ${e.delivered} 帧　采样 ${e.sampled} 帧` : "" }),
            el("span", { class: "sp" }),
            el("span", { text: "和上一段衔接：" }), joinSel,
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
            ta.placeholder = "写下要对所有片段生效的提示词，例如：把视频里的人换成参考图里的人（<Picture 1> 就是第 1 张参考图）";
            ta.value = st.gprompt;
            ta.addEventListener("input", () => { st.gprompt = ta.value; save(); });
            box.appendChild(ta);
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
            ta.addEventListener("input", () => { st.lprompts[active] = ta.value; save(); });
            box.appendChild(ta);
            box.appendChild(refRow(active));
            if (!String(st.lprompts[active] || "").trim()) box.appendChild(el("div", { class: "hint", text: `这一段留空，实际使用全局提示词：${st.gprompt || "（全局提示词也是空的）"}` }));
        }
        panelBox.append(head, box);
    }

    /* ---------------------------------------------------------------- 对外接口（和 H3 多段输入的编辑器一致） */
    const load = () => {
        let data = null;
        try { data = dataWidget.value ? JSON.parse(dataWidget.value) : null; } catch (_) { data = null; }
        st = parse(data);
        derive();
        active = 0;
        videoFile = "";
        renderTop();
        renderTimeline();
        renderPanel();
        syncOutputs();
    };
    return {
        root, load, getState: () => st, validDone, setProgress,
        getTask: () => st.progress?.task || "",
        rerender: () => { renderTop(); renderTimeline(); renderPanel(); },
        refreshLive: () => { syncOutputs(); refreshStatus(); },
        setRunText: (t) => { runText = t || ""; refreshStatus(); },
    };
}
