// 纯逻辑，不依赖 ComfyUI，方便单独测试。

const CJK = /[一-鿿]/;
export const hasCJK = (s) => typeof s === "string" && CJK.test(s);
const hasWords = (s) => typeof s === "string" && /[A-Za-z]{3}/.test(s);

// 旧版 LiteGraph 菜单 + PrimeVue 菜单 + 新版节点右键菜单（reka-ui，带 role 属性，渲染在页面最外层）
export const MENU_SELECTOR =
  ".litecontextmenu, .litemenu, .p-contextmenu, .p-menu, .p-tieredmenu, " +
  "[role='menu'], [role='menuitem'], [role='menuitemcheckbox'], [role='menuitemradio'], " +
  // 下拉框（选项列表、已选项显示）：只替换已有译文的选项，不上报未翻译词
  "[role='combobox'], [role='listbox'], [role='option'], .p-select-label, .p-select-overlay, .p-select-option, .p-dropdown-label, .p-dropdown-panel";
const OPTION_SELECTOR = "[role='combobox'], [role='listbox'], [role='option'], .p-select-label, .p-select-overlay, .p-select-option, .p-dropdown-label, .p-dropdown-panel";

// 前端自己注册的节点（后端节点列表里没有，扫描不到），内置默认译文；用户 nodes.json 里的优先
export const DEFAULT_NODES = {
  PrimitiveNode: { title: "原始值" },
  Note: { title: "注释" },
  MarkdownNote: { title: "Markdown 注释" },
  Reroute: { title: "转接点" },
};
export const DEFAULT_CATS = { primitive: "原始值" };

