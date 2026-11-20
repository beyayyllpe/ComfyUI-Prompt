"""
node_localizer.py — 节点汉化（AI）

运行一次：扫描已安装的节点 → 用你在 ai_prompt_keys.json 里配好的服务商，按节点用途意译成中文
→ 存进插件目录的 translations/zh-CN/ ；配套的 web/autotranslate_*.js 负责把译文显示到界面上。

· 服务商、Key、代理、关闭思考模式 全部复用 _shared.py，不需要另外配置 Key
· 核心逻辑在 _translate_core.py（下划线开头，不会被当成节点）
· 译文文件可以直接用记事本改，改完刷新网页生效
"""
import asyncio
import os
import threading
import time

import _translate_core as core
from _shared import get_all_providers, load_key, call_ai, advice

PACK_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 插件根目录（ai_prompt_keys.json 所在处）
_AI_RUN_LOCK = threading.Lock()


def _guarded_run(**kwargs):
    if not _AI_RUN_LOCK.acquire(blocking=False):
        raise RuntimeError("节点汉化正在运行，请等待当前任务结束")
    try:
        return core.run(**kwargs)
    finally:
        _AI_RUN_LOCK.release()


def _target_definition(node_id):
    import nodes as comfy_nodes
    if not isinstance(node_id, str) or node_id not in comfy_nodes.NODE_CLASS_MAPPINGS:
        raise ValueError("此节点没有可扫描的后端定义（例如前端工具或子图外壳）；请选一个实际节点")
    mapping = {node_id: comfy_nodes.NODE_CLASS_MAPPINGS[node_id]}
    info = core.collect_nodes(mapping, comfy_nodes.NODE_DISPLAY_NAME_MAPPINGS, scope="all")[node_id]
    return info, mapping, comfy_nodes.NODE_DISPLAY_NAME_MAPPINGS


# ─── 给前端用的接口 ────────────────────────────────────────────────────────────
try:
    from aiohttp import web
    from server import PromptServer

    _routes = PromptServer.instance.routes

    @_routes.get("/autotranslate/data")
    async def _get_data(request):
        core.ensure_override_template(PACK_DIR)   # 首次生成 手动翻译.json 模板
        return web.json_response(core.load_translations(PACK_DIR))

    @_routes.post("/autotranslate/collect")
    async def _collect(request):
        try:
            body = await request.json()
            items = body.get("items", [])
            if not isinstance(items, list):
                raise ValueError
            n = core.add_pending_menus(PACK_DIR, items[:60])
        except Exception:
            return web.json_response({"ok": False}, status=400)
        return web.json_response({"ok": True, "pending": n})

    @_routes.get("/autotranslate/target")
    async def _target_fields(request):
        try:
            info, _, _ = await asyncio.to_thread(_target_definition, request.query.get("id"))
            data = core.load_translations(PACK_DIR)
            entry = data["nodes"].get(info["id"], {})
            manual = data["manual_nodes"].get(info["id"], {})
            fields = core.node_translation_fields(info, entry, data["options"])
            for field in fields:
                stored = manual.get(field["section"])
                field["manual"] = bool(stored.get(field["key"])) if isinstance(stored, dict) else bool(stored)
            return web.json_response({"fields": fields})
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)

    @_routes.post("/autotranslate/target")
    async def _target_action(request):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("请求格式无效")
            info, mappings, display_names = await asyncio.to_thread(_target_definition, body.get("id"))
            if body.get("action") == "manual":
                count = await asyncio.to_thread(core.save_node_overrides, PACK_DIR, info, body.get("edits"))
                return web.json_response({"message": f"已保存 {count} 条手动译文。"})
            if body.get("action") != "ai":
                raise ValueError("未知操作")
            provider, model, proxy = (body.get(k, "") for k in ("provider", "model", "proxy"))
            if not all(isinstance(v, str) for v in (provider, model, proxy)) or provider not in _provider_names():
                raise ValueError("请先在节点汉化上选择有效的服务商和模型")
            model = _explicit_model(model)
            key = load_key(provider)
            if not key:
                raise ValueError(f"没有找到 {provider} 的 API Key")
            kinds = {k for k in core.ALL_KINDS if body.get(k) is True}
            translate_options = body.get("options") is True
            if not kinds and not translate_options:
                raise ValueError("请至少开启一项翻译内容")
            report = await asyncio.to_thread(
                _guarded_run, mappings=mappings, display_names=display_names, pack_dir=PACK_DIR,
                scope="all", retry_paused=True, translate_categories=False, translate_menus=False,
                migrate_legacy=False, target_only=True, translate_options=translate_options,
                overwrite=body.get("overwrite") is True,
                kinds=kinds, concurrency=1, max_chars=4000,
                chat=_make_chat(provider, model, key, proxy),
            )
            return web.json_response({"message": report})
        except (ValueError, RuntimeError) as e:
            return web.json_response({"error": str(e)}, status=400)
