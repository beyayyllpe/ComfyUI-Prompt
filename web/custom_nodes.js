import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

// 共用「获取模型列表」按钮及搜索选择弹窗的节点。
const TRANSLATOR_TYPES = ["AIPromptTranslator", "AIPromptTranslatorCLIP", "VideoPromptWriter", "NodeLocalizerAI"];
const LEGACY_AUTO_MODEL = "自动（推荐模型）";
const isExplicitModel = (v) => typeof v === "string" && v.trim() && v.trim() !== LEGACY_AUTO_MODEL;

// 空值只是未选择状态，拿到列表后下拉只提供真实模型。
function setLocalizerOptions(widget, models = [], includeCurrent = true) {
  const saved = includeCurrent && isExplicitModel(widget.value) ? widget.value : "";
  const choices = [...new Set([...models, saved].filter(isExplicitModel))];
  widget.options ??= {};
  widget.options.values = choices.length ? choices : [""];
  if (!saved) widget.value = "";
}
// 需要动态图像输入口的节点，以及各自的输入口前缀和上限
//   翻译节点：图像1..图像10
//   视频节点：参考图1..参考图9（全参考模式，官方上限 9 张；首帧/尾帧是固定口，不参与）
// ⚠ 视频节点【不要】放进来。它的参考图走 ComfyUI 官方的 io.Autogrow（V3 schema），
//    动态增减由前端原生处理，和官方 MiniMaxH3ReferenceToVideo 同一套机制。
//    自己用 addInput() 模仿会触发按内容重排，把节点撑高——试过三版都治不好。
const DYNAMIC_IMG_TYPES = {
  AIPromptTranslator:     { prefix: "图像", max: 10 },
  AIPromptTranslatorCLIP: { prefix: "图像", max: 10 },
};

// 记录鼠标位置，用于弹出模型列表的定位
let lastPointer = { x: window.innerWidth / 2, y: window.innerHeight / 3 };
document.addEventListener("pointerdown", (e) => {
  lastPointer = { x: e.clientX, y: e.clientY };
}, true);

const findWidget = (node, name) => node.widgets?.find((w) => w.name === name);

function notify(summary, detail, severity = "info") {
  const toast = app.extensionManager?.toast;
  if (toast?.add) {
    toast.add({ severity, summary, detail, life: severity === "error" ? 6000 : 2500 });
  } else if (severity === "error") {
    alert(`${summary}\n${detail}`);
  }
}

function setWidget(node, widget, value) {
  widget.value = value;
  widget.callback?.(value);
  node.setDirtyCanvas?.(true, true);
  app.graph?.setDirtyCanvas?.(true, true);
}

// ─── 模型选择弹窗 ────────────────────────────────────────────────────────────────
function showModelPicker(models, current, onPick) {
  document.getElementById("cn-model-picker")?.remove();

  const box = document.createElement("div");
  box.id = "cn-model-picker";
  Object.assign(box.style, {
    position: "fixed", zIndex: 10000, width: "340px", maxHeight: "440px",
    display: "flex", flexDirection: "column",
    background: "#1f2023", border: "1px solid #4a4d55", borderRadius: "8px",
    boxShadow: "0 8px 24px rgba(0,0,0,.6)", fontSize: "13px", color: "#ddd", overflow: "hidden",
  });
  box.style.left = `${Math.max(10, Math.min(lastPointer.x, window.innerWidth - 360))}px`;
  box.style.top  = `${Math.max(10, Math.min(lastPointer.y, window.innerHeight - 460))}px`;

  const input = document.createElement("input");
  input.placeholder = `搜索模型（共 ${models.length} 个）`;
  Object.assign(input.style, {
    margin: "8px", padding: "6px 8px", background: "#2b2d31", color: "#eee",
    border: "1px solid #4a4d55", borderRadius: "6px", outline: "none",
  });

  const list = document.createElement("div");
  Object.assign(list.style, { overflowY: "auto", padding: "0 4px 6px" });

  const close = () => {
    box.remove();
    document.removeEventListener("pointerdown", outside, true);
  };
  const outside = (e) => { if (!box.contains(e.target)) close(); };

  const render = () => {
    const q = input.value.trim().toLowerCase();
    list.innerHTML = "";
    models.filter((m) => m.toLowerCase().includes(q)).forEach((m) => {
      const item = document.createElement("div");
      item.textContent = m + (m === current ? " ✓" : "");
      Object.assign(item.style, {
        padding: "6px 10px", borderRadius: "4px", cursor: "pointer",
        background: m === current ? "#34373d" : "transparent",
      });
      item.onmouseenter = () => (item.style.background = "#3b3f46");
      item.onmouseleave = () => (item.style.background = m === current ? "#34373d" : "transparent");
      item.onclick = () => { onPick(m); close(); };
      list.appendChild(item);
    });
  };

  input.oninput = render;
  input.onkeydown = (e) => { if (e.key === "Escape") close(); };
  box.append(input, list);
  document.body.appendChild(box);
  render();
  input.focus();
  setTimeout(() => document.addEventListener("pointerdown", outside, true), 0);
}