// 官方中文包里的菜单词条（节点右键菜单等），用户 menus.json 里的优先
export const DEFAULT_MENUS = {
  "Add Group": "添加组",
  "Add Group For Selected Nodes": "为选定节点添加组",
  "Add Node": "添加节点",
  "Add Subgraph to Library": "添加子工作流到节点库",
  "Adjust Size": "调整尺寸",
  "Align Selected To": "对齐选中项到",
  "Bottom": "底部",
  "Bypass": "绕过",
  "Clone": "克隆",
  "Collapse": "折叠",
  "Color": "颜色",
  "Colors": "颜色",
  "Convert to Subgraph": "转换为子工作流",
  "Copy": "复制",
  "Copy (Clipspace)": "复制 (Clipspace)",
  "Copy Image": "复制图像",
  "Delete": "删除",
  "Distribute Nodes": "分布节点",
  "Download Images": "下载图像",
  "Duplicate": "复制",
  "Edit Subgraph Widgets": "编辑子工作流组件",
  "Expand": "展开",
  "Expand Node": "展开节点",
  "Extensions": "扩展",
  "FavoriteWidget": "收藏组件",
  "Hide Link": "隐藏连线",
  "Horizontal": "水平",
  "Inputs": "输入",
  "Left": "左侧",
  "Manage": "管理",
  "Minimize Node": "最小化节点",
  "Mode": "模式",
  "Node Info": "节点信息",
  "Node Templates": "节点模板",
  "Open Image": "打开图像",
  "Open in Mask Editor": "用遮罩编辑器打开",
  "Outputs": "输出",
  "Paste": "粘贴",
  "Paste Image": "粘贴图像",
  "Pin": "固定",
  "Properties": "属性",
  "Properties Panel": "属性面板",
  "Remove": "删除",
  "Remove Bypass": "删除所有忽略节点",
  "Rename": "重命名",
  "RenameWidget": "重命名组件",
  "Resize": "调整大小",
  "Right": "右侧",
  "Run Branch": "运行支流节点",
  "Save Image": "保存图像",
  "Save Selected as Template": "将选定节点另存为模板",
  "Search": "搜索",
  "Shape": "形状",
  "Shapes": "形状",
  "Show Link": "显示连线",
  "Show Links": "显示连线",
  "Title": "标题",
  "Top": "顶部",
  "UnfavoriteWidget": "取消收藏组件",
  "Unpack Subgraph": "解包子工作流",
  "Unpin": "取消固定",
  "Vertical": "竖直",
  "deprecated": "弃用",
  "new": "新",
  "About ComfyUI": "关于ComfyUI",
  "Assets": "资产",
  "Bottom Panel": "底部面板",
  "Browse Templates": "浏览模板",
  "Bypass/Unbypass Selected Nodes": "忽略/取消忽略选定节点",
  "Canvas Performance": "画布性能",
  "Canvas Toggle Lock": "切换视图锁定",
  "Check for Custom Node Updates": "检查自定义节点更新",
  "Check for Updates": "检查更新",
  "Clear Pending Tasks": "清除待处理任务",
  "Clear Workflow": "清除工作流",
  "Clipspace": "剪贴空间",
  "Close Current Workflow": "关闭当前工作流",
  "Collapse/Expand Selected Nodes": "折叠/展开选定节点",
  "ComfyUI Docs": "ComfyUI 文档",
  "ComfyUI Forum": "ComfyUI 论坛",
  "ComfyUI Issues": "ComfyUI 问题",
  "Contact Support": "联系支持",
  "Convert Selection to Subgraph": "将选中内容转换为子工作流",
  "Custom Nodes (Legacy)": "自定义节点（旧版）",
  "Custom Nodes Manager": "自定义节点管理器",
  "Decrease Brush Size in MaskEditor": "在 MaskEditor 中减小笔刷大小",
  "Delete Selected Items": "删除选定的项目",
  "Delete Selected Links": "删除选中的连线",
  "Desktop User Guide": "桌面端用户手册",
  "Duplicate Current Workflow": "复制当前工作流",
  "Edit": "编辑",
  "Exit Subgraph": "退出子工作流",
  "Experimental: Browse Model Assets": "实验性：浏览模型资产",
  "Experimental: Enable Nodes 2_0": "实验性：启用 Nodes 2.0",
  "Export": "导出",
  "Export (API)": "导出 (API)",
  "File": "文件",
  "Fit Group To Contents": "适应组内容",
  "Focus Mode": "专注模式",
  "Group Selected Nodes": "将选中节点转换为组节点",
  "Help": "帮助",
  "Help Center": "帮助中心",
  "Hide Selected Links": "隐藏选中的连线",
  "Increase Brush Size in MaskEditor": "在 MaskEditor 中增大笔刷大小",
  "Install Missing Custom Nodes": "安装缺失的自定义节点",
  "Interrupt": "中断",
  "Job History": "任务历史",
  "Load Default Workflow": "加载默认工作流",
  "Lock Canvas": "锁定画布",
  "Manager": "管理器",
  "Manager Menu (Legacy)": "管理菜单（旧版）",
  "Minimap": "小地图",
  "Mirror Horizontal in MaskEditor": "在蒙版编辑器中水平翻转",
  "Mirror Vertical in MaskEditor": "在蒙版编辑器中垂直翻转",
  "Model Library": "模型库",
  "Move Selected Nodes Down": "下移所选节点",
  "Move Selected Nodes Left": "左移所选节点",
  "Move Selected Nodes Right": "右移所选节点",
  "Move Selected Nodes Up": "上移所选节点",
  "Mute/Unmute Selected Nodes": "静音/取消静音选定节点",
  "New": "新建",
  "Next Opened Workflow": "下一个打开的工作流",
  "Node Library": "节点库",
  "Node Links": "节点连接",
  "Open": "打开",
  "Open 3D Viewer (Beta) for Selected Node": "为选中节点打开3D查看器（测试版）",
  "Open Color Picker in MaskEditor": "在 MaskEditor 中打开取色器",
  "Open Custom Nodes Folder": "打开自定义节点文件夹",
  "Open DevTools": "打开开发者工具",
  "Open Inputs Folder": "打开输入文件夹",
  "Open Layer Editor for Selected Node": "为所选节点打开图层编辑器",
  "Open Logs Folder": "打开日志文件夹",
  "Open Mask Editor for Selected Node": "为选中节点打开 Mask 编辑器",
  "Open Models Folder": "打开模型文件夹",
  "Open Outputs Folder": "打开输出文件夹",
  "Open Sign In Dialog": "打开登录对话框",
  "Open extra_model_paths_yaml": "打开 extra_model_paths.yaml",
  "Paste with Connect": "粘贴并连接",
  "Pin/Unpin Selected Items": "固定/取消固定选定项目",
  "Pin/Unpin Selected Nodes": "固定/取消固定选定节点",
  "Previous Opened Workflow": "上一个打开的工作流",
  "Publish": "发布",
  "Queue Prompt": "执行提示词",
  "Queue Prompt (Front)": "执行提示词 (优先执行)",
  "Queue Selected Output Nodes": "将所选输出节点加入队列",
  "Quit": "退出",
  "Redo": "重做",
  "Refresh Node Definitions": "刷新节点定义",
  "Reinstall": "重装",
  "Replay Onboarding": "重新播放引导",
  "Reset View": "重置视图",
  "Resize Selected Nodes": "调整选定节点的大小",
  "Restart": "重启",
  "Rotate Left in MaskEditor": "在蒙版编辑器中向左旋转",
  "Rotate Right in MaskEditor": "在蒙版编辑器中向右旋转",
  "Save": "保存",
  "Save As": "另存为",
  "Select All": "全选",
  "Set Subgraph Description": "设置子图描述",
  "Set Subgraph Search Aliases": "设置子图搜索别名",
  "Show Keybindings Dialog": "显示快捷键对话框",
  "Show Model Selector (Dev)": "显示模型选择器（开发用）",
  "Show Selected Links": "显示选中的连线",
  "Show Settings Dialog": "显示设置对话框",
  "Sign Out": "退出登录",
  "Toggle App Mode": "切换 App 模式",
  "Toggle Essential Bottom Panel": "切换基础底部面板",
  "Toggle Logs Bottom Panel": "切换日志底部面板",
  "Toggle Queue Panel V2": "切换队列面板 V2",
  "Toggle Search Box": "切换搜索框",
  "Toggle Terminal Bottom Panel": "切换终端底部面板",
  "Toggle Theme (Dark/Light)": "切换主题（暗/亮）",
  "Toggle View Controls Bottom Panel": "切换视图控制底部面板",
  "Toggle promotion of hovered widget": "切换悬停小部件的提升",
  "Undo": "撤销",
  "Unload Models": "卸载模型",
  "Unload Models and Execution Cache": "卸载模型和执行缓存",
  "Unlock Canvas": "解除锁定画布",
  "Unpack the selected Subgraph": "解包选中子工作流",
  "View": "视图",
  "Workflows": "工作流",
  "Zoom In": "放大画面",
  "Zoom Out": "缩小画面",
  "Zoom to fit": "缩放以适应",
  "linearMode_appModeToolbar_apps": "linearMode.appModeToolbar.apps"
};

