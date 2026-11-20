"""
核心逻辑（不依赖 ComfyUI，方便单独测试）。

流程：
1. 收集已安装节点的完整上下文（标题/分类/说明/参数类型/悬停提示）
2. 按节点分批发给大模型（OpenAI 兼容接口，如 DeepSeek），让模型结合用途意译
3. 结果写入本插件自己的 translations/zh-CN/*.json，由本插件的前端脚本读取显示
"""
import json
import os
import re
import time
import inspect
import threading
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

CJK_RE = re.compile(r"[一-鿿]")
LETTER_RE = re.compile(r"[A-Za-z]")
DYNAMIC_COMBO = "COMFY_DYNAMICCOMBO_V3"
DYNAMIC_SLOT = "COMFY_DYNAMICSLOT_V3"
AUTOGROW = "COMFY_AUTOGROW_V3"
WIDGET_TYPES = {"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO", DYNAMIC_COMBO}
LOCALE = "zh-CN"
_write_lock = threading.Lock()
_errors = []  # 最近的 API 错误，用于在报告里说明失败原因
_issues = []   # 本次被拒收的译文：{节点, 项, 原因, 返回内容}，用来在报告里说明为什么没汉化
_bad_raw = []  # 解析失败的原始返回（只留最近几条，写进日志便于排查）

DEFAULT_GLOSSARY = {
    "seed": "种子", "steps": "步数", "cfg": "CFG", "sampler": "采样器", "sampler_name": "采样器",
    "scheduler": "调度器", "denoise": "降噪强度", "latent": "潜空间", "mask": "遮罩",
    "conditioning": "条件", "positive": "正面", "negative": "负面", "prompt": "提示词",
    "image": "图像", "images": "图像", "video": "视频", "audio": "音频", "model": "模型",
    "clip": "CLIP", "vae": "VAE", "lora": "LoRA", "controlnet": "ControlNet",
    "upscale": "放大", "strength": "强度", "width": "宽度", "height": "高度",
    "batch_size": "批次大小", "text": "文本", "string": "字符串", "int": "整数",
    "float": "浮点数", "boolean": "布尔", "bypass": "旁路", "loader": "加载器",
    "load": "加载", "save": "保存", "preview": "预览", "crop": "裁剪", "resize": "缩放",
    "threshold": "阈值", "offset": "偏移", "frame": "帧", "fps": "帧率", "noise": "噪波",
    "guidance": "引导", "tile": "分块", "mode": "模式", "enable": "启用",
}


# ----------------------------------------------------------------- 配置 / 路径
def load_config(pack_dir):
    cfg = {
        "api_key": "",
        "api_base": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "max_tokens": 8000,
        "json_mode": None,  # None = 地址里含 deepseek 时自动开启
    }
    path = os.path.join(pack_dir, "config.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg.update({k: v for k, v in json.load(f).items() if v not in (None, "")})
        except Exception as e:
            print(f"[节点汉化] config.json 读取失败: {e}")
    env_key = os.environ.get("AUTO_TRANSLATE_API_KEY", "").strip()
    if env_key:
        cfg["api_key"] = env_key
    return cfg


def load_glossary(pack_dir):
    g = dict(DEFAULT_GLOSSARY)
    path = os.path.join(pack_dir, "glossary.json")  # 用户自己的术语表，优先级最高
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                g.update(json.load(f))
        except Exception as e:
            print(f"[节点汉化] glossary.json 读取失败: {e}")
    return g


def tr_dir(pack_dir):
    return os.path.join(pack_dir, "translations", LOCALE)


def _read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, type(default if default is not None else {})) else (default if default is not None else {})
    except Exception:
        return default if default is not None else {}


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


OVERRIDE_NAME = "手动翻译.json"
OVERRIDE_TEMPLATE = {
    "_说明": "这里是你手动写的译文，优先级最高，AI 重翻也不会覆盖。改完保存，刷新网页(F5)生效。"
             "以下划线 _ 开头的项会被忽略：把示例前面的“_示例_”去掉就会生效，不需要的示例直接删掉。",
    "节点": {
        "_示例_节点类名": {
            "标题": "节点显示名", "说明": "节点说明",
            "输入": {"输入口英文名": "中文"}, "参数": {"参数英文名": "中文"},
            "输出": {"输出口英文名": "中文"}, "提示": {"参数或端口英文名": "悬停提示的中文"},
        }
    },
    "分类": {"_示例_Latent": "潜空间"},
    "菜单": {"_示例_Add Node": "添加节点"},
    "选项": {"_示例_scale by multiplier": "按倍数缩放"},
}
_NODE_FIELDS = {"标题": "title", "说明": "description", "输入": "inputs", "参数": "widgets",
                "输出": "outputs", "提示": "tooltips", "下拉选项": "options"}


def override_path(pack_dir):
    return os.path.join(tr_dir(pack_dir), OVERRIDE_NAME)


def ensure_override_template(pack_dir):
    path = override_path(pack_dir)
    if not os.path.exists(path):
        try:
            _write_json(path, OVERRIDE_TEMPLATE)
        except OSError:
            pass


def load_overrides(pack_dir):
    """读取 手动翻译.json，返回 (内部结构, 错误信息)。文件写坏了也不会让翻译整体失效。"""
    out = {"nodes": {}, "categories": {}, "menus": {}, "options": {}}
    path = override_path(pack_dir)
    if not os.path.exists(path):
        return out, None
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise ValueError("最外层必须是 { } 对象")
    except Exception as e:
        return out, f"{OVERRIDE_NAME} 读取失败（已忽略，请检查 JSON 格式，如逗号、引号）：{e}"
    def flat(d):
        return {k: v for k, v in d.items() if isinstance(d, dict) and not k.startswith("_")
                and isinstance(k, str) and isinstance(v, str) and v.strip()} if isinstance(d, dict) else {}
    out["categories"], out["menus"], out["options"] = flat(raw.get("分类")), flat(raw.get("菜单")), flat(raw.get("选项"))
    nodes = raw.get("节点")
    for cls, e in (nodes.items() if isinstance(nodes, dict) else []):
        if cls.startswith("_") or not isinstance(e, dict):
            continue
        ne = {}
        for zh_key, key in _NODE_FIELDS.items():
            v = e.get(zh_key)
            if key in ("title", "description"):
                if isinstance(v, str) and v.strip():
                    ne[key] = v.strip()
            else:
                f = flat(v)  # 下拉选项使用由参数路径、原始值构成的 JSON 键
                if f:
                    ne[key] = f
        if ne:
            out["nodes"][cls] = ne
    return out, None


def merge_nodes(base, over):
    """手动翻译覆盖到 AI 译文上（逐字段、逐名字覆盖，没写的仍用 AI 的）。"""
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for cls, e in over.items():
        cur = out.get(cls) if isinstance(out.get(cls), dict) else {}
        cur = {k: (dict(v) if isinstance(v, dict) else v) for k, v in cur.items()}
        for k, v in e.items():
            if isinstance(v, dict):
                cur[k] = {**(cur.get(k) if isinstance(cur.get(k), dict) else {}), **v}
            else:
                cur[k] = v
        out[cls] = cur
    return out


def load_translations(pack_dir):
    d = tr_dir(pack_dir)
    ov, _ = load_overrides(pack_dir)
    return {
        "nodes": merge_nodes(_read_json(os.path.join(d, "nodes.json")), ov["nodes"]),
        "manual_nodes": ov["nodes"],
        "categories": {**_read_json(os.path.join(d, "categories.json")), **ov["categories"]},
        "menus": {**_read_json(os.path.join(d, "menus.json")), **ov["menus"]},
        "options": {**_read_json(os.path.join(d, "options.json")), **ov["options"]},
    }


