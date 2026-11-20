// 自有翻译前端：不依赖任何其他翻译插件。
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import {
  createState, applyNodeDef, applyVueNodeDef, applyGraph, applyNode, wrapDynamic, handleAddedNode, applyRegisteredTypes,
} from "./autotranslate_logic.js";

const ID = "AutoTranslate";
const LOG = "[AutoTranslate]";
let state = createState({}, {});
let ready = false;
let translationTarget = null;
const targetButtons = new WeakSet();
const sections = {title: "标题", description: "说明", inputs: "输入口", widgets: "参数名", outputs: "输出口", tooltips: "悬停提示", options: "下拉选项"};

function element(tag, text) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  return el;
}

function tell(message, severity = "info") {
  const toast = app.extensionManager?.toast;
  if (toast?.add) toast.add({severity, summary: "节点汉化", detail: message, life: 4500});
  else window.alert(message);
}

async function targetRequest(id, body) {
  const res = body
    ? await api.fetchApi("/autotranslate/target", {
        method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({...body, id}),
      })
    : await api.fetchApi(`/autotranslate/target?id=${encodeURIComponent(id)}`);
  const data = await res.json();
  if (!res.ok || data.error) throw new Error(data.error || `请求失败（${res.status}）`);
  return data;
}

function localizerValue(node, name) {
  return node.widgets?.find((w) => w.name === name)?.value;
}