// 这些是 FFmpeg 的固定枚举值。标签可读即可，不能改 options.values 或实际选中的值。
const H3_PRESET_NODES = new Set(["MiniMaxH3JointDecode", "H3VideoMerge"]);
const H3_PRESET_LABELS = Object.freeze({
  ultrafast: "最快", superfast: "超快", veryfast: "很快",
  faster: "较快", fast: "快速", medium: "中速", slow: "慢速",
});
const formattedWidgets = new WeakMap();

function applyOptionLabels(state, node, cls) {
  const scoped = state.nodes[cls]?.options;
  const h3 = H3_PRESET_NODES.has(cls);
  if (!h3 && !scoped) return false;
  let changed = false;
  for (const widget of node.widgets ?? []) {
    if (widget.type !== "combo" || !widget.options) continue;
    const presets = h3 && widget.name === "编码预设";
    const prefix = `[${JSON.stringify(widget.name)},`;
    if (!presets && !Object.keys(scoped ?? {}).some((key) => key.startsWith(prefix))) continue;
    if (formattedWidgets.get(widget) === widget.options.getOptionLabel && formattedWidgets.has(widget)) continue;
    const previous = widget.options.getOptionLabel;
    const format = function (value) {
      const raw = String(value ?? "");
      const label = lookup(scoped, JSON.stringify([widget.name, raw]));
      if (label) return label === raw ? (lookup(state.options, raw) && state.options[raw] !== raw ? `${raw}（原文）` : raw) : `${label}（${raw}）`;
      const preset = presets && lookup(H3_PRESET_LABELS, raw);
      if (preset) return `${raw}（${preset}）`;
      const global = lookup(state.options, raw);
      if (global && global !== raw) return state.optionLabelCounts.get(global) > 1 ? `${raw}（${global}）` : global;
      return (typeof previous === "function" ? previous.call(this, value) : undefined) ?? raw;
    };
    widget.options.getOptionLabel = format;
    formattedWidgets.set(widget, format);
    changed = true;
  }
  return changed;
}