def add_pending_menus(pack_dir, items, cap=1000):
    """前端上报的、还没有译文的菜单文字。"""
    d = tr_dir(pack_dir)
    with _write_lock:
        known = set(_read_json(os.path.join(d, "menus.json")))
        pending = _read_json(os.path.join(d, "menus_pending.json"), [])
        have = set(pending)
        for s in items:
            if (isinstance(s, str) and 3 <= len(s) <= 120 and needs_translation(s)
                    and s not in known and s not in have and len(pending) < cap):
                pending.append(s)
                have.add(s)
        _write_json(os.path.join(d, "menus_pending.json"), pending)
    return len(pending)


# ----------------------------------------------------------------- 收集节点
def needs_translation(s):
    return isinstance(s, str) and bool(LETTER_RE.search(s)) and not CJK_RE.search(s)


BUILTIN_MODULES = {"nodes", "comfy_extras", "comfy_api_nodes", "comfy_api", "comfy_execution"}
BUILTIN_PATH_TOPS = {"nodes.py", "comfy_extras", "comfy_api_nodes", "comfy_api", "comfy", "comfy_execution"}


def is_custom_node(cls, comfy_root=None):
    """只有『明确是 ComfyUI 自带』的才算内置，其余一律算第三方。
    1) 优先看 ComfyUI 给每个节点标的来源 RELATIVE_PYTHON_MODULE（内置是 nodes / comfy_extras.* 等）；
    2) 没有的话看文件是否在 ComfyUI 自己的目录里（不依赖目录必须叫 custom_nodes，
       所以从别处加载节点包的插件也能识别）；
    3) 都判断不了时，退回到路径里是否含 custom_nodes。"""
    mod = getattr(cls, "RELATIVE_PYTHON_MODULE", None)
    if isinstance(mod, str) and mod:
        return mod.split(".")[0] not in BUILTIN_MODULES
    try:
        path = os.path.abspath(inspect.getfile(cls))
    except (TypeError, OSError):
        return True
    if comfy_root:
        rel = os.path.relpath(path, comfy_root).replace("\\", "/")
        if not rel.startswith(".."):
            return rel.split("/")[0] not in BUILTIN_PATH_TOPS
        return True  # 在 ComfyUI 目录之外 → 肯定是第三方
    return "custom_nodes" in path.replace("\\", "/").split("/")


def pack_key(cls):
    """用于报告里按插件统计的名字"""
    mod = getattr(cls, "RELATIVE_PYTHON_MODULE", None)
    if isinstance(mod, str) and mod:
        return ".".join(mod.split(".")[:2])
    try:
        return os.path.basename(os.path.dirname(inspect.getfile(cls))) or "?"
    except (TypeError, OSError):
        return "?"


def _spec_parts(spec):
    if not isinstance(spec, (list, tuple)) or not spec:
        return spec, {}
    opts = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
    return spec[0], opts


def _parse_spec(spec):
    t, opts = _spec_parts(spec)
    options = None
    if isinstance(t, (list, tuple)):
        type_s, options = "COMBO", [str(x) for x in t[:6]]
    else:
        type_s = str(t)
        if type_s in ("COMBO", DYNAMIC_COMBO):
            options = _full_options(spec)[:6]
    is_widget = type_s in WIDGET_TYPES and not opts.get("forceInput")
    tip = opts.get("tooltip")
    return type_s, options, is_widget, (tip[:240] if isinstance(tip, str) else None)


# H3 编码预设有固定双语标签，不作为脱离上下文的普通选项重新翻译。
_FIXED_OPTION_WIDGETS = {("MiniMaxH3JointDecode", "编码预设"), ("H3VideoMerge", "编码预设")}
OPTION_RE = re.compile(r"[A-Za-z][A-Za-z0-9 _+\-]*")


def is_display_option(v):
    """只翻“像单词”的下拉选项（如 scale by multiplier）。文件名、路径、带点号或斜杠的不碰。"""
    return (isinstance(v, str) and len(v) <= 40 and bool(OPTION_RE.fullmatch(v))
            and sum(ch.isalpha() for ch in v) >= 2)


def _full_options(spec):
    t, opts = _spec_parts(spec)
    if isinstance(t, (list, tuple)):
        return [x for x in t if isinstance(x, str)]
    values = opts.get("options")
    if not isinstance(values, (list, tuple)):
        return []
    if t == "COMBO":
        return [x for x in values if isinstance(x, str)]
    if t == DYNAMIC_COMBO:
        return [x["key"] for x in values if isinstance(x, dict) and isinstance(x.get("key"), str)]
    return []


def _input_specs(sections):
    if isinstance(sections, dict):
        for section in ("required", "optional"):
            entries = sections.get(section)
            if isinstance(entries, dict):
                yield from ((k, v) for k, v in entries.items() if isinstance(k, str) and k)


def _walk_input_specs(sections, prefix="", active=None):
    """按前端使用的点号路径展开动态输入；分支选项值不是路径的一部分。"""
    if not isinstance(sections, dict):
        return
    active = set() if active is None else active
    marker = id(sections)
    if marker in active:
        return
    active.add(marker)
    try:
        for key, spec in _input_specs(sections):
            path = f"{prefix}.{key}" if prefix else key
            yield path, spec, False
            t, opts = _spec_parts(spec)
            if t == DYNAMIC_COMBO and isinstance(opts.get("options"), (list, tuple)):
                for option in opts["options"]:
                    if isinstance(option, dict):
                        yield from _walk_input_specs(option.get("inputs"), path, active)
            elif t == DYNAMIC_SLOT:
                yield from _walk_input_specs(opts.get("inputs"), path, active)
            elif t == AUTOGROW:
                template = opts.get("template")
                if not isinstance(template, dict):
                    continue
                entries = list(_input_specs(template.get("input")))
                names = template.get("names")
                for template_key, template_spec in entries:
                    # 自动编号的端口共享一个词条，运行时再拼上原编号。
                    yield f"{path}.{template_key}[*]", template_spec, True
                    if isinstance(names, (list, tuple)):
                        for child in names[:100]:
                            if isinstance(child, str) and child:
                                yield f"{path}.{child}", template_spec, True
    finally:
        active.remove(marker)


