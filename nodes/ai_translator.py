"""
✨ 提示词翻译 / 扩写节点
菜单：自定义 → 高级提示词 → 提示词翻译

· 按目标底模架构输出对应格式（中文 / 英文句子 / 英文标签 / 负面词）
· 扩写程度可选（不扩写 / 简洁 / 适中 / 详细）
· 支持图像逆推（接入图像后 AI 直接描述图像生成提示词）
· 服务商由 ai_prompt_keys.json 驱动；系统指令由 ai_prompt_templates.json 驱动
· 公共能力（调用、日志、Key、模型列表）来自 _shared.py
"""

import os, re, json, asyncio, urllib.request, urllib.error

from _shared import (
    BUILTIN_PROVIDERS, THINKING_PARAMS, KEYS_FILE, TEMPLATES_FILE, ERROR_LOG,
    Log, HAS_CN, advice, write_error_log,
    ensure_keys_file, get_all_providers, _provider_names, load_key, load_mt_creds,
    call_ai, fetch_models, pick_fallback, _fallback_cache,
    img_to_b64, clean_text, parse_zh_en, join_parts, _open_direct,
)

# ─── 模式 & 扩写程度 ────────────────────────────────────────────────────────────
MODE_ZH    = "中文（Qwen/ZIT/通用）"
MODE_EN_NL = "英文句子（Krea2/FLUX）"
MODE_EN_TAG= "英文标签（SDXL/Pony）"
MODE_EDIT_EN = "编辑指令（英文）"
MODE_EDIT_ZH = "编辑指令（中文）"
MODE_NEG   = "负面词（英文）"
MODE_PASS  = "不处理"
MODES = [MODE_ZH, MODE_EN_NL, MODE_EN_TAG,
         MODE_EDIT_EN, MODE_EDIT_ZH, MODE_NEG, MODE_PASS]
EDIT_MODES = (MODE_EDIT_EN, MODE_EDIT_ZH)
MAX_IMAGES = 10

EXP_NONE   = "不扩写"
EXP_BRIEF  = "简洁"
EXP_MID    = "适中"
EXP_DETAIL = "详细"
EXPANSIONS = [EXP_NONE, EXP_BRIEF, EXP_MID, EXP_DETAIL]