export function createState(data, opts) {
  const options = data?.options || {};
  const optionLabelCounts = new Map();
  for (const label of Object.values(options)) {
    if (typeof label !== "string" || !label.trim()) continue;
    optionLabelCounts.set(label, (optionLabelCounts.get(label) ?? 0) + 1);
  }
  return {
    nodes: { ...DEFAULT_NODES, ...(data?.nodes || {}) },
    manualNodes: data?.manual_nodes || {},
    cats: { ...DEFAULT_CATS, ...(data?.categories || {}) },
    menus: { ...DEFAULT_MENUS, ...(data?.menus || {}) },
    options,   // 下拉框显示词典，原选项值不变
    optionLabelCounts,
    opts: { categories: true, menus: true, ...opts },
    origTitles: new Map(),   // class -> 原英文标题
    knownTitles: new Set(),  // 所有节点的原标题，上报菜单时用来排除
    titleZh: new Map(),      // 原英文标题 -> 中文标题（菜单里列出的节点名用）
    knownSegs: new Set(),    // 所有分类片段
    autogrow: new Map(),    // class -> 自动编号输入的模板
    pendingNodes: new WeakSet(),
    misses: new Set(),
  };
}

export function translateCategory(state, category) {
  if (typeof category !== "string" || !category) return category;
  return category
    .split("/")
    .map((seg) => {
      const key = seg.trim();
      state.knownSegs.add(key);
      return state.cats[key] ?? seg;
    })
    .join("/");
}

// 菜单里列出节点名时（如 rgthree 的节点子菜单）常去掉 "(rgthree)" 之类的后缀，所以两种写法都登记
function addTitleZh(state, orig, zh) {
  state.titleZh.set(orig, zh);
  const bare = orig.replace(/\s*\([^)]*\)\s*$/, "").trim();
  if (bare && bare !== orig && !state.titleZh.has(bare)) state.titleZh.set(bare, zh.replace(/\s*[（(][^）)]*[）)]\s*$/, "").trim() || zh);
}

const DYNAMIC_COMBO = "COMFY_DYNAMICCOMBO_V3";
const DYNAMIC_SLOT = "COMFY_DYNAMICSLOT_V3";
const AUTOGROW = "COMFY_AUTOGROW_V3";
const leafKey = (key) => typeof key === "string" ? key.slice(key.lastIndexOf(".") + 1) : "";
const lookup = (dict, key) => dict && typeof key === "string" && Object.hasOwn(dict, key) && typeof dict[key] === "string" ? dict[key] : undefined;