def collect_nodes(mappings, display_names, scope="custom_only", limit=0, comfy_root=None):
    nodes = {}
    for class_name, cls in mappings.items():
        if scope == "custom_only" and not is_custom_node(cls, comfy_root):
            continue
        try:
            it = cls.INPUT_TYPES() or {}
        except Exception:
            it = {}
        inputs, widgets, opt_values, editable_options = {}, {}, {}, {}
        for key, spec, force_port in _walk_input_specs(it):
            full = _full_options(spec)
            if not force_port and 0 < len(full) <= 80:
                for value in full:
                    # 保留短技术枚举，但不把文件名、路径、模型资产等长列表塞进编辑窗。
                    if value.strip() and len(value) <= 80 and not any(c in value for c in ("/", "\\", "\n")):
                        editable_options[(key, value)] = None
            if 0 < len(full) <= 40 and (class_name, key) not in _FIXED_OPTION_WIDGETS:
                for v in full:
                    if is_display_option(v):
                        opt_values.setdefault(v, key)
            type_s, options, is_widget, tip = _parse_spec(spec)
            entries = widgets if is_widget and not force_port else inputs
            item = entries.setdefault(key, {"name": key, "type": type_s})
            if options:
                item["options"] = list(dict.fromkeys(item.get("options", []) + options))[:6]
            if tip:
                item.setdefault("tooltip", tip)
        names = getattr(cls, "RETURN_NAMES", None)
        types = getattr(cls, "RETURN_TYPES", None) or ()
        outputs = []
        for i, t in enumerate(types):
            n = names[i] if names and i < len(names) else t
            outputs.append({"name": str(n), "type": str(t)})
        desc = getattr(cls, "DESCRIPTION", "")
        nodes[class_name] = {
            "id": class_name,
            "pack": pack_key(cls),
            "title": str(display_names.get(class_name, class_name)),
            "category": str(getattr(cls, "CATEGORY", "") or ""),
            "description": desc[:300] if isinstance(desc, str) else "",
            "inputs": list(inputs.values()), "widgets": list(widgets.values()), "outputs": outputs,
            "opt_values": opt_values, "editable_options": list(editable_options),
        }
        if limit and len(nodes) >= limit:
            break
    return nodes


def option_key(widget, original):
    return json.dumps([widget, original], ensure_ascii=False, separators=(",", ":"))


def node_translation_fields(info, entry, global_options=None):
    fields = []
    entry = entry if isinstance(entry, dict) else {}
    def add(section, key, source, value, **extra):
        fields.append({"section": section, "key": key, "source": source, "value": value,
                       "needs_translation": needs_translation(_parameter_leaf(source) if section in ("inputs", "widgets", "outputs") else source), **extra})
    for field in ("title", "description"):
        if info[field]:
            add(field, "", info[field], entry.get(field, ""))
    seen_tips = set()
    for section in ("inputs", "widgets", "outputs"):
        for item in info[section]:
            name = item["name"]
            add(section, name, name, (entry.get(section) or {}).get(name, ""))
            if item.get("tooltip") and name not in seen_tips:
                add("tooltips", name, item["tooltip"], (entry.get("tooltips") or {}).get(name, ""))
                seen_tips.add(name)
    for widget, original in info.get("editable_options", []):
        key = option_key(widget, original)
        value = (entry.get("options") or {}).get(key) or (global_options or {}).get(original, "")
        add("options", key, original, value, widget=widget)
    return fields


def save_node_overrides(pack_dir, info, edits):
    allowed = {(f["section"], f["key"]) for f in node_translation_fields(info, {})}
    labels = {field: label for label, field in _NODE_FIELDS.items()}
    patch = {}
    if not isinstance(edits, list) or not edits or len(edits) > len(allowed):
        raise ValueError("没有有效修改，或修改项超出节点定义")
    for change in edits:
        if not isinstance(change, dict):
            raise ValueError("译文格式无效")
        section, key, value = change.get("section"), change.get("key"), change.get("value")
        if not all(isinstance(v, str) for v in (section, key, value)) or (section, key) not in allowed:
            raise ValueError("存在未知字段；请重新打开编辑窗口")
        value = value.strip()
        limit = 1200 if section in ("description", "tooltips") else 80 if section == "title" else 40
        if not value or len(value) > limit or (section not in ("description", "tooltips") and "\n" in value):
            raise ValueError(f"{key or labels[section]}：译文不能为空，最多 {limit} 字；名称不能换行")
        if key:
            patch.setdefault(labels[section], {})[key] = value
        else:
            patch[labels[section]] = value
    node_id = info["id"]
    if node_id.startswith("_"):
        raise ValueError("该节点类名不支持手动词库")
    path = override_path(pack_dir)
    with _write_lock:
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8-sig") as f:
                    raw = json.load(f)
            except (OSError, ValueError) as e:
                raise ValueError("现有手动词库无法读取，未覆盖原文件") from e
        else:
            raw = {}
        if not isinstance(raw, dict) or not isinstance(raw.get("节点", {}), dict):
            raise ValueError("现有手动词库格式无效，未覆盖原文件")
        entries = raw.setdefault("节点", {})
        entries[node_id] = _merge_entry(entries.get(node_id), patch, overwrite=True)
        _write_json(path, raw)
    return len(edits)


IDENT_RE = re.compile(r"[A-Za-z0-9]_[A-Za-z0-9]|[a-z][A-Z]")


def _parameter_leaf(name):
    return name.rsplit(".", 1)[-1].removesuffix("[*]") if isinstance(name, str) else name


def is_identifier(name):
    """snake_case / camelCase 形式的参数名：这类一定该有中文名，没有就是漏翻了。"""
    leaf = _parameter_leaf(name)
    return isinstance(leaf, str) and bool(IDENT_RE.search(leaf)) and not CJK_RE.search(leaf)


def scrub_bad_labels(done_nodes):
    """一次性迁移：清掉旧版本留下的明显错误译文，让新提示词重新翻一遍。
    1) 把说明长句填成名称的；2) 下划线/驼峰名的“译文”里没有中文的（旧版留下的原样或英文词，当时模型没被允许自己判断）。
    返回 {节点: {(段, 名)}}。"""
    scrubbed = {}
    for cls, e in done_nodes.items():
        if not isinstance(e, dict):
            continue
        for sect in ("inputs", "widgets", "outputs"):
            d = e.get(sect)
            if not isinstance(d, dict):
                continue
            bad = [k for k, v in d.items() if isinstance(v, str) and (
                len(v) > 40 or re.search("[。！？；]", v)
                or (is_identifier(k) and not CJK_RE.search(v)))]   # 下划线名的译文里没有中文：旧版留下的英文词或原样
            for k in bad:
                del d[k]
                scrubbed.setdefault(cls, set()).add((sect, k))
            if not d:
                del e[sect]
    return scrubbed


def _merge_entry(old, new, overwrite=False):
    """只合并返回的字段；覆盖模式也不删除未返回的旧译文。"""
    if not isinstance(old, dict):
        return new
    out = dict(old)
    for k, v in new.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v} if overwrite else {**v, **out[k]}
        elif overwrite or k not in out:
            out[k] = v
    return out


ALL_KINDS = frozenset({"title", "names", "tips"})   # 标题和说明 / 端口和参数名 / 悬停提示


def make_record(info, entry=None, giveup=(), kinds=ALL_KINDS):
    """按清单逐项检查：标题、说明、每个端口/参数名、每条悬停提示，在已有译文 entry 里没有的才发给 AI。
    entry=None 表示全部都要翻。返回 None 表示这个节点已经齐全。"""
    entry = entry if isinstance(entry, dict) else {}
    def has(sect, name):
        d = entry.get(sect)
        return (isinstance(d, dict) and name in d) or f"{sect}:{name}" in giveup
    rec = {"id": info["id"], "category": info["category"]}
    if "title" in kinds and needs_translation(info["title"]) and "title" not in entry and "title" not in giveup:
        rec["title"] = info["title"]
    if "title" in kinds and needs_translation(info["description"]) and "description" not in entry and "description" not in giveup:
        rec["description"] = info["description"]
    found = bool(rec.keys() & {"title", "description"})
    sent_tips = set()
    for sect in ("inputs", "widgets", "outputs"):
        items = []
        for x in info[sect]:
            want_name = "names" in kinds and needs_translation(_parameter_leaf(x["name"])) and not has(sect, x["name"])
            want_tip = ("tips" in kinds and needs_translation(x.get("tooltip"))
                        and not has("tooltips", x["name"]) and x["name"] not in sent_tips)
            if want_name or want_tip:
                y = dict(x)
                if not want_name:
                    y["skip_name"] = True       # 只翻提示，不重新请求已有或已暂停的名称
                if want_tip:
                    sent_tips.add(x["name"])
                else:
                    y.pop("tooltip", None)
                items.append(y)
        if items:
            rec[sect] = items
            found = True
    return rec if found else None