async function openTargetEditor(localizer, target) {
  if (!target) { tell("先右键点击有漏译的节点，选择「设为汉化目标」。"); return; }
  let data;
  try { data = await targetRequest(target.id); }
  catch (error) { tell(error.message, "error"); return; }
  const dialog = element("dialog");
  Object.assign(dialog.style, {width: "min(720px, 90vw)", maxHeight: "85vh", color: "var(--input-text, #222)", background: "var(--comfy-menu-bg, #fff)", border: "1px solid #888", borderRadius: "10px", padding: "16px"});
  const title = element("h3", `汉化目标：${target.title}（${target.id}）`);
  const hint = element("p", "在每条原文下方输入中文，再点「保存手动译文」。空白项不修改；下拉只改显示文字，不改实际值。同类型节点复用译文，刷新后生效。");
  const result = element("pre");
  Object.assign(result.style, {whiteSpace: "pre-wrap", overflow: "auto", maxHeight: "160px"});
  result.setAttribute("role", "status");
  const form = element("div");
  Object.assign(form.style, {maxHeight: "42vh", overflowY: "auto"});
  const showExisting = element("input");
  showExisting.type = "checkbox";
  const filter = element("label");
  filter.append(showExisting, " 显示已有译文和原本是中文的项（可纠正）");
  const fieldType = element("select");
  for (const [key, text] of [["", "全部字段"], ...Object.entries(sections)]) {
    const option = element("option", text);
    option.value = key;
    fieldType.append(option);
  }
  const visibleCount = element("p");
  const rows = data.fields.map((field) => {
    const context = field.widget || (field.key && field.key !== field.source ? field.key : "");
    const row = element("label");
    const label = element("span", `${sections[field.section]}${context ? ` · ${context}` : ""} — 原文：${field.source} `);
    const status = element("small");
    row.append(label, status);
    row.style.display = "block";
    row.style.margin = "8px 0";
    const input = element(["description", "tooltips"].includes(field.section) ? "textarea" : "input");
    input.value = field.value || "";
    input.placeholder = "在这里填写译文（留空不修改）";
    input.setAttribute("aria-label", `译文：${field.widget || field.key || sections[field.section]} ${field.source}`);
    input.style.display = "block";
    input.style.width = "98%";
    row.append(input);
    form.append(row);
    return {field, row, input, status};
  });
  const updateFilter = () => {
    let shown = 0;
    for (const {field, row, status} of rows) {
      const visible = (!fieldType.value || fieldType.value === field.section) && (showExisting.checked || (!field.value && field.needs_translation));
      row.style.display = visible ? "block" : "none";
      status.textContent = field.manual ? "[手动译文]" : field.value ? "[已有译文]" : field.needs_translation ? "[待译]" : "[原文可保留]";
      shown += Number(visible);
    }
    visibleCount.textContent = shown ? `显示 ${shown} 项` : "没有符合条件的项目。勾选“显示已有译文和原本是中文的项”可查看并修改全部。";
  };
  showExisting.onchange = fieldType.onchange = updateFilter;
  updateFilter();
  const manual = element("button", "保存手动译文");
  const ai = element("button", "用 AI 补译这个节点");
  const retranslate = element("input");
  retranslate.type = "checkbox";
  const aiMode = element("label");
  aiMode.append(retranslate, " AI 也重翻已有的自动译文（不覆盖手动译文）");
  retranslate.onchange = () => { ai.textContent = retranslate.checked ? "用 AI 重翻这个节点" : "用 AI 补译这个节点"; };
  const close = element("button", "关闭");
  let busy = false;
  const editList = () => rows.filter(({field, input}) => input.value.trim() && input.value.trim() !== (field.value || "").trim())
    .map(({field, input}) => ({section: field.section, key: field.key, value: input.value.trim()}));
  const submit = async (body) => {
    if (busy) return;
    busy = manual.disabled = ai.disabled = close.disabled = true;
    for (const {input} of rows) input.disabled = true;
    result.textContent = body.action === "ai" ? "正在翻译，请稍候…" : "正在保存…";
    try {
      const reply = await targetRequest(target.id, body);
      result.textContent = reply.message + "\n刷新页面后应用译文。";
      const fresh = await targetRequest(target.id);
      const byField = new Map(fresh.fields.map((f) => [JSON.stringify([f.section, f.key]), f]));
      for (const {field, input} of rows) {
        const f = byField.get(JSON.stringify([field.section, field.key]));
        if (f) {
          field.value = input.value = f.value || "";
          field.manual = f.manual;
        }
      }
      updateFilter();
    } catch (error) { result.textContent = error.message; }
    finally {
      busy = manual.disabled = ai.disabled = close.disabled = false;
      for (const {input} of rows) input.disabled = false;
    }
  };
  manual.onclick = () => {
    const edits = editList();
    if (edits.length) void submit({action: "manual", edits});
    else result.textContent = "没有需要保存的修改。";
  };
  ai.onclick = () => {
    if (editList().length) { result.textContent = "请先保存手动改动，再使用 AI 翻译。"; return; }
    if (localizerValue(localizer, "只预览不翻译")) { result.textContent = "当前开启了「只预览不翻译」，关闭后才能调用 AI。"; return; }
    const model = localizerValue(localizer, "模型");
    if (!model) { result.textContent = "请在节点上获取模型列表并选择一个模型。"; return; }
    void submit({action: "ai", provider: localizerValue(localizer, "服务商"), model,
      proxy: localizerValue(localizer, "代理地址") || "",
      title: localizerValue(localizer, "翻译节点名称和说明") === true,
      names: localizerValue(localizer, "翻译端口和参数名") === true,
      tips: localizerValue(localizer, "翻译悬停提示") === true,
      options: localizerValue(localizer, "翻译下拉选项") === true,
      overwrite: retranslate.checked});
  };
  close.onclick = () => dialog.close();
  dialog.addEventListener("close", () => dialog.remove(), {once: true});
  const actions = element("div");
  Object.assign(actions.style, {display: "flex", gap: "8px", marginTop: "10px"});
  actions.append(manual, ai, close);
  const filters = element("div");
  Object.assign(filters.style, {display: "flex", flexWrap: "wrap", gap: "12px", alignItems: "center"});
  filters.append(fieldType, filter);
  aiMode.style.display = "block";
  aiMode.style.marginTop = "10px";
  dialog.append(title, hint, filters, visibleCount, form, aiMode, actions, result);
  document.body.append(dialog);
  dialog.showModal();
}

function addTargetButton(node) {
  if ((node?.constructor?.comfyClass || node?.comfyClass || node?.type) !== "NodeLocalizerAI" || targetButtons.has(node)) return;
  if (!node.addWidget) return;
  const button = node.addWidget("button", "翻译指定节点", null, () => void openTargetEditor(node, translationTarget), {serialize: false});
  button.serialize = false;
  targetButtons.add(node);
}

function setting(id, def) {
  try {
    const v = app.ui?.settings?.getSettingValue?.(id, def);
    return v === undefined ? def : v;
  } catch (e) {
    return def;
  }
}