function inputSpecs(input) {
  return ["required", "optional"].flatMap((section) => Object.entries(input?.[section] ?? {}));
}

// 只处理 ComfyUI 的动态输入结构，不递归遍历任意参数值或下拉选项对象。
function applyInputDefinitions(t, input, templates, manual, prefix = "", active = new Set()) {
  if (!input || active.has(input)) return;
  active.add(input);
  for (const [key, spec] of inputSpecs(input)) {
    if (!Array.isArray(spec) || !spec[1] || typeof spec[1] !== "object") continue;
    const [type, opts] = spec;
    const path = prefix ? `${prefix}.${key}` : key;
    const tip = lookup(t.tooltips, path);
    if (tip && (lookup(manual?.tooltips, path) || !hasCJK(opts.tooltip))) opts.tooltip = tip;
    const label = lookup(manual?.inputs, path) ?? lookup(manual?.widgets, path);
    if (label) opts.display_name = label;
    if (type === DYNAMIC_COMBO && Array.isArray(opts.options)) {
      for (const option of opts.options) applyInputDefinitions(t, option?.inputs, templates, manual, path, active);
    } else if (type === DYNAMIC_SLOT) {
      applyInputDefinitions(t, opts.inputs, templates, manual, path, active);
    } else if (type === AUTOGROW && opts.template) {
      const { names, prefix: base = "", input: templateInput } = opts.template;
      const entries = inputSpecs(templateInput);
      for (const [templateKey, templateSpec] of entries) {
        const templatePath = `${path}.${templateKey}[*]`;
        const templateTip = lookup(t.tooltips, templatePath);
        if (templateTip && Array.isArray(templateSpec) && templateSpec[1] && typeof templateSpec[1] === "object" && !hasCJK(templateSpec[1].tooltip)) {
          templateSpec[1].tooltip = templateTip;
        }
        if (!Array.isArray(names)) {
          const stem = `${path}.${entries.length === 1 ? base : templateKey}`;
          templates.set(stem, templatePath);
        }
      }
    }
  }
  active.delete(input);
}

// 定义阶段写入内置控件读取的提示；名称由实例阶段应用，保留原始输入键。
function applyTooltips(state, t, def) {
  const templates = new Map();
  const manual = state.manualNodes[def.name];
  applyInputDefinitions(t, def.input, templates, manual);
  if (templates.size) state.autogrow.set(def.name, templates);
  else state.autogrow.delete(def.name);
  if (!t.tooltips) return;
  const names = Array.isArray(def.output_name) ? def.output_name : [];
  if (Array.isArray(def.output_tooltips)) {
    def.output_tooltips.forEach((tip, i) => {
      const zh = t.tooltips[names[i]];
      if (zh && (lookup(manual?.tooltips, names[i]) || (typeof tip === "string" && tip && !hasCJK(tip)))) def.output_tooltips[i] = zh;
    });
  }
}

// 节点定义阶段：标题 / 分类 / 说明 / 参数悬停提示
export function applyNodeDef(state, nodeData, nodeType) {
  if (!nodeData || typeof nodeData.name !== "string") return;
  const orig = nodeData.display_name || nodeData.name;
  state.knownTitles.add(orig);
  state.origTitles.set(nodeData.name, orig);
  { const z = state.nodes[nodeData.name]?.title; if (z && hasCJK(z)) addTitleZh(state, orig, z); }

  if (state.opts.categories && nodeData.category) {
    nodeData.category = translateCategory(state, nodeData.category);
    if (nodeType && typeof nodeType.category === "string") nodeType.category = nodeData.category;
  }

  const t = state.nodes[nodeData.name];
  if (!t) return;

  const manual = state.manualNodes[nodeData.name];
  if (t.title && (manual?.title || !hasCJK(orig))) {
    nodeData.display_name = t.title;
    if (nodeType) nodeType.title = t.title;
  }
  if (t.description && (manual?.description || !hasCJK(nodeData.description))) nodeData.description = t.description;

  applyTooltips(state, t, nodeData);
}