// ─── 动态图像输入口：接一个出现下一个，最多 10 个 ─────────────────────────────────
// 新拖出来的节点给个像样的初始尺寸。
// ⚠ 只能在 onNodeCreated 里【同步】设一次。时序是：
//     节点创建 → onNodeCreated（设这里的值）→ ComfyUI 恢复工作流里存的尺寸
//   同步设的会被后面的恢复覆盖掉，这正是我们要的；
//   但凡用 setTimeout / requestAnimationFrame 延迟设，就排到恢复之后了，
//   结果是用户拖好的尺寸每次切换工作流都被打回去。别再加延迟。
const NODE_SIZE = {
  AIPromptTranslator:     [460, 520],
  AIPromptTranslatorCLIP: [460, 540],
  VideoPromptWriter:      [560, 640],
};

const imgRe = (prefix) => new RegExp("^" + prefix + "(\\d+)$");


function imgNum(inp, re) {
  if (!inp || inp.widget || typeof inp.name !== "string") return 0;
  const m = inp.name.match(re);
  return m ? parseInt(m[1], 10) : 0;
}

function syncImageInputs(node, cfg) {
  if (!node?.inputs || node.__cnSyncing || !cfg) return;
  const re = imgRe(cfg.prefix);
  node.__cnSyncing = true;
  try {
    // 已接线的最大编号 → 需要显示到 最大编号+1（留一个空位）
    let maxConnected = 0;
    for (const inp of node.inputs) {
      const n = imgNum(inp, re);
      if (n && inp.link != null) maxConnected = Math.max(maxConnected, n);
    }
    const want = Math.min(maxConnected + 1, cfg.max);

    // 从后往前删：编号超出且没接线的
    for (let k = node.inputs.length - 1; k >= 0; k--) {
      const inp = node.inputs[k];
      const n = imgNum(inp, re);
      if (n > want && inp.link == null) node.removeInput(k);
    }
    // 补齐 1..want 中缺少的
    const have = new Set(node.inputs.map((i) => imgNum(i, re)).filter(Boolean));
    for (let n = 1; n <= want; n++) {
      if (!have.has(n)) node.addInput(cfg.prefix + n, "IMAGE");
    }
  } finally {
    node.__cnSyncing = false;
  }
  // ⚠ 这个函数里【不要】动节点尺寸。刷新页面、切换工作流、增减输入口都会走到这里，
  //    任何 setSize / computeSize 重算都会把用户拖好的大小打回去。只重绘。
  node.setDirtyCanvas?.(true, true);
  app.graph?.setDirtyCanvas?.(true, true);
}