# ─── 系统提示词 ─────────────────────────────────────────────────────────────────
# ─── 默认指令模板（可被 ai_prompt_templates.json 覆盖）─────────────────────────
DEFAULT_TEMPLATES = {
    "硬性规则": (
        "硬性规则：\n"
        "- 只输出提示词本身，不要解释、不要客套、不要 markdown 代码块。\n"
        "- 用户原文里已有的英文、专有名词、LoRA 触发词原样保留。\n"
        "- (word:1.2)、<sks>、<lora:> 等特殊写法原样保留。"
    ),
    "扩写核心": (
        "你是资深 AI 绘画提示词设计师，将简短描述扩写成画面完整的提示词。\n"
        "扩写原则：\n"
        "1. 紧扣用户原意，不替换或弱化用户写到的内容。\n"
        "2. 把抽象词转成具体视觉细节（服装款式、光线方向、镜头焦段等）。\n"
        "3. 只补用户没有指定的维度，给出一种最协调的具体设定。\n"
        "4. 全篇只有一种明确的视觉风格。\n"
        "5. 按顺序：主体→外貌→服装→动作→场景→光线→镜头→风格与质感。"
    ),
    "篇幅_简洁": "篇幅：中文控制在 40~60 字左右，只补最关键的细节。",
    "篇幅_适中": "篇幅：中文控制在 80~120 字左右。",
    "篇幅_详细": "篇幅：中文控制在 150~220 字左右，细节完整但不啰嗦。",
    "输出_中文": "输出：中文自然语言，逗号连接短句。",
    "输出_英文句子": "英文版要求：把中文扩写结果转成流畅具体的英文自然语言描述（3~5 句），保留全部视觉细节，不要逐字硬译。",
    "输出_英文标签": "英文版要求：把中文扩写结果转成 SDXL 习惯的英文标签，短语用逗号分隔，每个短语 1~4 个词，使用 SD 社区通用标签，总长度 40~80 词。",
    "JSON格式要求": ' 输出格式：只输出一个 JSON 对象，不要代码块，形如 {"zh": "中文扩写内容", "en": "英文内容"}',
    "不扩写_中文": "你是 AI 绘画提示词整理师。将用户的描述整理成流畅规范的中文提示词，保持原意，修正语法，不增加内容，用逗号连接短句。",
    "不扩写_英文句子": "You are an AI image prompt translator. Translate the user description into natural English sentences for AI image generation. Do NOT add content beyond what the user specified. Output English only.",
    "不扩写_英文标签": "You are an AI image prompt translator. Translate the user description into English comma-separated tags for SDXL, using standard Stable Diffusion community tags. Do NOT add content beyond what the user specified. Output English only.",
    "负面词": "你是 AI 绘画提示词工程师。用户描述的是画面中不想要的内容。转写为英文负面提示词，逗号分隔短语，只补充与用户意图直接相关的负面词，不堆砌无关的通用词。Output English only.",
    "图像_中文": (
        "你是 AI 绘画提示词提取师。分析图片的全部视觉特征，生成能复现这张画面的中文提示词。\n"
        "必须逐项检查并写出（图中有就写，没有就跳过，不要编造）：\n"
        "1 主体：性别、年龄感、族裔特征、脸型五官特点\n"
        "2 发型：长度、发色、造型方式、发饰\n"
        "3 妆容与面部装饰：花钿、贴纸、痣、彩绘\n"
        "4 服装：款式、材质（金属/丝绸/纱/皮革）、颜色、纹样、露出部位\n"
        "5 配饰：头饰、耳饰、项链、手镯、纹身、持物\n"
        "6 姿态：身体朝向、头部角度、视线方向、手部动作、表情\n"
        "7 景别与构图：特写/半身/全身、竖构图或横构图、主体在画面中的位置\n"
        "8 镜头：视角高低、焦段感、景深虚化程度\n"
        "9 光线：主光方向、侧逆光或顺光、光比、色温\n"
        "10 配色：主色调、对比色\n"
        "11 背景：场景内容、虚化程度、光斑或粒子等元素\n"
        "12 风格与质感：写实/CG/插画/摄影、渲染质感、皮肤与材质细节\n"
        "只描述图中真实存在、肉眼可见的内容，看不见的部分（如被遮挡的下半身、鞋子）一律不写。\n"
        "输出一整段，用逗号连接短句，不要编号、不要小标题、不要解释。"
    ),
    "图像_英文句子": (
        "You are an AI image prompt extractor. Analyze all visual features and write an English natural "
        "language prompt that could recreate this image.\n"
        "Systematically cover (skip anything not visible, never invent): subject (gender, age, ethnicity, "
        "facial features); hair (length, color, styling, hair accessories); makeup and face decorations; "
        "clothing (style, material, color, patterns, exposed areas); accessories (headpiece, earrings, "
        "necklace, bracelets, tattoos, held objects); pose (body orientation, head angle, gaze direction, "
        "hand gestures, expression); shot type and composition; camera angle and depth of field; lighting "
        "(direction, rim/side/front light, contrast, color temperature); color palette; background elements "
        "and blur; art style and rendering quality.\n"
        "Describe only what is actually visible. Output English only, no explanations, no negative prompts."
    ),
    "图像_英文标签": (
        "You are an AI image prompt extractor. Analyze all visual features and generate a standard AI art "
        "positive prompt as English comma-separated tags.\n"
        "Systematically extract (skip anything not visible, never invent): subject and facial features; hair "
        "and hair accessories; makeup and face decorations; clothing style, material, color and patterns; "
        "accessories, jewelry, tattoos, held objects; pose, body orientation, gaze direction, hand gestures, "
        "expression; shot type and composition; camera angle and depth of field; lighting direction and "
        "color temperature; color palette; background elements; art style, rendering type and image quality.\n"
        "Arrange in order: subject, hair and face, clothing, accessories, pose and gaze, composition and "
        "camera, lighting and color, background, style and quality.\n"
        "Use standard Stable Diffusion community tags (1girl, looking at viewer, upper body, rim light, "
        "bokeh, highly detailed). Describe only what is actually visible.\n"
        "Output the positive prompt only, no explanations, no negative prompts."
    ),
    "图像_负面词": "You are an AI image prompt engineer. Analyze the image, identify visual defects or undesirable elements, and write them as English negative prompt tags separated by commas. Output English only.",
    "图像详细度_不扩写": "详细程度：只写最核心的信息——主体、服装主要特征、景别、整体风格，控制在 15~25 个词或 30~50 字。",
    "图像详细度_简洁": "详细程度：写主要特征即可，覆盖主体、发型、服装、姿态、光线、风格，控制在 25~45 个词或 50~90 字。",
    "图像详细度_适中": "详细程度：覆盖上述全部检查项的主要内容，每项用 1~2 个短语描述，控制在 50~80 个词或 100~160 字。",
    "图像详细度_详细": "详细程度：逐项展开上述全部检查项，材质、纹样、光线层次、背景元素都要写清楚，控制在 80~130 个词或 180~280 字。宁可细也不要漏，但仍然只写图中真实可见的内容。",
    "编辑_英文": (
        "You are an expert at writing instructions for image editing models (Qwen-Image-Edit, FLUX Kontext). "
        "The user describes what they want to change, usually referring to the pictures as 图1/图2 (Picture 1/Picture 2).\n"
        "Look at every picture carefully, then write ONE clear editing instruction in English:\n"
        "1. Refer to pictures as \"Picture 1\", \"Picture 2\" and so on, matching the given order.\n"
        "2. State exactly what to change, and describe the transferred elements concretely by what you see "
        "(style, material, color, pattern, accessories), not vaguely like \"the clothes in Picture 1\".\n"
        "3. State explicitly what must stay unchanged (face, identity, hairstyle, pose, background, lighting) unless the user wants it changed.\n"
        "4. Keep it one coherent instruction, imperative voice. No explanations, no bullet points.\n"
        "Output English only."
    ),
    "编辑_中文": (
        "你是图像编辑模型（Qwen-Image-Edit、FLUX Kontext 等）的指令撰写专家。用户会描述想怎么改图，通常用 图1、图2 指代图片。\n"
        "仔细看每一张图，然后写出一条清晰的中文编辑指令：\n"
        "1. 用\"图1\"\"图2\"等称呼图片，与给出的顺序一致。\n"
        "2. 明确要改什么，要迁移的元素按你看到的具体写出来（款式、材质、颜色、纹样、配饰），不要笼统写成\"图1的衣服\"。\n"
        "3. 明确写出必须保持不变的部分（脸、身份特征、发型、姿势、背景、光线），除非用户要改它们。\n"
        "4. 写成一条连贯的祈使句指令，不要解释、不要分点。"
    ),
    "编辑详细度_不扩写": "详细程度：只做必要的规范化，一两句话说清改什么、保留什么，不展开描述细节。",
    "编辑详细度_简洁": "详细程度：简要说明要改的内容和关键特征，保留部分只列最重要的一两项。30~50 词或 50~80 字。",
    "编辑详细度_适中": "详细程度：写清要迁移元素的主要特征（款式、颜色、材质），并列出需要保留的部分。50~90 词或 80~150 字。",
    "编辑详细度_详细": "详细程度：逐项展开要迁移元素的细节（款式、材质、颜色、纹样、配饰、位置关系），完整列出需要保留的部分，并说明融合要自然（光线、比例、透视一致）。90~150 词或 150~250 字。",
    "多图融合": "本次提供了多张参考图，按顺序称为图1、图2……。请按用户文字要求，从各图中提取对应元素，组合成描述**一个统一画面**的提示词；用户没有说明分工时，以图1为主体，其他图作为补充参考（服装、风格、场景等）。不要把多张图分别描述。",
    "随机提示": "补充：请给出与上次不同的具体场景、光线、构图设定，紧扣原意但换一种视觉方案。",
    "英文重试强化": "IMPORTANT: Your previous answer contained Chinese characters. Output MUST be in English ONLY. Do not include any Chinese character.",
}