// 新版前端的节点库/搜索用的定义
export function applyVueNodeDef(state, def) {
  if (!def || typeof def.name !== "string") return;
  const orig = def.display_name || def.name;
  if (state.opts.categories && def.category) def.category = translateCategory(state, def.category);
  const t = state.nodes[def.name];
  if (!t) return;
  const manual = state.manualNodes[def.name];
  if (t.title && (manual?.title || !hasCJK(orig))) {
    def.display_name = t.title;
    // 保留英文名可搜索（旧版前端不认识该字段，无副作用）
    def.search_aliases = [...(Array.isArray(def.search_aliases) ? def.search_aliases : []), orig];
  }
  if (t.description && (manual?.description || !hasCJK(def.description))) def.description = t.description;
  applyTooltips(state, t, def);   // 新版前端的节点定义仓库也需要译文
}

function slotKey(item) {
  return item?.widget?.name ?? item?.name;
}

const nodeClass = (node) => node?.constructor?.comfyClass || node?.comfyClass || node?.type;

function inputTranslation(state, cls, keys, widget = false) {
  const t = state.nodes[cls];
  if (!t) return {};
  const primary = widget ? t.widgets : t.inputs;
  const fallback = widget ? t.inputs : t.widgets;
  const from = (key) => lookup(primary, key) ?? lookup(fallback, key);
  const exactKeys = [...new Set(keys)];
  let label = exactKeys.map(from).find(Boolean);
  let tooltip = exactKeys.map((key) => lookup(t.tooltips, key)).find(Boolean);
  for (const key of exactKeys) {
    const suffix = typeof key === "string" ? /\d+$/.exec(key)?.[0] : undefined;
    if (suffix === undefined) continue;
    // Try the longest matching stem first (a template prefix can itself end in digits).
    const templates = state.autogrow.get(cls);
    if (!templates) continue;
    for (let length = 1; length <= suffix.length && (!label || !tooltip); length++) {
      const pattern = templates.get(key.slice(0, -length));
      if (!pattern) continue;
      const base = from(pattern);
      if (!label && base) label = `${base} ${key.slice(-length)}`;
      tooltip ||= lookup(t.tooltips, pattern);
      break;
    }
  }
  // 兼容旧词典：路径译文优先，只在同一节点内回退到末级名称。
  label ||= exactKeys.map((key) => from(leafKey(key))).find(Boolean);
  return { label, tooltip };
}

function applyLabel(item, label, defaults = [], manual = false) {
  if (!label || label.length > 40 || item.label === label || (!manual && hasCJK(item.label))) return false;
  if (item.label && ![item.name, item.localized_name, slotKey(item), ...defaults].includes(item.label)) return false;
  if (!manual && !item.label && hasCJK(item.localized_name)) return false;
  item.label = label;
  return true;
}

// 替换子图外露控件的显示名时，从实际连线追溯来源；重名、分歧或未连接时不要猜。
function promotedLabel(state, node, input, active = new Set()) {
  if (!node?.isSubgraphNode?.() || !input || active.has(input)) return undefined;
  const graph = node.subgraph;
  const slot = input._subgraphSlot;
  if (!graph || !slot?.linkIds?.length) return undefined;
  active.add(input);
  const labels = new Set();
  try {
    for (const linkId of slot.linkIds) {
      const link = graph.getLink?.(linkId);
      const child = link && graph.getNodeById?.(link.target_id);
      const target = child?.inputs?.[link.target_slot];
      if (!target) return undefined;
      const label = child.isSubgraphNode?.()
        ? promotedLabel(state, child, target, active)
        : inputTranslation(state, nodeClass(child), [slotKey(target), target.name], true).label;
      if (!label) return undefined;
      labels.add(label);
    }
    return labels.size === 1 ? labels.values().next().value : undefined;
  } finally {
    active.delete(input);
  }
}