MAX_TRIES = 2   # 同一项模型连续两次都没给译文就先放弃，避免每次运行都白花一次请求


def rec_keys(rec, entry=None, kinds=ALL_KINDS):
    """一个记录里这次真正在等译文的项：title、description、段:名字、tooltips:名字。
    entry 是请求前已有的译文（已有的名字不算等待项）。"""
    entry = entry if isinstance(entry, dict) else {}
    keys = {k for k in ("title", "description") if k in rec}
    for sect in ("inputs", "widgets", "outputs"):
        have = entry.get(sect) if isinstance(entry.get(sect), dict) else {}
        for x in rec.get(sect, []):
            if ("names" in kinds and not x.get("skip_name")
                    and needs_translation(_parameter_leaf(x["name"])) and x["name"] not in have):
                keys.add(f"{sect}:{x['name']}")
            if "tips" in kinds and needs_translation(x.get("tooltip")):
                keys.add(f"tooltips:{x['name']}")
    return keys


def entry_keys(entry):
    keys = {k for k in ("title", "description") if isinstance(entry, dict) and k in entry}
    for sect in ("inputs", "widgets", "outputs", "tooltips"):
        d = entry.get(sect) if isinstance(entry, dict) else None
        if isinstance(d, dict):
            keys |= {f"{sect}:{n}" for n in d}
    return keys


def count_missing(rec, entry=None, kinds=ALL_KINDS):
    """一个缺口记录里各类缺项的数量。"""
    keys = rec_keys(rec, entry, kinds) if rec else set()
    return {"标题": int("title" in keys), "说明": int("description" in keys),
            "端口/参数名": sum(1 for k in keys if k.split(":")[0] in ("inputs", "widgets", "outputs")),
            "悬停提示": sum(1 for k in keys if k.startswith("tooltips:"))}