// ─── 注册扩展 ────────────────────────────────────────────────────────────────────
app.registerExtension({
  name: "CustomNodes.TranslatorModelPicker",

  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (!TRANSLATOR_TYPES.includes(nodeData.name)) return;
    const isLocalizer = nodeData.name === "NodeLocalizerAI";

    const onCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const result   = onCreated?.apply(this, arguments);
      const node     = this;
      const providerW= findWidget(node, "服务商");
      const modelW   = findWidget(node, "模型");
      if (!providerW || !modelW) return result;

      if (isLocalizer) setLocalizerOptions(modelW);
      let requestId = 0;
      let loading = false;
      // 切换服务商时清空旧模型，忽略旧服务商正在返回的列表。
      const origCb = providerW.callback;
      providerW.callback = function () {
        const r = origCb?.apply(this, arguments);
        requestId++;
        loading = false;
        if (isLocalizer) setLocalizerOptions(modelW, [], false);
        setWidget(node, modelW, "");
        return r;
      };

      // 获取模型列表按钮
      const btn = node.addWidget("button", "🔄 获取模型列表", null, async () => {
        if (loading) return;
        loading = true;
        const currentRequest = ++requestId;
        const provider = providerW.value;
        const isCurrent = () => currentRequest === requestId && providerW.value === provider;
        notify("自定义节点", `正在获取 ${provider} 的模型列表…`);
        try {
          const resp = await api.fetchApi("/custom_nodes/models", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              provider,
              key:   findWidget(node, "API密钥")?.value   || "",
              proxy: findWidget(node, "代理地址")?.value  || "",
            }),
          });
          const data = await resp.json();
          if (!isCurrent()) return;
          const models = Array.isArray(data.models)
            ? [...new Set(data.models.filter((m) => typeof m === "string" && m.trim() && (!isLocalizer || isExplicitModel(m))))]
            : [];
          if (data.error || !resp.ok || !models.length) {
            notify("获取模型列表失败", data.error || "接口没有返回可用模型", "error");
          } else {
            if (isLocalizer) {
              setLocalizerOptions(modelW, models);
              node.setDirtyCanvas?.(true, true);
            }
            showModelPicker(models, modelW.value || (isLocalizer ? "" : data.default), (m) => {
              if (!isCurrent()) return;
              setWidget(node, modelW, m);
              notify("自定义节点", `已选择：${m}`, "success");
            });
          }
        } catch (err) {
          if (isCurrent()) notify("获取模型列表失败", String(err), "error");
        } finally {
          if (currentRequest === requestId) loading = false;
        }
      }, { serialize: false });
      btn.serialize = false;

      // 把按钮移到"服务商"和"模型"之间（视觉位置），
      // 但不改动序列化顺序，通过 onSerialize/onConfigure 保证重载不错位
      const bIdx = node.widgets.indexOf(btn);
      const pIdx = node.widgets.indexOf(providerW);
      // ComfyUI 恢复位置数组时会跳过 serialize=false 的按钮。
      if (bIdx > -1 && pIdx > -1) {
        node.widgets.splice(bIdx, 1);
        node.widgets.splice(pIdx + 1, 0, btn);
      }
      if (DYNAMIC_IMG_TYPES[nodeData.name]) syncImageInputs(node, DYNAMIC_IMG_TYPES[nodeData.name]);
      // 新节点的初始尺寸：同步设一次，不要延迟（理由见 NODE_SIZE 上方注释）
      const want = NODE_SIZE[nodeData.name];
      if (want) node.setSize?.([want[0], want[1]]);
      return result;
    };

    // 连线变化时同步图像输入口
    const onConnChange = nodeType.prototype.onConnectionsChange;
    nodeType.prototype.onConnectionsChange = function (type, index, connected, linkInfo) {
      const r = onConnChange?.apply(this, arguments);
      if (type === 1 && DYNAMIC_IMG_TYPES[nodeData.name]) {  // 1 = 输入端
        const node = this, cfg = DYNAMIC_IMG_TYPES[nodeData.name];
        setTimeout(() => syncImageInputs(node, cfg), 0);
      }
      return r;
    };

    // ── 按参数名保存/恢复，解决按钮插在中间导致参数顺序错位的问题 ──────────────
    const onSerialize = nodeType.prototype.onSerialize;
    nodeType.prototype.onSerialize = function (o) {
      const r = onSerialize?.apply(this, arguments);
      const vals = {};
      for (const w of this.widgets || []) {
        if (w?.name && w.type !== "button") vals[w.name] = w.value;
      }
      o.properties = o.properties || {};
      o.properties.cn_values = vals;
      return r;
    };

    const onConfigure = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function (info) {
      const r = onConfigure?.apply(this, arguments);
      const self = this;
      if (DYNAMIC_IMG_TYPES[nodeData.name]) {
        const cfg = DYNAMIC_IMG_TYPES[nodeData.name];
        setTimeout(() => syncImageInputs(self, cfg), 0);
      }
      const vals = info?.properties?.cn_values;
      if (vals) {
        for (const w of this.widgets || []) {
          if (w?.name && w.type !== "button" && Object.prototype.hasOwnProperty.call(vals, w.name)) {
            w.value = vals[w.name];
          }
        }
      }
      if (isLocalizer) {
        const modelW = findWidget(this, "模型");
        if (modelW) setLocalizerOptions(modelW);
      }
      return r;
    };
  },
});