// 节点实例阶段：只更新显示属性；参数名、控件值和连线留给 ComfyUI。
export function applyNode(state, node) {
  if (!node) return;
  const cls = nodeClass(node);
  const t = state.nodes[cls];
  const subgraph = node.isSubgraphNode?.();
  let changed = applyOptionLabels(state, node, cls);
  const manual = state.manualNodes[cls];
  if (!changed && !t && !subgraph) return;
  if (t) {
    for (const input of node.inputs ?? []) {
      if (typeof input?.name !== "string") continue;
      const key = slotKey(input);
      const { label } = inputTranslation(state, cls, [input.name, key]);
      const correction = lookup(manual?.inputs, input.name) ?? lookup(manual?.widgets, key);
      changed = applyLabel(input, correction ?? label, [leafKey(input.name), leafKey(key)], !!correction) || changed;
    }
    for (const output of node.outputs ?? []) {
      if (typeof output?.name !== "string") continue;
      const correction = lookup(manual?.outputs, output.name);
      changed = applyLabel(output, correction ?? lookup(t.outputs, output.name), [], !!correction) || changed;
    }
    for (const widget of node.widgets ?? []) {
      if (typeof widget?.name !== "string") continue;
      const { label, tooltip } = inputTranslation(state, cls, [widget.name], true);
      const correction = lookup(manual?.widgets, widget.name) ?? lookup(manual?.inputs, widget.name);
      changed = applyLabel(widget, correction ?? label, [leafKey(widget.name)], !!correction) || changed;
      const manualTip = lookup(manual?.tooltips, widget.name);
      const newTip = manualTip ?? tooltip;
      if (newTip && widget.tooltip !== newTip && (manualTip || (widget.name.includes(".") && !hasCJK(widget.tooltip)))) {
        widget.tooltip = newTip;
        changed = true;
      }
    }
    if (t.title && node.title !== t.title && (node.title === state.origTitles.get(cls) || node.title === cls)) {
      node.title = t.title;
      changed = true;
    }
  }
  if (subgraph) {
    for (const widget of node.widgets ?? []) {
      const input = node.getSlotFromWidget?.(widget);
      if (!input || !input.widgetId) continue;
      // 自定义外露名（与来源字段名不一致）保留，不覆盖用户在子图上设置的名称。
      const slot = input._subgraphSlot;
      const links = slot?.linkIds;
      if (!links?.length) continue;
      const sourceNames = Array.from(links, (id) => {
        const link = node.subgraph.getLink?.(id);
        const child = link && node.subgraph.getNodeById?.(link.target_id);
        return slotKey(child?.inputs?.[link?.target_slot]);
      });
      const hostName = slot.name ?? input.name;
      if (!sourceNames.every((name) => name && (hostName === name || hostName === leafKey(name)))) continue;
      if (input.label && input.label !== input.name && input.label !== input.localized_name) continue;
      const label = promotedLabel(state, node, input);
      changed = applyLabel(widget, label, [input.name, hostName]) || changed;
    }
  }
  if (changed) node.setDirtyCanvas?.(true, true);
}

// 新建控件时前端会在 addInput/addWidget 返回后继续设置标签、调整数组顺序。
// 同一轮事件只排一次补译，等原生更新全部完成再读取最终实例。
export function wrapDynamic(state, node) {
  const cls = nodeClass(node);
  if (!state.nodes[cls] && !H3_PRESET_NODES.has(cls) && !node.isSubgraphNode?.()) return;
  for (const fn of ["addInput", "addOutput", "addWidget", "addCustomWidget", "addDOMWidget"]) {
    const orig = node[fn];
    if (typeof orig !== "function" || orig.__autoTranslate) continue;
    const wrapped = function (...args) {
      const result = orig.apply(this, args);
      if (!state.pendingNodes.has(this)) {
        state.pendingNodes.add(this);
        queueMicrotask(() => {
          state.pendingNodes.delete(this);
          try { applyNode(state, this); } catch (e) { /* 翻译失败不影响节点 */ }
        });
      }
      return result;
    };
    wrapped.__autoTranslate = true;
    node[fn] = wrapped;
  }
}