function addBool(id, name, tooltip) {
  app.ui.settings.addSetting({
    id, name, type: "boolean", defaultValue: true, tooltip,
    onChange: (nv, ov) => {
      // 初始化阶段不刷新；用户在设置里切换后刷新页面使其生效
      if (ready && ov !== undefined && nv !== ov) setTimeout(() => location.reload(), 300);
    },
  });
}

let flushTimer = null;
function scheduleFlush() {
  if (flushTimer || !state.opts.menus) return;
  flushTimer = setTimeout(async () => {
    flushTimer = null;
    const items = [...state.misses].slice(0, 60);
    items.forEach((s) => state.misses.delete(s));
    if (!items.length) return;
    try {
      await api.fetchApi("/autotranslate/collect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ items }),
      });
    } catch (e) { /* 收集失败不影响使用 */ }
  }, 4000);
}

function installMenuObserver() {
  const obs = new MutationObserver((muts) => {
    for (const m of muts) {
      if (m.type === "characterData") handleAddedNode(state, m.target.parentElement);  // Vue 原地更新文字
      else m.addedNodes.forEach((n) => handleAddedNode(state, n));
    }
    if (state.misses.size) scheduleFlush();
  });
  obs.observe(document.body, { childList: true, subtree: true, characterData: true });
}

app.registerExtension({
  name: "AutoTranslate.Nodes",

  async init() {
    addBool(`${ID}.Enabled`, "🌐 自有翻译：启用", "切换后会自动刷新页面");
    addBool(`${ID}.Categories`, "🌐 自有翻译：翻译节点分类", "关闭后菜单分类保持英文");
    addBool(`${ID}.Menus`, "🌐 自有翻译：翻译右键菜单文字", "同时会自动收集还没有译文的菜单英文，供一键翻译节点使用");

    if (!setting(`${ID}.Enabled`, true)) {
      console.info(LOG, "已在设置中关闭");
      return;
    }
    try {
      const resp = await api.fetchApi("/autotranslate/data");
      const data = await resp.json();
      state = createState(data, {
        categories: setting(`${ID}.Categories`, true),
        menus: setting(`${ID}.Menus`, true),
      });
      console.info(LOG, `译文已加载：节点 ${Object.keys(state.nodes).length} / 分类 ${Object.keys(state.cats).length} / 菜单 ${Object.keys(state.menus).length}`);
    } catch (e) {
      console.warn(LOG, "译文加载失败", e);
    }
  },

  async setup() {
    ready = true;
    if (!setting(`${ID}.Enabled`, true)) return;
    try {
      const n = applyRegisteredTypes(state, globalThis.LiteGraph?.registered_node_types);
      if (n) console.info(LOG, `已翻译前端自带节点标题 ${n} 个`);
    } catch (e) { console.warn(LOG, "扫描已注册节点类型失败", e); }
    if (state.opts.menus) installMenuObserver();
    window.AutoTranslate = { get state() { return state; } };
  },

  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (!setting(`${ID}.Enabled`, true)) return;
    try { applyNodeDef(state, nodeData, nodeType); } catch (e) { console.warn(LOG, nodeData?.name, e); }
  },

  beforeRegisterVueAppNodeDefs(nodeDefs) {
    if (!setting(`${ID}.Enabled`, true)) return;
    try { (nodeDefs || []).forEach((d) => applyVueNodeDef(state, d)); } catch (e) { console.warn(LOG, e); }
  },

  getNodeMenuItems(node) {
    const id = node?.constructor?.comfyClass || node?.comfyClass || node?.type;
    if (!id || id === "NodeLocalizerAI" || node.isSubgraphNode?.()) return [];
    return [{content: "设为汉化目标", callback: () => {
      translationTarget = {id, title: node.title || id};
      tell(`已选：${translationTarget.title}。在「节点汉化（AI）」上点「翻译指定节点」。`);
    }}];
  },

  nodeCreated(node) {
    addTargetButton(node);
    if (setting(`${ID}.Enabled`, true)) {
      try { applyNode(state, node); wrapDynamic(state, node); } catch (e) { console.warn(LOG, e); }
    }
  },

  loadedGraphNode(node) {
    addTargetButton(node);
    if (setting(`${ID}.Enabled`, true)) {
      try { applyNode(state, node); wrapDynamic(state, node); } catch (e) { console.warn(LOG, e); }
    }
  },

  afterConfigureGraph() {
    if (setting(`${ID}.Enabled`, true)) applyGraph(state, app.rootGraph ?? app.graph);
  },
});