def _close_brackets(s):
    """修复两类常见毛病：漏写结尾的 } 或 ]；} 或 ] 前面多出的逗号。
    若结尾处字符串还没闭合（说明输出被截断），返回 None——宁可失败重试，也不收残缺内容。"""
    out, stack, in_str, i, n = [], [], False, 0, len(s)
    while i < n:
        c = s[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(s[i + 1])
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif c == ",":
            j = i + 1
            while j < n and s[j] in " \t\r\n":
                j += 1
            if j < n and s[j] not in "}]":  # 后面直接是 } 或 ] 或已到结尾的逗号是多余的，丢掉
                out.append(c)
        else:
            if c in "{[":
                stack.append("}" if c == "{" else "]")
            elif c in "}]" and stack:
                stack.pop()
            out.append(c)
        i += 1
    if in_str:
        return None
    return "".join(out).rstrip() + "".join(reversed(stack))


def _extract_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("响应里没有 JSON（可能被截断）")
    body = text[start:end + 1]
    try:
        return json.loads(body, strict=False)  # strict=False：允许字符串里出现真换行
    except json.JSONDecodeError as e1:
        fixed = _close_brackets(body)
        if fixed is not None:
            try:
                return json.loads(fixed, strict=False)
            except json.JSONDecodeError:
                pass
        near = body[max(0, e1.pos - 30):e1.pos + 30].replace("\n", " ")
        at_end = "（在结尾处，可能漏写括号或被截断）" if e1.pos >= len(body) - 2 else ""
        raise ValueError(f"JSON 格式错误：{e1.msg}{at_end}（出错位置附近：{near}）")


class AuthError(RuntimeError):
    pass


def call_api(cfg, api_base, model, messages, timeout=180, retries=3):
    url = api_base.rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    body = {"model": model, "messages": messages, "temperature": 0.3,
            "max_tokens": int(cfg.get("max_tokens") or 8000)}
    json_mode = cfg.get("json_mode")
    if json_mode is None:
        json_mode = "deepseek" in api_base.lower()
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {cfg['api_key']}"}
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                out = json.loads(resp.read().decode("utf-8"))
            return out["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:300]
            last = f"HTTP {e.code}: {detail}"
            if e.code in (401, 403):
                raise AuthError(last)
            if e.code in (400, 404):
                raise RuntimeError(last)
        except Exception as e:
            last = str(e)
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"API 调用失败: {last}")


# ----------------------------------------------------------------- 提示词
SYSTEM_TEMPLATE = """你是一名资深的软件本地化翻译官，长期为 ComfyUI、Stable Diffusion、视频与音频生成等 AI 工作流工具做简体中文界面本地化，熟悉社区里约定俗成的中文叫法，也清楚哪些词不该翻。你的译文会直接显示在节点界面上。

【你的职责】
把我给你的英文界面文字（节点名称、说明、端口名、参数名、悬停提示、下拉选项、菜单文字）翻译成简体中文。每一项“翻还是不翻”由你自己判断，我不会替你做限制；你的目标是让中文用户一眼看懂这个控件是做什么的。

【怎么判断翻不翻】
1. 应当翻译：普通英文单词和短语。snake_case、camelCase、全大写下划线形式的名字，先还原成英文含义再翻译。
   例：crop_scale→裁剪比例；aspect_ratio→宽高比；min_padding_allowed→最小留白；jpeg_quality→JPEG 质量；seamless_loop→无缝循环；IMAGE→图像；mask_out→输出遮罩。
2. 应当保留英文（原样返回）：模型/算法/项目/品牌名与专有缩写（如 ControlNet、VAE、CLIP、LoRA、SDXL、Flux、GGUF、IPAdapter、SAM、YOLO、KJNodes、rgthree）、文件格式、代码、占位符（{}、%s）、快捷键（Ctrl+C）、单位、版本号。
3. 混合时，保留专有名词，其余翻译：VAE Decode→VAE 解码；lora_name_15→LoRA 15；Load CLIP→加载 CLIP；top_p→Top-P（核采样）这类业内通用参数名，可保留英文，也可括注中文，你自己权衡哪个更易懂。
4. 拿不准时，选对中文用户更容易理解的写法；确实应该保留的，就原样返回，这是合法的答案。

【各类文字的写法】
- 端口名、参数名：显示空间很窄，用简短名称，一般 2~6 个汉字，不要写成说明句。
- 节点标题：简洁的功能名称，尽量不超过 12 个字；标题里的插件/品牌前缀可以保留，如“KJ 采样器”。
- 说明、悬停提示：完整通顺的一两句话，保留其中的专有名词、占位符、数字、单位和示例值，不要漏掉关键限制条件（如 0 表示不限制）。
- 下拉选项、菜单文字：简短的中文界面用语；选项如果是算法名、模型名、型号，保留英文。
- 开头的 emoji、括号里的快捷键和缩写原样保留。

【一致性】
同一个英文词在所有节点里译法保持一致。下面是术语表（英文=中文），遇到相同的词请遵守：
{glossary}

【输出格式】
- 只输出一个 JSON 对象，不要任何解释、不要 Markdown 代码围栏。
- 键必须与我给的输入逐字一致；每一项都必须返回，不得遗漏，无需翻译的原样返回。
- 输入里的 tooltip 只是帮你理解这个控件用途的参考，不要把它的译文填进名称里；它的译文只放在 tooltips 字段。
- 译文里不要出现英文双引号 "，需要引用时用中文引号“”或「」；译文里不要换行。"""


def system_prompt(glossary):
    gl = "；".join(f"{k}={v}" for k, v in glossary.items())
    return SYSTEM_TEMPLATE.replace("{glossary}", gl)


NODE_TASK = (
    "下面是若干个 ComfyUI 节点（JSON 数组）。每个节点只列出了需要翻译的项。\n"
    "请返回 JSON 对象：键是节点 id，值的结构为：\n"
    '{"title": 节点标题译文, "description": 说明译文, '
    '"inputs": {输入名: 译文}, "widgets": {参数名: 译文}, "outputs": {输出名: 译文}, '
    '"tooltips": {参数或输入名: 悬停提示译文}}\n'
    "没有对应项的字段可以省略；category 只是给你理解上下文用的，不需要翻译。\n"
    "重要：inputs / widgets / outputs 里填的是显示在节点上的【短名称】（2~6 个汉字，如 max_resolution→最大分辨率），"
    "绝不能写成说明句；每一项自带的 tooltip 只是帮你理解用途的参考，不要把它的翻译填到名称里。"
    "tooltip 的译文只放在 tooltips 字段，没有 tooltip 的项不需要 tooltips。"
    "有 skip_name: true 的参数只翻悬停提示，不返回名称。\n"
    "参数名可能是 mode.scale 这样的嵌套路径，或 items.image[*] 这样的编号模板。输出的键保留完整原名，"
    "只翻译末尾参数的含义；模板的译文不要带编号或 [*]。\n\n"
)

CATEGORY_TASK = (
    "下面是 ComfyUI 节点菜单里的分类名片段（JSON 数组，附带它们出现的完整路径作参考）。"
    "请返回 JSON 对象：键是 segment 原文，值是简短的中文分类名（不超过 6 个汉字，品牌/插件名保留英文）。\n\n"
)

CATEGORY_TASK_ALL = (
    "下面是 ComfyUI 节点菜单里的分类名片段（JSON 数组，附带它们出现的完整路径作参考）。\n"
    "本任务中品牌/插件名也要汉化：给每个分类名起一个简短的中文菜单名（不超过 8 个汉字）。"
    "品牌或插件名可以取其含义或加类别词，例如 KJNodes→KJ工具节点，Efficiency Nodes→效率节点，"
    "rgthree→rgthree 工具集，ControlNet Preprocessors→ControlNet 预处理器；"
    "通用词直接翻译（Latent→潜空间，utilities→工具）；实在无法翻译的专有缩写保留英文并加类别词（如 SEEDVR2→SEEDVR2 放大）。"
    "不要原样返回英文。\n"
    "返回 JSON 对象：键是 segment 原文，值是中文名。\n\n"
)

OPTION_TASK = (
    "下面是 ComfyUI 下拉选项。每项包含原文 option 和它出现过的若干组节点 node / 参数 widget。\n"
    "译文全局共用；若不同节点里的含义不一致，或不能确认含义，原样保留。"
    "普通界面词翻成简短中文（不超过 8 个汉字），例如 enable/disable→启用/禁用。"
    "算法名、模型名、编解码预设和技术标识（如 bicubic、euler、fp16、ultrafast）保留原文。"
    "不同原值不要压成同一个中文标签，无法区分就保留英文。\n"
    "返回 JSON 对象：键是 option 原文，值是显示文字。\n\n"
)

MENU_TASK = (
    "下面是 ComfyUI 右键菜单/界面里的英文文字（JSON 数组）。请翻译成简洁的中文界面用语，"
    "括号里的快捷键、缩写（如 Ctrl+C、TCL、MEC）原样保留。返回 JSON 对象：键是原文，值是译文。\n\n"
)


# ----------------------------------------------------------------- 结果校验
def _label_problem(k, v):
    """只拦“格式级”的垃圾（引号/换行是 JSON 修复过头的产物，超长多半是把说明填进了名称）。
    译得好不好、该不该保留英文，一律由模型自己决定，这里不再替它判断。"""
    if '"' in v or "\n" in v:
        return "译文里含英文引号或换行（多半是模型输出格式出错）"
    if len(v) > (80 if k == "title" else 40):
        return "返回内容过长，疑似把说明当成了名称"
    return None


def clean_entry(rec, res):
    """校验模型对单个节点的返回；失败返回 None（下次会重试）。被拒收的项记入 _issues。"""
    if not isinstance(res, dict):
        return None
    nid = rec["id"]
    def reject(key, reason, v):
        _issues.append({"节点": nid, "项": key, "原因": reason, "返回": str(v)[:60]})

    e = {}
    for field in ("title", "description"):
        v = res.get(field)
        if field in rec and isinstance(v, str) and v.strip():   # 原样返回也记下：表示“已检查，决定保留”，下次不再重复请求
            why = _label_problem(field, v) if field == "title" else None
            if why:
                reject(field, why, v)
                continue
            e[field] = v.strip()
    tip_names = set()
    for sect in ("inputs", "widgets", "outputs"):
        items = rec.get(sect, [])
        names = {x["name"] for x in items if not x.get("skip_name")}
        tip_names |= {x["name"] for x in items if x.get("tooltip")}
        d = res.get(sect)
        if isinstance(d, dict):
            out = {}
            for k, v in d.items():
                if k not in names:
                    continue
                if not isinstance(v, str) or not v.strip():
                    reject(f"{sect}:{k}", "返回的内容为空或不是文字", v)
                    continue
                why = _label_problem(k, v)
                if why:
                    reject(f"{sect}:{k}", why, v)
                    continue
                out[k] = v.strip()
            if out:
                e[sect] = out
    t = res.get("tooltips")
    if isinstance(t, dict) and tip_names:
        out = {k: v.strip() for k, v in t.items() if k in tip_names and isinstance(v, str) and v.strip()}
        if out:
            e["tooltips"] = out
    # 模型什么都没给，却有东西要翻 → 视为失败，下次重试
    return e or None


# ----------------------------------------------------------------- 任务执行
def make_default_chat(cfg, api_base, model):
    """独立版：直接用本文件的 call_api。被框架集成时可传入自己的 chat(messages)->str。"""
    return lambda messages: call_api(cfg, api_base, model, messages)


def _chat_json(chat, glossary, task, payload):
    user = task + json.dumps(payload, ensure_ascii=False)
    raw = chat([{"role": "system", "content": system_prompt(glossary)},
                {"role": "user", "content": user}])
    try:
        return _extract_json(raw)
    except ValueError:
        _bad_raw.append(raw)
        del _bad_raw[:-5]
        raise


def _record_units(rec):
    for field in ("title", "description"):
        if field in rec:
            yield field, rec[field]
    for section in ("inputs", "widgets", "outputs"):
        for item in rec.get(section, []):
            yield section, item


def _record_from_units(rec, units):
    out = {"id": rec["id"], "category": rec["category"]}
    for section, value in units:
        if section in ("title", "description"):
            out[section] = value
        else:
            out.setdefault(section, []).append(value)
    return out


def _split_retry(batch):
    if len(batch) > 1:
        mid = len(batch) // 2
        return batch[:mid], batch[mid:]
    rec = batch[0]
    units = list(_record_units(rec))
    if len(units) > 1:
        mid = len(units) // 2
        return [_record_from_units(rec, units[:mid])], [_record_from_units(rec, units[mid:])]
    return None


def translate_node_batch(batch, chat, glossary, _retries=2):
    """解析失败或完全空跑时按节点/字段继续细分，限制额外请求数。"""
    remaining = _retries * 4
    if not batch:
        return {}

    def run_part(part):
        nonlocal remaining
        try:
            res = _chat_json(chat, glossary, NODE_TASK, part)
        except AuthError:
            raise
        except RuntimeError as e:
            _errors.append(str(e))
            return {}  # 网络、模型配置、限流错误不是拆小就能修复的
        except ValueError as e:
            _errors.append(str(e))
        else:
            out = {}
            for rec in part:
                entry = clean_entry(rec, res.get(rec["id"]))
                if entry:
                    out[rec["id"]] = entry
            if out:
                return out  # 保留这次成功的字段，缺项下次补
            _errors.append(f"节点 {part[0]['id']} 未返回有效译文")
        pieces = _split_retry(part)
        if not pieces:
            return {}
        merged = {}
        for piece in pieces:
            if remaining <= 0:
                break
            remaining -= 1
            for nid, entry in run_part(piece).items():
                merged[nid] = _merge_entry(merged.get(nid), entry)
        return merged

    return run_part(batch)


def translate_flat(items, task, chat, glossary, payload=None, _retries=2):
    """分类/菜单：返回 {原文: 译文}。"""
    try:
        res = _chat_json(chat, glossary, task, payload if payload is not None else items)
    except AuthError:
        raise
    except (ValueError, RuntimeError) as e:
        _errors.append(str(e))
        if len(items) > 1:
            mid = len(items) // 2
            a = translate_flat(items[:mid], task, chat, glossary, payload and payload[:mid])
            a.update(translate_flat(items[mid:], task, chat, glossary, payload and payload[mid:]))
            return a
        if _retries > 0 and isinstance(e, ValueError):
            return translate_flat(items, task, chat, glossary, payload, _retries - 1)
        return {}
    # 模型明确返回原文（品牌名、插件名、快捷键等）也记为完成，否则每次运行都会重复请求它们
    return {k: v.strip() for k, v in res.items()
            if k in set(items) and isinstance(v, str) and v.strip()}


MAX_NODE_UNITS = 24   # 短字段也会产生输出，不能只按输入字符数切分


def _split_large_record(rec, max_chars):
    units = []
    for unit in _record_units(rec):
        chunk = _record_from_units(rec, [*units, unit])
        if units and (len(units) >= MAX_NODE_UNITS or len(json.dumps([chunk], ensure_ascii=False)) > max_chars):
            yield _record_from_units(rec, units)
            units = []
        units.append(unit)
    if units:
        yield _record_from_units(rec, units)


def make_batches(records, max_nodes=6, max_chars=7000):
    batches, cur, size, ids = [], [], 2, set()
    for record in records:
        for rec in _split_large_record(record, max_chars):
            # API 用节点 ID 当 JSON 键，同一个节点的两个分块不能放在同一次请求里。
            n = len(json.dumps(rec, ensure_ascii=False))
            if cur and (len(cur) >= max_nodes or size + 2 + n > max_chars or rec["id"] in ids):
                batches.append(cur)
                cur, size, ids = [], 2, set()
            size += n + (2 if cur else 0)
            cur.append(rec)
            ids.add(rec["id"])
    if cur:
        batches.append(cur)
    return batches


def category_segments(nodes):
    """{片段: [出现过的完整路径(最多2个)]}"""
    segs = {}
    for info in nodes.values():
        for seg in filter(None, (s.strip() for s in info["category"].split("/"))):
            if needs_translation(seg):
                paths = segs.setdefault(seg, [])
                if info["category"] not in paths and len(paths) < 2:
                    paths.append(info["category"])
    return segs


# ----------------------------------------------------------------- 总流程
def run(mappings, display_names, pack_dir, api_base="", model="",
        scope="custom_only", overwrite=False, batch_nodes=6, concurrency=4,
        limit=0, dry_run=False, translate_menus=True, progress=None, log=print,
        chat=None, max_chars=7000, comfy_root=None, translate_brands=False, translate_options=False,
        kinds=ALL_KINDS, translate_categories=True, retry_paused=False, migrate_legacy=True,
        target_only=False):
    """chat: 可选的 chat(messages)->str；不传则用 api_base/model + config.json 的 key。"""
    if chat is None:
        cfg = load_config(pack_dir)
        if not dry_run and not cfg["api_key"]:
            raise ValueError(
                "没有找到 API Key。请设置环境变量 AUTO_TRANSLATE_API_KEY，"
                "或在插件目录的 config.json 里填写 api_key（参考 config.example.json）。")
        chat = make_default_chat(cfg, api_base, model)
    glossary = load_glossary(pack_dir)
    _errors.clear()
    _issues.clear()
    _bad_raw.clear()
    d = tr_dir(pack_dir)
    nodes_path, cats_path, menus_path = (os.path.join(d, n) for n in ("nodes.json", "categories.json", "menus.json"))
    done_nodes, done_cats, done_menus = (_read_json(p) for p in (nodes_path, cats_path, menus_path))
    if not dry_run:
        ensure_override_template(pack_dir)
    ov, ov_err = load_overrides(pack_dir)
    done_cats = {**done_cats, **ov["categories"]}
    done_menus = {**done_menus, **ov["menus"]}

    nodes = collect_nodes(mappings, display_names, scope, limit, comfy_root)
    attempts_path = os.path.join(d, "尝试记录.json")
    attempts = {} if overwrite else _read_json(attempts_path)
    def giveup_for(cls):
        if retry_paused:
            return set()
        return {k for k, n in (attempts.get(cls) or {}).items() if isinstance(n, int) and n >= MAX_TRIES}
    state_path = os.path.join(d, "状态.json")
    run_state = _read_json(state_path)
    scrubbed = {}
    if migrate_legacy and not overwrite and run_state.get("清理版本", 0) < 4:
        scrubbed = scrub_bad_labels(done_nodes)
        if not dry_run:
            with _write_lock:
                if scrubbed:
                    _write_json(nodes_path, done_nodes)
                _write_json(state_path, {**run_state, "清理版本": 4})
    done_view = merge_nodes(done_nodes, ov["nodes"])
    n_scrubbed = sum(len(v) for v in scrubbed.values())
    records = []
    miss_total = {"标题": 0, "说明": 0, "端口/参数名": 0, "悬停提示": 0}
    for cls, info in nodes.items():
        base = ov["nodes"].get(cls) if overwrite else done_view.get(cls)   # 覆盖重翻时只认手动翻译
        rec = make_record(info, base, giveup_for(cls), kinds)
        if rec:
            records.append(rec)
            for k, v in count_missing(rec, base, kinds).items():
                miss_total[k] += v
    # translate_brands：之前被原样保留的分类名（译文==原文）也重新请求，这次要求给出中文名
    segs = {s: p for s, p in (category_segments(nodes).items() if translate_categories else [])
            if overwrite or s not in done_cats or (translate_brands and done_cats.get(s) == s)}
    cat_task = CATEGORY_TASK_ALL if translate_brands else CATEGORY_TASK
    pending = _read_json(os.path.join(d, "menus_pending.json"), []) if translate_menus else []
    pending = [s for s in pending if overwrite or s not in done_menus]

    node_batches = make_batches(records, max_nodes=batch_nodes, max_chars=max_chars)
    seg_list = sorted(segs)
    cat_batches = [seg_list[i:i + 80] for i in range(0, len(seg_list), 80)]
    menu_batches = [pending[i:i + 80] for i in range(0, len(pending), 80)]
    opts_path = os.path.join(d, "options.json")
    done_opts = {**_read_json(opts_path), **ov["options"]}
    opt_ctx = {}
    if translate_options:
        for info in nodes.values():
            for v, w in info.get("opt_values", {}).items():
                context = {"node": info["id"], "widget": w}
                contexts = opt_ctx.setdefault(v, [])
                if len(contexts) < 4 and context not in contexts:
                    contexts.append(context)
    opt_list = sorted(v for v in opt_ctx if overwrite or v not in done_opts) if not target_only else []
    opt_batches = [opt_list[i:i + 80] for i in range(0, len(opt_list), 80)]
    target_options = {}
    if target_only and translate_options:
        # 定向回写限定一个节点类型，避免同名参数的选项键在不同节点间冲突。
        if len(nodes) > 1:
            raise ValueError("定向汉化一次只能选择一种节点")
        for nid, info in nodes.items():
            manual = (ov["nodes"].get(nid) or {}).get("options", {})
            existing = (done_nodes.get(nid) or {}).get("options", {})
            for widget, original in info["editable_options"]:
                k = option_key(widget, original)
                if ((nid, widget) in _FIXED_OPTION_WIDGETS or not is_display_option(original) or k in manual
                        or (not overwrite and (k in existing or original in done_opts))):
                    continue
                target_options[k] = (nid, widget, original)
    option_keys = list(target_options)
    scoped_batches = [option_keys[i:i + 20] for i in range(0, len(option_keys), 20)]
    total_jobs = len(node_batches) + len(cat_batches) + len(menu_batches) + len(opt_batches) + len(scoped_batches)

    n_custom = sum(1 for c in mappings.values() if is_custom_node(c, comfy_root))
    lines = ["本次仅补译指定节点：" + "、".join(nodes)] if target_only else [
        f"ComfyUI 共 {len(mappings)} 个节点：第三方 {n_custom} 个，内置 {len(mappings) - n_custom} 个"]
    if ov_err:
        lines.insert(0, "⚠ " + ov_err)
    if n_scrubbed:
        lines.insert(0, f"升级迁移：清理了 {n_scrubbed} 个旧版留下的无效译文（说明长句，或下划线名被原样记录），已交给模型重新翻译")
    if nodes:
        from collections import Counter
        top = Counter(i["pack"] for i in nodes.values()).most_common(8)
        lines.append("本次范围内的来源分布：" + "、".join(f"{k.replace('custom_nodes.', '')}（{v} 个）" for k, v in top))
    on = [n for k, n in (("title", "名称说明"), ("names", "端口参数名"), ("tips", "悬停提示")) if k in kinds]
    on += [n for flag, n in ((translate_options, "下拉选项"), (translate_categories, "菜单分类"),
                             (translate_brands, "品牌名"), (translate_menus, "右键菜单")) if flag]
    lines.append(f"本次设置：范围={'仅第三方' if scope == 'custom_only' else '全部（含内置）'}；"
                 f"覆盖已有译文={'是' if overwrite else '否'}；翻译项：{'、'.join(on) if on else '（全部关闭）'}")
    if limit and len(nodes) >= limit:
        lines.append(f"⚠ 因为“试跑节点数”设为 {limit}，只处理了前 {limit} 个节点。要处理全部节点，请把它改成 0")
    if nodes:
        lines.append("范围内前几个节点：" + "、".join(list(nodes)[:5]))
    lines += [
        f"扫描到 {len(nodes)} 个节点；本次安排补译 {len(records)} 个节点："
        f"标题 {miss_total['标题']}、说明 {miss_total['说明']}、端口/参数名 {miss_total['端口/参数名']}、悬停提示 {miss_total['悬停提示']}",
        f"分类名片段 {len(seg_list)} 个，待翻译的菜单文字 {len(pending)} 条，下拉选项 {len(opt_list) + len(option_keys)} 个；计划 {total_jobs} 批（不含重试）",
    ]
    if dry_run:
        chars = sum(len(json.dumps(r, ensure_ascii=False)) for r in records)
        lines.append(f"节点数据约 {chars} 字符。【预览模式】未调用 API，也未写入文件。")
        return "\n".join(lines)
    if total_jobs == 0:
        lines.append("本次没有安排 API 请求；下方仍会列出已暂停的缺项。")

    jobs = []
    pool = ThreadPoolExecutor(max_workers=max(1, int(concurrency)))
    for b in node_batches:
        jobs.append(("nodes", b, pool.submit(translate_node_batch, b, chat, glossary)))
    for b in cat_batches:
        payload = [{"segment": s, "paths": segs[s]} for s in b]
        jobs.append(("cats", b, pool.submit(translate_flat, b, cat_task, chat, glossary, payload)))
    for b in menu_batches:
        jobs.append(("menus", b, pool.submit(translate_flat, b, MENU_TASK, chat, glossary)))
    for b in opt_batches:
        payload = [{"option": v, "contexts": opt_ctx[v]} for v in b]
        jobs.append(("opts", b, pool.submit(translate_flat, b, OPTION_TASK, chat, glossary, payload)))
    for b in scoped_batches:
        payload = [{"id": k, "node": target_options[k][0], "widget": target_options[k][1], "option": target_options[k][2]} for k in b]
        task = "逐项翻译这个节点的下拉选项。返回 JSON 对象，键必须是每条 id 原文，值为简洁的显示译文。模型名、算法名、编码预设或技术标识保留原文。\n"
        jobs.append(("scoped_opts", b, pool.submit(translate_flat, b, task, chat, glossary, payload)))
    future_info = {f: (kind, b) for kind, b, f in jobs}

    n_cats = n_menus = n_opts = 0
    written_nodes, failed_nodes = set(), set()
    flat_missing = {"分类名": [], "菜单文字": [], "下拉选项": []}
    new_pending_left = set(pending)
    try:
        for i, fut in enumerate(as_completed(future_info), 1):
            kind, b = future_info[fut]
            try:
                res = fut.result()
            except AuthError:
                raise
            except Exception as e:
                log(f"[节点汉化] 一个批次失败: {e}")
                res = {}
            with _write_lock:  # 每个批次完成就落盘，中途中断也不丢
                if kind == "nodes":
                    att = _read_json(attempts_path)
                    for rec in b:
                        got = entry_keys(res.get(rec["id"]))
                        rejected = {x["项"] for x in _issues if x["节点"] == rec["id"]}
                        expected = rec_keys(rec, None, kinds)
                        if expected - got:
                            failed_nodes.add(rec["id"])
                        for k in expected - got - rejected:
                            if rec["id"] in res:
                                _issues.append({"节点": rec["id"], "项": k, "原因": "模型的返回里漏掉了这一项", "返回": ""})
                            else:
                                _issues.append({"节点": rec["id"], "项": k, "原因": "所在请求没有返回有效译文（详见请求错误）", "返回": ""})
                        for k in expected - got:
                            a = att.setdefault(rec["id"], {})
                            a[k] = (0 if overwrite else a.get(k, 0)) + 1
                        for k in expected & got:
                            (att.get(rec["id"]) or {}).pop(k, None)
                    if res:
                        cur = _read_json(nodes_path)
                        for k, v in res.items():
                            cur[k] = _merge_entry(cur.get(k), v, overwrite=overwrite)
                        _write_json(nodes_path, cur)
                        written_nodes.update(res)
                    _write_json(attempts_path, att)
                elif kind == "scoped_opts":
                    res = {k: v for k, v in res.items() if len(v) <= 80 and "\n" not in v}
                    flat_missing["下拉选项"] += [target_options[x][2] for x in b if x not in res]
                    if res:
                        cur = _read_json(nodes_path)
                        for k, v in res.items():
                            nid = target_options[k][0]
                            cur[nid] = _merge_entry(cur.get(nid), {"options": {k: v}}, overwrite=overwrite)
                        _write_json(nodes_path, cur)
                    n_opts += len(res)
                elif kind == "opts":
                    flat_missing["下拉选项"] += [x for x in b if x not in res]
                    cur = _read_json(opts_path)
                    cur.update(res)
                    _write_json(opts_path, cur)
                    n_opts += len(res)
                elif kind == "cats":
                    flat_missing["分类名"] += [x for x in b if x not in res]
                    cur = _read_json(cats_path)
                    cur.update(res)
                    _write_json(cats_path, cur)
                    n_cats += len(res)
                else:
                    flat_missing["菜单文字"] += [x for x in b if x not in res]
                    cur = _read_json(menus_path)
                    cur.update(res)
                    _write_json(menus_path, cur)
                    n_menus += len(res)
                    new_pending_left -= set(res)
            what = {"nodes": "节点", "cats": "分类名", "menus": "菜单文字", "opts": "下拉选项", "scoped_opts": "指定节点下拉选项"}[kind]
            names = "、".join(str(x.get("id", x) if isinstance(x, dict) else x) for x in b[:3]) + ("…" if len(b) > 3 else "")
            log(f"[节点汉化] 进度 {i}/{total_jobs}：已完成{what}翻译（{names}）")
            if progress:
                progress(i, total_jobs)
    except BaseException:
        for f in future_info:
            f.cancel()
        raise
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    if translate_menus:
        _write_json(os.path.join(d, "menus_pending.json"), sorted(new_pending_left))

    final = load_translations(pack_dir)["nodes"]
    left = {}
    for cls, info in nodes.items():
        rec = make_record(info, final.get(cls), kinds=kinds)
        if rec:
            left[cls] = rec
    att_now = _read_json(attempts_path)
    # 逐项归因：现在仍然缺的项，对应本次运行里记录的原因
    why_of = {(x["节点"], x["项"]): x for x in _issues}
    by_reason, gave = {}, 0
    for cls, rec in left.items():
        for k in rec_keys(rec, final.get(cls), kinds):
            x = why_of.get((cls, k))
            paused = (att_now.get(cls) or {}).get(k, 0) >= MAX_TRIES
            gave += int(paused)
            if x:
                reason, got = x["原因"] + ("；已暂停自动重试" if paused else ""), x["返回"]
            elif paused:
                reason, got = f"连续 {MAX_TRIES} 次都没拿到译文，已暂停自动重试", ""
            else:
                reason, got = "本次未取得该项译文，尚可重试", ""
            by_reason.setdefault(reason, []).append((cls, k, got))
    kept = [(cls, sect, n) for cls, e in final.items() if cls in nodes and isinstance(e, dict)
            for sect in ("inputs", "widgets", "outputs") if isinstance(e.get(sect), dict)
            for n, v in e[sect].items() if v == n]
    if left:
        lines.append(f"完整性检查：仍有 {len(left)} 个节点存在 {sum(len(v) for v in by_reason.values())} 处缺项，原因如下：")
        for reason, items in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
            ex = "；".join(f"{c} 的 {k}" + (f"（返回“{g}”）" if g else "") for c, k, g in items[:3])
            lines.append(f"  · {reason}：{len(items)} 处。例如：{ex}")
        if gave:
            lines.append(f"  已暂停 {gave} 处；普通再运行会跳过。可按目标节点补译，或开启「重试已暂停的缺项」。")
        lines.append("  未暂停的缺项可在下次增量运行时继续补译。" + ("" if target_only else "完整清单见运行日志.txt"))
    else:
        lines.append(f"完整性检查：范围内 {len(nodes)} 个节点的标题、说明、端口/参数名、悬停提示全部齐全")
    if kept:
        lines.append(f"另有 {len(kept)} 个端口/参数名是模型判断“无需翻译”而保留原文（如 VAE、seed 这类通用词），例如：" +
                     "、".join(f"{c}.{n}" for c, _, n in kept[:3]))
    lines.append(f"本次写入：节点 {len(written_nodes)} 个、分类 {n_cats} 个、菜单文字 {n_menus} 条、下拉选项 {n_opts} 个")
    failed_cats = max(0, len(seg_list) - n_cats)
    failed_menus = max(0, len(pending) - n_menus)
    failed_opts = max(0, len(opt_list) + len(option_keys) - n_opts)
    has_failures = bool(failed_nodes or failed_cats or failed_menus or failed_opts)
    if failed_nodes:
        lines.append(f"有 {len(failed_nodes)} 个节点本次只拿到部分译文或没有拿到译文；已有译文保留。")
    last_err = f"最近一次错误：{_errors[-1][:120]}" if _errors else "没有报错，模型的返回里漏掉了这些项"
    for what, miss in flat_missing.items():
        if miss:
            lines.append(f"有 {len(miss)} 个{what}没拿到译文：{'、'.join(miss[:5])}{'…' if len(miss) > 5 else ''}。"
                         f"原因：{last_err}。再运行一次会重试")
    if _errors:
        if has_failures:
            lines.append(f"失败原因（示例）：{_errors[0][:200]}")
        else:
            lines.append(f"过程中有 {len(_errors)} 次请求或解析异常，已自动重试并恢复（示例：{_errors[0][:80]}）")
    if _bad_raw and has_failures and not target_only:
        log_path = os.path.join(d, "bad_responses.log")
        try:
            with open(log_path, "w", encoding="utf-8") as f:
                for i, raw in enumerate(_bad_raw, 1):
                    f.write(f"===== 解析失败的原始返回 {i}/{len(_bad_raw)} =====\n{raw}\n\n")
            lines.append(f"解析失败的原始返回已保存到：{log_path}（需要排查时把它发给我）")
        except OSError:
            pass
    if target_only:
        return "\n".join(lines)
    try:
        import datetime
        with open(os.path.join(d, "运行日志.txt"), "w", encoding="utf-8") as f:
            f.write(f"节点汉化 运行日志  {datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n\n")
            f.write("\n".join(lines) + "\n\n")
            f.write(f"===== 仍然缺项的完整清单（{sum(len(v) for v in by_reason.values())} 处）=====\n")
            for reason, items in by_reason.items():
                f.write(f"\n【{reason}】{len(items)} 处\n")
                for c, k, g in items:
                    f.write(f"  {c}  {k}" + (f"  返回：{g}" if g else "") + "\n")
            f.write(f"\n===== 模型判断无需翻译而保留原文的项（{len(kept)} 个）=====\n")
            for c, sect, n in kept:
                f.write(f"  {c}  {sect}:{n}\n")
            f.write(f"\n===== 本次出现过的请求/解析错误（{len(_errors)} 次）=====\n")
            for e in _errors:
                f.write("  " + e.replace("\n", " ")[:300] + "\n")
    except OSError:
        pass
    lines.append(f"译文文件：{d}")
    lines.append(f"完整运行日志：{os.path.join(d, '运行日志.txt')}")
    lines.append(f"想自己改译文：编辑 {override_path(pack_dir)}（优先级最高，重翻不会覆盖）")
    lines.append("刷新网页(F5)后生效。")
    return "\n".join(lines)