export function applyGraph(state, graph) {
  const seen = new Set();
  const visit = (g) => {
    if (!g || seen.has(g)) return;
    seen.add(g);
    for (const node of g.nodes ?? g._nodes ?? []) {
      if (node?.isSubgraphNode?.()) visit(node.subgraph);
      try { applyNode(state, node); wrapDynamic(state, node); } catch (e) { /* 跳过不兼容节点 */ }
    }
  };
  visit(graph);
}

// 右键菜单：只在菜单容器里做精确匹配替换，并收集没译文的英文
export function translateMenuTree(state, root, doc) {
  if (!root || !state.opts.menus) return;
  const d = doc || root.ownerDocument || globalThis.document;
  const walker = d.createTreeWalker(root, 4 /* NodeFilter.SHOW_TEXT */);
  const texts = [];
  while (walker.nextNode()) texts.push(walker.currentNode);
  for (const tn of texts) {
    const raw = tn.nodeValue;
    const s = raw?.trim();
    if (!s) continue;
    const inOpt = !!tn.parentElement?.closest?.(OPTION_SELECTOR);
    const option = inOpt ? lookup(state.options, s) : undefined;
    // 旧全局词库的不同英文若撞成同一中文，至少保留原值供区分。
    // 普通右键菜单与 H3 已经格式化的标签均不受此规则影响。
    const zh = inOpt
      ? (option && state.optionLabelCounts.get(option) > 1 && option !== s ? `${s}（${option}）` : option)
      : (state.menus[s] || state.titleZh.get(s));
    if (zh) {
      // 值没变就不要写：写同样的文字也会触发 characterData 变动，被观察器再次捕获，形成死循环卡死网页
      const next = raw.replace(s, () => zh.replace(/&nbsp;/g, "\u00a0"));
      if (next !== raw) tn.nodeValue = next;
    } else if (!inOpt && hasWords(s) && !hasCJK(s) && s.length <= 120
               && !state.knownTitles.has(s) && !state.knownSegs.has(s)) {
      state.misses.add(s);
    }
  }
}

export function handleAddedNode(state, el) {
  // Vue 更新文字常是 el.textContent = ...，新增的是文本节点而不是元素，要回到它的父元素处理
  if (el && el.nodeType === 3) el = el.parentElement;
  if (!el || el.nodeType !== 1) return;
  if (el.matches?.(MENU_SELECTOR) || el.closest?.(MENU_SELECTOR)) {
    translateMenuTree(state, el);
  } else if (el.querySelectorAll) {
    el.querySelectorAll(MENU_SELECTOR).forEach((m) => translateMenuTree(state, m));
  }
}

// 启动后扫一遍 LiteGraph 里已注册的节点类型：前端自带节点（Note、Reroute、Primitive…）
// 不经过后端节点定义，只能在这里改标题和分类。已经是中文的不动，可重复执行。
export function applyRegisteredTypes(state, registry) {
  if (!registry) return 0;
  let n = 0;
  for (const [type, nt] of Object.entries(registry)) {
    if (!nt) continue;
    const id = nt.comfyClass || nt.type || type;
    const t = state.nodes[id];
    if (t?.title && typeof nt.title === "string" && nt.title && !hasCJK(nt.title)) {
      state.origTitles.set(id, nt.title);
      state.knownTitles.add(nt.title);
      addTitleZh(state, nt.title, t.title);
      nt.title = t.title;
      n++;
    }
    if (state.opts.categories && typeof nt.category === "string" && nt.category) {
      const c = translateCategory(state, nt.category);
      if (c !== nt.category) nt.category = c;
    }
  }
  return n;
}