def ensure_templates_file():
    """生成/补全指令模板文件；用户改坏或删除时自动用内置默认值"""
    data = {}
    if os.path.isfile(TEMPLATES_FILE):
        try:
            data = json.load(open(TEMPLATES_FILE, encoding="utf-8"))
        except Exception:
            data = {}
    changed = not os.path.isfile(TEMPLATES_FILE)
    for k, v in DEFAULT_TEMPLATES.items():
        if k not in data:
            data[k] = v
            changed = True
    if changed:
        try:
            with open(TEMPLATES_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


def load_templates():
    """每次调用都重新读，改完文件不用重启 ComfyUI"""
    tpl = dict(DEFAULT_TEMPLATES)
    try:
        user = json.load(open(TEMPLATES_FILE, encoding="utf-8"))
        for k, v in user.items():
            if isinstance(v, str) and v.strip():
                tpl[k] = v
    except Exception:
        pass
    return tpl


_EXP_KEY = {EXP_BRIEF: "篇幅_简洁", EXP_MID: "篇幅_适中", EXP_DETAIL: "篇幅_详细"}
_MODE_KEY = {MODE_ZH: "中文", MODE_EN_NL: "英文句子", MODE_EN_TAG: "英文标签",
             }


def _build_system(mode, expansion, style_hint, extra_rules, has_image=False, custom="", n_images=0):
    """组装系统指令。custom 非空时完全替换（仍会追加风格方向和额外要求）"""
    tpl = load_templates()
    parts = []

    if custom and custom.strip():
        parts.append(custom.strip())
    elif mode in EDIT_MODES:
        parts.append(tpl["编辑_英文" if mode == MODE_EDIT_EN else "编辑_中文"])
        lvl = {EXP_NONE: "编辑详细度_不扩写", EXP_BRIEF: "编辑详细度_简洁",
               EXP_MID: "编辑详细度_适中", EXP_DETAIL: "编辑详细度_详细"}.get(expansion)
        if lvl and lvl in tpl:
            parts.append(tpl[lvl])
        parts.append(tpl["硬性规则"])
    elif mode == MODE_PASS and not has_image:
        return ""
    elif has_image:
        actual = MODE_ZH if mode == MODE_PASS else mode
        key = "图像_负面词" if actual == MODE_NEG else "图像_" + _MODE_KEY.get(actual, "中文")
        parts.append(tpl[key])
        # 图像逆推：扩写程度用来控制"描述多细"，而不是脑补内容
        if actual != MODE_NEG:
            lvl = {EXP_NONE: "图像详细度_不扩写", EXP_BRIEF: "图像详细度_简洁",
                   EXP_MID: "图像详细度_适中", EXP_DETAIL: "图像详细度_详细"}.get(expansion)
            if lvl and lvl in tpl:
                parts.append(tpl[lvl])
        if n_images > 1:
            parts.append(tpl["多图融合"])
        parts.append(tpl["硬性规则"])
    elif mode == MODE_NEG:
        parts.append(tpl["负面词"])
        parts.append(tpl["硬性规则"])
    elif expansion == EXP_NONE:
        parts.append(tpl["不扩写_" + _MODE_KEY.get(mode, "中文")])
        parts.append(tpl["硬性规则"])
    else:
        parts.append(tpl["扩写核心"])
        parts.append(tpl[_EXP_KEY.get(expansion, "篇幅_适中")])
        if mode == MODE_ZH:
            parts.append(tpl["输出_" + _MODE_KEY[mode]])
        else:
            parts.append(tpl["输出_" + _MODE_KEY[mode]])
            parts.append(tpl["JSON格式要求"])
        parts.append(tpl["硬性规则"])

    if style_hint and style_hint.strip():
        parts.append("风格方向：整体风格按 " + style_hint.strip() + " 来统一（用户原文写了的风格优先）。")
    if extra_rules and extra_rules.strip():
        parts.append("额外要求：" + extra_rules.strip())

    return "\n".join(parts)


# ─── JSON Key 管理 ───────────────────────────────────────────────────────────────
# ─── 网络 ────────────────────────────────────────────────────────────────────────
# ─── LLM 调用 ────────────────────────────────────────────────────────────────────
# ─── 自动切换下线模型 ─────────────────────────────────────────────────────────────
_SKIP = ("embed", "tts", "audio", "whisper", "dall-e", "image", "imagen", "veo",
         "moderation", "transcribe", "realtime", "rerank", "speech", "aqa", "sora",
         "lyria", "vision-exp")
_FAST = {"flash", "mini", "haiku", "plus", "turbo", "chat"}
_UNSTABLE = {"preview", "exp", "beta", "test"}
_fallback_cache: dict = {}

# ─── 机器翻译（腾讯 / 百度）────────────────────────────────────────────────────────
def _tencent_translate(sid, skey, text):
    import hashlib, hmac as hm, time
    from datetime import datetime, timezone
    host = "tmt.tencentcloudapi.com"
    svc, action, ver, region = "tmt", "TextTranslate", "2018-03-21", "ap-guangzhou"
    payload = json.dumps({"SourceText": text, "Source": "auto", "Target": "en", "ProjectId": 0}, ensure_ascii=False)
    ts   = int(time.time())
    date = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
    ct   = "application/json; charset=utf-8"
    ch   = f"content-type:{ct}\nhost:{host}\nx-tc-action:{action.lower()}\n"
    sh   = "content-type;host;x-tc-action"
    cr   = "\n".join(["POST", "/", "", ch, sh, hashlib.sha256(payload.encode()).hexdigest()])
    scope= f"{date}/{svc}/tc3_request"
    s2s  = "\n".join(["TC3-HMAC-SHA256", str(ts), scope, hashlib.sha256(cr.encode()).hexdigest()])
    def _h(k, m): return hm.new(k, m.encode(), hashlib.sha256).digest()
    sk   = _h(_h(_h(("TC3"+skey).encode(), date), svc), "tc3_request")
    sig  = hm.new(sk, s2s.encode(), hashlib.sha256).hexdigest()
    headers = {
        "Authorization": f"TC3-HMAC-SHA256 Credential={sid}/{scope}, SignedHeaders={sh}, Signature={sig}",
        "Content-Type": ct, "Host": host, "X-TC-Action": action,
        "X-TC-Timestamp": str(ts), "X-TC-Version": ver, "X-TC-Region": region,
    }
    req  = urllib.request.Request(f"https://{host}/", data=payload.encode(), headers=headers, method="POST")
    body = _open_direct(req).get("Response", {})
    if "Error" in body:
        e = body["Error"]
        raise RuntimeError(f"腾讯翻译报错 {e.get('Code')}：{e.get('Message')}")
    return str(body.get("TargetText", "")).strip()

_BAIDU_ERR = {"52003":"APPID或密钥不正确","54001":"签名错误","54003":"访问频率受限",
              "54004":"余额不足","58000":"IP非法","58002":"服务已关闭","90107":"认证未通过"}

def _baidu_translate(appid, secret, text):
    import hashlib, random
    from urllib.parse import urlencode
    salt = str(random.randint(32768, 65536))
    sign = hashlib.md5((appid + text + salt + secret).encode()).hexdigest()
    form = urlencode({"q": text, "from": "auto", "to": "en", "appid": appid, "salt": salt, "sign": sign})
    req  = urllib.request.Request("https://fanyi-api.baidu.com/api/trans/vip/translate",
                                  data=form.encode(),
                                  headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    d = _open_direct(req)
    if "error_code" in d and str(d["error_code"]) != "52000":
        code = str(d["error_code"])
        raise RuntimeError(f"百度翻译报错 {code}：{_BAIDU_ERR.get(code, d.get('error_msg',''))}")
    return "\n".join(item.get("dst","") for item in d.get("trans_result",[])).strip()

# ─── 日志 ────────────────────────────────────────────────────────────────────────
import time as _time

HAS_CN = re.compile(r"[\u4e00-\u9fff]")

ERROR_ADVICE = {
    "HTTP 401": "API Key 错误或已失效，请检查 ai_prompt_keys.json 里的 Key",
    "HTTP 403": "没有访问权限，可能是 Key 无效、服务未开通或该地区不可用",
    "HTTP 402": "账户余额不足，请到服务商官网充值",
    "HTTP 404": "模型不存在或已下线，点“获取模型列表”重新选一个",
    "HTTP 429": "请求太频繁或超出配额，稍后再试",
    "HTTP 500": "服务商内部错误，稍后重试即可",
    "HTTP 503": "服务商暂时不可用，稍后重试即可",
    "timed out": "连接超时：海外服务商请检查代理，国内服务商请检查网络",
    "URLError": "网络连接失败：海外服务商请检查代理，国内服务商请检查网络",
    "Name or service": "域名解析失败，请检查服务器 DNS 设置",
}


# ─── 结果清洗 ────────────────────────────────────────────────────────────────────
# ─── 节点 ────────────────────────────────────────────────────────────────────────
class AIPromptTranslator:

    @classmethod
    def INPUT_TYPES(cls):
        providers = _provider_names()
        return {
            "required": {
                "文本": ("STRING", {
                    "multiline": True, "default": "",
                    "placeholder": "输入提示词或编辑意图，可用 图1、图2 指代接入的图像；只接一张图时可留空，AI 自动描述图像",
                }),
                "目标格式": (MODES, {
                    "default": MODE_ZH,
                    "tooltip": (
                        "中文（Qwen/ZIT/通用）：中文自然语言，适合 Z-Image、Qwen-Image、可图、混元、Wan\n"
                        "英文句子（Krea2/FLUX）：英文自然语言句子，适合 Krea 2、FLUX.2\n"
                        "英文标签（SDXL/Pony）：英文逗号标签，适合 SDXL、Pony、Illustrious\n"
                        "· 视频模式接入图片时按“首帧”处理，描述画面接下来如何运动\n"
                        "编辑指令（英文/中文）：给 Qwen-Image-Edit、FLUX Kontext 等编辑模型用的改图指令；"
                        "文本框写意图如“让图2的女孩穿上图1的衣服”。Kontext 只认英文，Qwen-Edit 中英都行\n"
                        "负面词（英文）：把不想要的内容整理成英文负面提示词\n"
                        "不处理：原样输出，不调用 API（接了图像时按中文描述图像）"
                    ),
                }),
                "扩写程度": (EXPANSIONS, {
                    "default": EXP_MID,
                    "tooltip": (
                        "不扩写：只翻译/整理，保持原意不增加内容\n"
                        "简洁：补充关键细节（40~60字）\n"
                        "适中：补充完整细节（80~120字）\n"
                        "详细：补充丰富细节（150~220字）\n"
                        "· 负面词和不处理模式不受此设置影响"
                    ),
                }),
                "服务商": (providers if providers else ["（请重启ComfyUI）"], {
                    "tooltip": "列表从 ai_prompt_keys.json 读取。添加自定义服务商后重启即可显示",
                }),
                "模型": ("STRING", {
                    "default": "",
                    "placeholder": "留空用推荐模型，或点下方按钮选择",
                    "tooltip": "留空自动使用推荐模型。推荐模型下线后会自动切换并提示",
                }),
                "创意度": ("FLOAT", {
                    "default": 0.6, "min": 0.0, "max": 1.5, "step": 0.05,
                    "tooltip": "控制扩写发散程度（越高越天马行空）。翻译/整理模式固定使用低数值，不受此影响",
                }),
                "每次运行": (["固定", "随机"], {
                    "default": "固定",
                    "tooltip": "固定：内容不变时复用上次结果不扣费；随机：每次都重新生成一版",
                }),
            },
            "optional": {
                "图像1": ("IMAGE", {"tooltip": "可选，最多 10 张。发给 AI 时依次标注为 图1、图2……，文本框里可以直接引用。\n需要服务商支持看图（通义千问/Gemini/OpenAI/Claude 默认支持，DeepSeek 不支持，可在 json 里用 vision 字段声明）。\n注意：Qwen-Image-Edit 本身最多接 3 张图，编辑指令里引用的图要和编辑模型实际接的图对应。"}),
                "图像2": ("IMAGE", {"tooltip": "可选，发给 AI 时标注为 图2"}),
                "图像3": ("IMAGE", {"tooltip": "可选，发给 AI 时标注为 图3"}),
                "图像4": ("IMAGE", {"tooltip": "可选，发给 AI 时标注为 图4"}),
                "图像5": ("IMAGE", {"tooltip": "可选，发给 AI 时标注为 图5"}),
                "图像6": ("IMAGE", {"tooltip": "可选，发给 AI 时标注为 图6"}),
                "图像7": ("IMAGE", {"tooltip": "可选，发给 AI 时标注为 图7"}),
                "图像8": ("IMAGE", {"tooltip": "可选，发给 AI 时标注为 图8"}),
                "图像9": ("IMAGE", {"tooltip": "可选，发给 AI 时标注为 图9"}),
                "图像10": ("IMAGE", {"tooltip": "可选，发给 AI 时标注为 图10"}),
                "思考模式": (["关闭", "开启"], {
                    "default": "关闭", "advanced": True,
                    "tooltip": "开启后 AI 先推理再回答，复杂要求更稳定，但更慢更贵。普通扩写翻译建议关闭",
                }),
                "风格方向": ("STRING", {
                    "default": "", "advanced": True,
                    "placeholder": "如：胶片摄影 / 日系清新 / 赛博朋克",
                    "tooltip": "给画面定整体风格，留空由 AI 按原文自判断。原文中的风格描述优先",
                }),
                "开头固定文字": ("STRING", {
                    "default": "", "advanced": True,
                    "placeholder": "不经过 AI，原样放最前面（触发词、<sks> 等）",
                }),
                "结尾固定文字": ("STRING", {
                    "default": "", "advanced": True,
                    "placeholder": "不经过 AI，原样放最后面",
                }),
                "给AI的额外要求": ("STRING", {
                    "multiline": True, "default": "", "advanced": True,
                    "placeholder": "如：人物是亚洲面孔；不要出现文字；只描写上半身",
                }),
                "API密钥": ("STRING", {
                    "default": "", "advanced": True,
                    "placeholder": "建议留空，在 ai_prompt_keys.json 里填写",
                    "tooltip": "填在节点上会被写进出图 PNG，分享图片等于泄露 Key",
                }),
                "代理地址": ("STRING", {
                    "default": "", "advanced": True,
                    "placeholder": "海外服务商用，如 127.0.0.1:7890；留空用系统代理",
                    "tooltip": "DeepSeek、通义千问、腾讯翻译、百度翻译始终直连，不受此影响",
                }),
                "自定义指令": ("STRING", {
                    "multiline": True, "default": "", "advanced": True,
                    "placeholder": "填了会完全替换本次的系统指令（风格方向和额外要求仍会追加）",
                    "tooltip": "临时试验用。长期修改建议直接编辑插件目录下的 ai_prompt_templates.json，改完不用重启",
                }),
                "详细日志": (["关闭", "开启"], {
                    "default": "关闭", "advanced": True,
                    "tooltip": "开启后命令行会额外打印完整系统指令、请求参数和 API 原始返回，用于排查问题",
                }),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("提示词",)
    FUNCTION = "run"
    CATEGORY = "自定义/高级提示词/提示词翻译"
    DESCRIPTION = "按目标底模架构翻译/扩写提示词；支持图像逆推；服务商由 JSON 配置"

    def run(self, 文本, 目标格式, 扩写程度, 服务商, 模型, 创意度, 每次运行,
            思考模式="关闭", 风格方向="", 开头固定文字="",
            结尾固定文字="", 给AI的额外要求="", API密钥="", 代理地址="",
            自定义指令="", 详细日志="关闭", **images):

        log       = Log(verbose=(详细日志 == "开启"))
        text      = 文本.strip()
        image_list = [images.get("图像" + str(i)) for i in range(1, MAX_IMAGES + 1)]
        image_list = [(i + 1, img) for i, img in enumerate(image_list) if img is not None]
        has_image = bool(image_list)
        providers = get_all_providers()
        cfg       = providers.get(服务商, {})
        style     = cfg.get("style", "openai")
        is_mt     = style == "mt"

        # ── 不处理（无图像）：不调用 API ────────────────────────────────────────
        if 目标格式 == MODE_PASS and not has_image:  # 不处理且无图：直接输出
            out = join_parts(开头固定文字, text, 结尾固定文字)
            log.head("模式：不处理（未调用 API）")
            log.add("最终输出：" + (out[:200] or "（空）"))
            log.flush()
            return (out,)

        # ── 图像能力检查（调用前拦截）──────────────────────────────────────────
        if has_image:
            if is_mt:
                raise RuntimeError(服务商 + " 是机器翻译，不支持图像输入")
            if not cfg.get("vision", False):
                msg = (服务商 + " 不支持图像输入（vision=false）。\n"
                       "解决办法：在 ai_prompt_keys.json 里把该服务商改成字典形式并加上 \"vision\": true，"
                       "或换用通义千问、Gemini、OpenAI、Claude 等支持视觉的服务商。")
                write_error_log(msg)
                raise RuntimeError(msg)

        # ── 机器翻译分支 ────────────────────────────────────────────────────────
        if is_mt:
            if 目标格式 in (MODE_ZH, MODE_EDIT_ZH):
                raise RuntimeError(服务商 + " 是机器翻译，只能翻译成英文。请选英文类的目标格式，或换用 AI 服务商")
            if not text:
                raise RuntimeError(服务商 + " 需要输入文本才能翻译")
            log.head("服务商：" + 服务商 + "（官方机器翻译）\n模式：忠实翻译（机器翻译不支持扩写）")
            log.sep()
            log.add("原文：" + text[:200])
            id_, secret = load_mt_creds(服务商, API密钥)
            if not id_ or not secret:
                f1, f2 = BUILTIN_PROVIDERS[服务商]["fields"]
                msg = ("没有找到 " + 服务商 + " 的凭据，请在 ai_prompt_keys.json 里填写 "
                       + 服务商 + "_" + f1 + " 和 " + 服务商 + "_" + f2)
                write_error_log(msg)
                raise RuntimeError(msg)
            try:
                fn = _tencent_translate if 服务商 == "腾讯翻译" else _baidu_translate
                result = fn(id_, secret, text)
            except Exception as e:
                adv = advice(str(e))
                msg = 服务商 + " 调用失败：" + str(e) + ("\n建议：" + adv if adv else "")
                write_error_log(msg)
                log.warn(str(e)); log.flush()
                raise RuntimeError(msg)
            if not result:
                raise RuntimeError(服务商 + " 返回了空内容")
            out = join_parts(开头固定文字, result, 结尾固定文字)
            log.add("\u2190 收到响应 " + ("%.1f" % log.elapsed()) + " 秒")
            log.sep()
            log.add("结果：" + result[:300])
            if out != result:
                log.add("最终输出：" + out[:300])
            log.flush()
            return (out,)

        # ── AI 服务商 ───────────────────────────────────────────────────────────
        key = load_key(服务商, API密钥)
        if not key:
            msg = "没有找到 " + 服务商 + " 的 API Key，请填写 " + KEYS_FILE
            write_error_log(msg)
            raise RuntimeError(msg)

        img_b64, img_info = [], []
        for idx, img in image_list:
            b64, o, n = img_to_b64(img)
            img_b64.append((idx, b64))
            img_info.append("图" + str(idx) + " " + str(o[0]) + "\u00d7" + str(o[1])
                            + ("" if o == n else "\u2192" + str(n[0]) + "\u00d7" + str(n[1])))

        actual_mode  = (MODE_ZH if 目标格式 == MODE_PASS else 目标格式) if has_image else 目标格式
        is_edit      = actual_mode in EDIT_MODES
        needs_expand = (扩写程度 != EXP_NONE and actual_mode != MODE_NEG)
        temperature  = 0.2 if (has_image or is_edit or not needs_expand) else 创意度
        use_json     = needs_expand and not has_image and not is_edit and actual_mode in (MODE_EN_NL, MODE_EN_TAG)

        if has_image:
            default_ask = ("请根据以上图片写出编辑指令" if is_edit else "请分析这张图片并生成提示词")
            user_msg = ("用户要求：" + text) if text else default_ask
        else:
            user_msg = text
        if not user_msg:
            return (join_parts(开头固定文字, "", 结尾固定文字),)

        system = _build_system(actual_mode, 扩写程度, 风格方向, 给AI的额外要求,
                               has_image and not is_edit, 自定义指令, len(img_b64))
        if 每次运行 == "随机" and needs_expand:
            system += "\n" + load_templates()["随机提示"]

        # ── 日志头 ──────────────────────────────────────────────────────────────
        model_show = (模型.strip() or (_fallback_cache.get(服务商) or cfg.get("model", "")) )
        is_reco    = not 模型.strip()
        head = ("服务商：" + 服务商 + "  模型：" + model_show + ("（推荐）" if is_reco else "（手动指定）") + "\n"
                + "模式：" + actual_mode + "  扩写：" + 扩写程度 + "  创意度：" + ("%.2f" % temperature) + "\n"
                + "思考模式：" + 思考模式 + "  运行方式：" + 每次运行
                + ("  风格方向：" + 风格方向.strip() if 风格方向.strip() else ""))
        if has_image:
            # 上传体积要打出来：带图的请求比刷新模型列表大上千倍，
            # 太大了代理会在读完前掐断连接，看着像网络坏了其实不是。
            _mb = sum(len(b) for _, b in img_b64) / 1048576.0
            head += ("\n识图：" + str(len(img_b64)) + " 张（" + "，".join(img_info) + "）"
                     + "　上传 %.2f MB" % _mb)
        if 自定义指令.strip():
            head += "\n\u26a0 使用了自定义指令（已替换默认指令）"
        log.head(head)
        log.sep()
        log.add("原文：" + (user_msg[:300] if user_msg else "（空）"))
        log.add("\u2192 组装指令：" + ("自定义" if 自定义指令.strip() else actual_mode)
                + ("（JSON 双语输出）" if use_json else "") + "，共 " + str(len(system)) + " 字")
        log.detail("系统指令全文", system)
        log.flush_now()

        # ── 调用（含重试）────────────────────────────────────────────────────────
        last_err = None
        zh = ""
        result = ""
        used = model_show
        usage = {}
        cn_retried = False

        for attempt in range(3):
            try:
                use_model = 模型.strip() or _fallback_cache.get(服务商, "")
                try:
                    raw, used, usage = call_ai(服务商, use_model, key, system, user_msg,
                                                temperature, 代理地址, (img_b64 or None),
                                                思考模式 == "开启", log)
                except RuntimeError as e:
                    msg = str(e)
                    model_problem = (msg.startswith("HTTP 404")
                                     or (msg.startswith("HTTP 400") and "model" in msg.lower()))
                    if 模型.strip() or not model_problem:
                        raise
                    log.warn("推荐模型 " + (cfg.get("model") or "") + " 不可用，正在查询可用模型…")
                    models = fetch_models(服务商, key, 代理地址)
                    new_m  = pick_fallback(服务商, models)
                    if not new_m:
                        raise
                    log.warn("已自动改用 " + new_m + "（本次运行期间将持续使用）")
                    _fallback_cache[服务商] = new_m
                    raw, used, usage = call_ai(服务商, new_m, key, system, user_msg,
                                                temperature, 代理地址, (img_b64 or None),
                                                思考模式 == "开启", log)

                # ── 解析 ────────────────────────────────────────────────────────
                if use_json:
                    zh, result = parse_zh_en(raw)
                    if not zh:
                        log.warn("未按 JSON 格式返回，已按纯文本处理")
                else:
                    result = clean_text(raw)
                    zh = result if actual_mode in (MODE_ZH, MODE_EDIT_ZH) else ""

                if not result:
                    last_err = "模型返回了空内容"
                    log.warn(last_err + "，正在重试")
                    continue

                # ── 英文模式中文残留检测 ────────────────────────────────────────
                english_mode = actual_mode in (MODE_EN_NL, MODE_EN_TAG, MODE_NEG, MODE_EDIT_EN)
                if english_mode and HAS_CN.search(result):
                    if not cn_retried:
                        cn_retried = True
                        system += "\n" + load_templates()["英文重试强化"]
                        log.warn("检测到英文模式返回了中文，已加强指令并重试")
                        continue
                    msg = (服务商 + "/" + used + " 多次返回中文，无法得到英文结果。\n"
                           "建议：换一个更强的模型，或把创意度调低到 0.3 以下，"
                           "或在 ai_prompt_templates.json 里强化对应模式的指令。")
                    write_error_log(msg + "\n\n原始返回：\n" + str(raw)[:2000])
                    log.warn("重试后仍有中文，判定失败"); log.flush()
                    raise RuntimeError(msg)

                break

            except RuntimeError as e:
                last_err = str(e)
                if "多次返回中文" in last_err:
                    raise
                adv = advice(last_err)
                log.warn("第 " + str(attempt + 1) + " 次失败：" + last_err[:150]
                         + ("　建议：" + adv if adv else ""))
                if any(("HTTP " + str(c)) in last_err for c in (400, 401, 402, 403, 404)):
                    break
            except Exception as e:
                last_err = repr(e)
                log.warn("第 " + str(attempt + 1) + " 次异常：" + last_err[:150])

        if not result:
            adv  = advice(last_err or "")
            hint = "" if cfg.get("domestic", False) else "（海外服务商需要能访问外网：开启代理，或在代理地址里填本地端口）"
            msg  = (服务商 + " 调用失败：" + str(last_err) + " " + hint
                    + ("\n建议：" + adv if adv else "")
                    + "\n详细信息已写入：" + ERROR_LOG)
            write_error_log("服务商：" + 服务商 + "\n模型：" + str(used) + "\n错误：" + str(last_err)
                             + "\n\n系统指令：\n" + system + "\n\n用户输入：\n" + str(user_msg)[:2000])
            log.warn("最终失败"); log.flush()
            raise RuntimeError(msg)

        # ── 成功日志 ────────────────────────────────────────────────────────────
        tok = ""
        if usage:
            i_t = usage.get("prompt_tokens") or usage.get("input_tokens") or "?"
            o_t = usage.get("completion_tokens") or usage.get("output_tokens") or "?"
            tok = "　用量：输入 " + str(i_t) + " / 输出 " + str(o_t) + " token"
        log.add("\u2190 收到响应 " + ("%.1f" % log.elapsed()) + " 秒" + tok)
        log.sep()
        if zh and zh != result:
            log.add("中文：" + zh[:300])
        log.add("结果：" + result[:400])

        out = join_parts(开头固定文字, result, 结尾固定文字)
        if out != result:
            log.add("最终输出：" + out[:400])
        log.flush()
        return (out,)

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        if kwargs.get("每次运行") == "随机":
            return float("nan")
        return json.dumps(kwargs, sort_keys=True, ensure_ascii=False, default=str)


class AIPromptTranslatorCLIP(AIPromptTranslator):

    @classmethod
    def INPUT_TYPES(cls):
        base = AIPromptTranslator.INPUT_TYPES()
        base["required"] = {"clip": ("CLIP",), **base["required"]}
        return base

    RETURN_TYPES = ("CONDITIONING", "STRING")
    RETURN_NAMES = ("条件", "提示词")
    FUNCTION = "encode"
    CATEGORY = "自定义/高级提示词/提示词翻译"

    def encode(self, clip, **kwargs):
        (prompt,) = self.run(**kwargs)
        tokens = clip.tokenize(prompt)
        if hasattr(clip, "encode_from_tokens_scheduled"):
            cond = clip.encode_from_tokens_scheduled(tokens)
        else:
            c, pooled = clip.encode_from_tokens(tokens, return_pooled=True)
            cond = [[c, {"pooled_output": pooled}]]
        return (cond, prompt)


# ─── 服务端：获取模型列表 API ──────────────────────────────────────────────────────
try:
    from server import PromptServer
    from aiohttp import web

    @PromptServer.instance.routes.post("/custom_nodes/models")
    async def _list_models(request):
        try:
            body     = await request.json()
            provider = body.get("provider", "")
            all_p    = get_all_providers()
            if provider not in all_p:
                return web.json_response({"error": f"未知服务商：{provider}"})
            cfg = all_p[provider]
            if cfg.get("style") == "mt":
                return web.json_response({"error": f"{provider} 是机器翻译，不需要选择模型"})
            key = load_key(provider, body.get("key", ""))
            if not key:
                return web.json_response({"error": f"没有找到 {provider} 的 API Key，请先填写 ai_prompt_keys.json"})
            loop   = asyncio.get_running_loop()
            models = await loop.run_in_executor(
                None, fetch_models, provider, key, body.get("proxy", "")
            )
            if not models:
                return web.json_response({"error": "接口没有返回可用模型"})
            return web.json_response({"models": models, "default": cfg.get("model", "")})
        except Exception as e:
            adv = advice(str(e))
            return web.json_response({"error": str(e) + ("\n建议：" + adv if adv else "")})

except Exception:
    pass  # 不在 ComfyUI 环境里时跳过

# ─── 初始化 & 注册 ───────────────────────────────────────────────────────────────
ensure_keys_file()
ensure_templates_file()


def _startup_summary():
    try:
        ps = get_all_providers()
        ok, no = [], []
        for name, cfg in ps.items():
            if cfg.get("style") == "mt":
                a, b = load_mt_creds(name)
                (ok if (a and b) else no).append(name)
            else:
                (ok if cfg.get("_key", "").strip() else no).append(name)
        msg = "[CustomNodes] 提示词翻译 | 已配置：" + ("、".join(ok) if ok else "无")
        if no:
            msg += " | 未填Key：" + "、".join(no)
        print(msg)
    except Exception:
        pass


_startup_summary()

NODE_CLASS_MAPPINGS = {
    "AIPromptTranslator":     AIPromptTranslator,
    "AIPromptTranslatorCLIP": AIPromptTranslatorCLIP,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "AIPromptTranslator":     "✨ 提示词翻译（扩写/翻译）",
    "AIPromptTranslatorCLIP": "✨ 提示词翻译（含CLIP编码）",
}