except Exception as _e:  # 单独测试或 ComfyUI 版本差异时不要让整个插件崩
    print(f"[节点汉化] 未能注册接口: {_e}")


# ─── AI 调用适配：把 _shared.call_ai 包成核心需要的 chat(messages) -> str ──────
def _make_chat(provider, model, key, proxy):
    def chat(messages):
        system, user = messages[0]["content"], messages[1]["content"]
        last = None
        for attempt in range(3):
            try:
                text, _used, _usage = call_ai(provider, model, key, system, user, 0.3,
                                              proxy=proxy, thinking=False, timeout_s=180)
                return text
            except Exception as e:
                msg = str(e)
                hint = advice(msg)
                full = msg + ("\n  → " + hint if hint else "")
                # 密钥错误 / 没权限 / 余额不足：重试没有意义，直接终止整个任务
                if any(c in msg for c in ("HTTP 401", "HTTP 402", "HTTP 403")):
                    raise core.AuthError(full)
                # 参数/模型名错误：同样不用重试
                if "HTTP 400" in msg or "HTTP 404" in msg:
                    raise RuntimeError(full)
                last = full
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(last or "调用失败")
    return chat


def _provider_names():
    # 腾讯/百度翻译是机器翻译，不是大模型，这里不提供
    return [n for n, c in get_all_providers().items() if c.get("style") != "mt"]


LEGACY_AUTO_MODEL = "自动（推荐模型）"


def _explicit_model(model):
    if not isinstance(model, str) or not model.strip() or model.strip() == LEGACY_AUTO_MODEL:
        raise ValueError("请先点击「获取模型列表」并选择一个模型")
    return model.strip()


