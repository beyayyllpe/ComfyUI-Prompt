import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

// 节点类型：只处理这两个
// 需要「获取模型列表」按钮的节点
const TRANSLATOR_TYPES = ["AIPromptTranslator", "AIPromptTranslatorCLIP", "VideoPromptWriter"];
// 需要动态图像输入口的节点，以及各自的输入口前缀和上限
//   翻译节点：图像1..图像10
//   视频节点：参考图1..参考图9（全参考模式，官方上限 9 张；首帧/尾帧是固定口，不参与）
const DYNAMIC_IMG_TYPES = {
  AIPromptTranslator:     { prefix: "图像",   max: 10 },
  AIPromptTranslatorCLIP: { prefix: "图像",   max: 10 },
  VideoPromptWriter:      { prefix: "参考图", max: 9  },
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
    models.filter((m) => m.toLowerCase().includes(q)).forEach((m, idx) => {
      const item = document.createElement("div");
      item.textContent = (idx === 0 && !q ? "⭐ " : "") + m + (m === current ? " ✓" : "");
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
const imgRe = (prefix) => new RegExp("^" + prefix + "(\\d+)$");
const DEFAULT_WIDTH = 460;

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
  // 高度收紧到刚好容纳内容，宽度保持
  const min = node.computeSize?.();
  if (min) node.setSize?.([Math.max(node.size[0], min[0]), min[1]]);
  node.setDirtyCanvas?.(true, true);
  app.graph?.setDirtyCanvas?.(true, true);
}

// ─── 注册扩展 ────────────────────────────────────────────────────────────────────
app.registerExtension({
  name: "CustomNodes.TranslatorModelPicker",

  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (!TRANSLATOR_TYPES.includes(nodeData.name)) return;

    const onCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const result   = onCreated?.apply(this, arguments);
      const node     = this;
      const providerW= findWidget(node, "服务商");
      const modelW   = findWidget(node, "模型");
      if (!providerW || !modelW) return result;

      // 切换服务商时清空模型名
      const origCb = providerW.callback;
      providerW.callback = function () {
        const r = origCb?.apply(this, arguments);
        setWidget(node, modelW, "");
        return r;
      };

      // 获取模型列表按钮
      let loading = false;
      const btn = node.addWidget("button", "🔄 获取模型列表", null, async () => {
        if (loading) return;
        loading = true;
        const provider = providerW.value;
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
          if (data.error) {
            notify("获取模型列表失败", data.error, "error");
          } else {
            showModelPicker(data.models, modelW.value || data.default, (m) => {
              setWidget(node, modelW, m);
              notify("自定义节点", `已选择：${m}`, "success");
            });
          }
        } catch (err) {
          notify("获取模型列表失败", String(err), "error");
        } finally {
          loading = false;
        }
      }, { serialize: false });
      btn.serialize = false;

      // 把按钮移到"服务商"和"模型"之间（视觉位置），
      // 但不改动序列化顺序，通过 onSerialize/onConfigure 保证重载不错位
      const bIdx = node.widgets.indexOf(btn);
      const pIdx = node.widgets.indexOf(providerW);
      if (bIdx > -1 && pIdx > -1) {
        node.widgets.splice(bIdx, 1);
        node.widgets.splice(pIdx + 1, 0, btn);
      }
      // 默认更宽、更矮：先收起多余的图像口，再按内容定高度
      if (DYNAMIC_IMG_TYPES[nodeData.name]) syncImageInputs(node, DYNAMIC_IMG_TYPES[nodeData.name]);
      const min = node.computeSize?.() ?? node.size;
      node.setSize?.([Math.max(DEFAULT_WIDTH, min[0]), min[1]]);
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
      return r;
    };
  },
});