class NodeLocalizerAI:

    @classmethod
    def INPUT_TYPES(cls):
        providers = _provider_names()
        return {
            "required": {
                "服务商": (providers if providers else ["（请重启ComfyUI）"], {
                    "default": "DeepSeek" if "DeepSeek" in providers else (providers[0] if providers else ""),
                    "tooltip": "列表和 Key 都来自 ai_prompt_keys.json，和提示词翻译节点共用",
                }),
                "模型": ([""], {
                    "default": "",
                    "tooltip": "先选服务商，再点击上方的“获取模型列表”并选择模型。运行 AI 翻译前必须选择模型。",
                }),
                "范围": (["仅第三方节点", "全部节点（含内置）"], {
                    "default": "仅第三方节点",
                    "tooltip": "内置节点 ComfyUI 官方已有中文，一般不用翻",
                }),
                "翻译节点名称和说明": ("BOOLEAN", {
                    "default": True, "label_on": "是", "label_off": "否",
                    "tooltip": '节点的显示名称和节点说明',
                }),
                "翻译端口和参数名": ("BOOLEAN", {
                    "default": True, "label_on": "是", "label_off": "否",
                    "tooltip": '节点上的输入口、输出口和参数名（如 crop_scale→裁剪比例）',
                }),
                "翻译悬停提示": ("BOOLEAN", {
                    "default": True, "label_on": "是", "label_off": "否",
                    "tooltip": '鼠标悬停在端口、参数上显示的提示文字',
                }),
                "翻译下拉选项": ("BOOLEAN", {
                    "default": True, "label_on": "是", "label_off": "否",
                    "tooltip": '下拉框里的选项（如 scale by multiplier）。只改显示，实际保存和使用的值仍是英文原值，不影响运行；文件名、路径类选项不动',
                }),
                "翻译添加节点菜单分类": ("BOOLEAN", {
                    "default": True, "label_on": "是", "label_off": "否",
                    "tooltip": '“添加节点”菜单里的各级分类名',
                }),
                "品牌名也汉化": ("BOOLEAN", {
                    "default": False, "label_on": "是", "label_off": "否",
                    "tooltip": '默认保留品牌/插件名的英文（如 KJNodes）。开启后分类名也尽量译成中文，之前被原样保留的会重新翻译',
                }),
                "翻译右键菜单文字": ("BOOLEAN", {
                    "default": True, "label_on": "是", "label_off": "否",
                    "tooltip": '前端会自动收集右键菜单里还没有译文的英文（需要先右键点开过那些菜单），开启后一并翻译',
                }),
                "覆盖已有译文": ("BOOLEAN", {
                    "default": False, "label_on": "是", "label_off": "否",
                    "tooltip": '关：只补缺项，你手改过的、已翻好的不动\n开：全部重新翻译（手动翻译.json 里写的始终不会被覆盖）',
                }),
                "只预览不翻译": ("BOOLEAN", {
                    "default": False, "label_on": "是", "label_off": "否",
                    "tooltip": '只统计有多少缺项，不调用 API，不写文件。第一次建议先预览',
                }),
            },
            "optional": {
                "每批节点数": ("INT", {
                    "default": 4, "min": 1, "max": 30, "advanced": True,
                    "tooltip": "每次请求带几个节点。输出被截断时会自动拆小重试",
                }),
                "并发数": ("INT", {
                    "default": 4, "min": 1, "max": 16, "advanced": True,
                    "tooltip": "同时发几个请求；遇到限流就调小",
                }),
                "代理地址": ("STRING", {
                    "default": "", "advanced": True,
                    "placeholder": "海外服务商用，如 127.0.0.1:7890；留空用系统代理",
                    "tooltip": "DeepSeek、通义千问始终直连，不受此影响",
                }),
                "重试已暂停的缺项": ("BOOLEAN", {
                    "default": False, "advanced": True,
                    "tooltip": "本次重新尝试超过重试次数的缺项；不需要开启覆盖已有译文。",
                }),
            },
        }

    @classmethod
    def VALIDATE_INPUTS(cls, 模型):
        return True  # 模型列表是前端按服务商动态拉取的，不能用固定列表校验

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("报告",)
    FUNCTION = "run"
    OUTPUT_NODE = True
    CATEGORY = "自定义/界面汉化"

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")  # 每次点运行都重新执行

    def run(self, 服务商, 模型, 范围, 翻译节点名称和说明, 翻译端口和参数名, 翻译悬停提示, 翻译下拉选项,
            翻译添加节点菜单分类, 品牌名也汉化, 翻译右键菜单文字, 覆盖已有译文, 只预览不翻译,
            每批节点数=4, 并发数=4, 代理地址="", 重试已暂停的缺项=False):
        import nodes as comfy_nodes

        key = ""
        chosen_model = ""
        if not 只预览不翻译:
            chosen_model = _explicit_model(模型)
            key = load_key(服务商)
            if not key:
                raise ValueError(f"没有找到 {服务商} 的 API Key，请在 ai_prompt_keys.json 里填写，改完重启")

        progress = None
        try:
            from comfy.utils import ProgressBar
            holder = {}

            def progress(i, total):
                if "bar" not in holder:
                    holder["bar"] = ProgressBar(total)
                holder["bar"].update_absolute(i, total)
        except Exception:
            pass

        report = _guarded_run(
            mappings=comfy_nodes.NODE_CLASS_MAPPINGS,
            display_names=comfy_nodes.NODE_DISPLAY_NAME_MAPPINGS,
            pack_dir=PACK_DIR,
            scope="all" if 范围.startswith("全部") else "custom_only",
            overwrite=覆盖已有译文,
            batch_nodes=每批节点数,
            concurrency=并发数,
            dry_run=只预览不翻译,
            retry_paused=重试已暂停的缺项,
            translate_menus=翻译右键菜单文字,
            translate_brands=品牌名也汉化,
            translate_options=翻译下拉选项,
            translate_categories=翻译添加节点菜单分类,
            kinds={k for k, on in (("title", 翻译节点名称和说明), ("names", 翻译端口和参数名), ("tips", 翻译悬停提示)) if on},
            progress=progress,
            chat=_make_chat(服务商, chosen_model, key, 代理地址),
            max_chars=4000,  # 控制单次输出体量，避免超出服务商默认的输出上限
            comfy_root=os.path.dirname(os.path.abspath(comfy_nodes.__file__)) if getattr(comfy_nodes, "__file__", None) else None,
            log=lambda s: print(s, flush=True),
        )
        print("[节点汉化]\n" + report, flush=True)
        return {"ui": {"text": [report]}, "result": (report,)}


NODE_CLASS_MAPPINGS = {"NodeLocalizerAI": NodeLocalizerAI}
NODE_DISPLAY_NAME_MAPPINGS = {"NodeLocalizerAI": "🌐 节点汉化（AI）"}
