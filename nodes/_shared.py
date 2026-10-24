"""
_shared.py — 公共工具模块（下划线开头，框架不会把它当成节点加载）

新写节点时可直接导入，不用重复实现：
    from _shared import Log, call_ai, load_key, get_all_providers, clean_text, join_parts, img_to_b64

主要内容
    Log                 带边框的流程日志（print 风格统一）
    get_all_providers() 读取 ai_prompt_keys.json，返回内置+自定义服务商配置
    load_key(名称)       取 API Key（节点参数 > JSON > 环境变量）
    call_ai(...)        调用 AI，自动处理各家格式差异、思考模式、参数降级
    fetch_models(...)   拉取该服务商可用模型列表
    pick_fallback(...)  推荐模型下线时挑一个替代
    img_to_b64(tensor)  ComfyUI 图像张量 → base64 JPEG（上限 1024px，JPEG 省带宽）
    clean_text(s)       清洗 AI 返回（去代码块/引号/前缀）
    join_parts(前,中,后) 拼接固定文字
    advice(错误信息)     把错误码翻译成中文解决建议
    write_error_log(内容) 写入 last_error.log
"""

import os, re, json, time, base64, urllib.request, urllib.error
from io import BytesIO

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEYS_FILE      = os.path.join(HERE, "ai_prompt_keys.json")
TEMPLATES_FILE = os.path.join(HERE, "ai_prompt_templates.json")
ERROR_LOG      = os.path.join(HERE, "last_error.log")

# ─── 服务商配置 ────────────────────────────────────────────────────────────────
BUILTIN_PROVIDERS = {
    "DeepSeek": {
        "base": "https://api.deepseek.com",
        "model": "deepseek-flash",
        "env": "DEEPSEEK_API_KEY",
        "domestic": True, "style": "openai", "vision": True,
        "extra": {"thinking": {"type": "disabled"}},  # 新模型默认开思考，这里关掉更快
        "prefix": ("deepseek",),
    },
    "通义千问": {
        "base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "env": "DASHSCOPE_API_KEY",
        "domestic": True, "style": "openai", "vision": True,
        "prefix": ("qwen",),
    },
    "Gemini": {
        "base": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-3.8-flash",
        "env": "GEMINI_API_KEY",
        "domestic": False, "style": "openai", "vision": True,
        "prefix": ("gemini",),
    },
    "OpenAI": {
        "base": "https://api.openai.com/v1",
        "model": "gpt-5-mini",
        "env": "OPENAI_API_KEY",
        "domestic": False, "style": "openai", "vision": True,
        "prefix": ("gpt-",),
    },
    "Claude": {
        "base": "https://api.anthropic.com/v1",
        "model": "claude-haiku-4-5-20251001",
        "env": "ANTHROPIC_API_KEY",
        "domestic": False, "style": "anthropic", "vision": True,
        "prefix": ("claude",),
    },
    "腾讯翻译": {
        "base": "", "model": "", "env": "", "domestic": True, "style": "mt", "vision": False,
        "fields": ("SecretId", "SecretKey"),
        "envs": ("TENCENTCLOUD_SECRET_ID", "TENCENTCLOUD_SECRET_KEY"),
    },
    "百度翻译": {
        "base": "", "model": "", "env": "", "domestic": True, "style": "mt", "vision": False,
        "fields": ("APPID", "密钥"),
        "envs": ("BAIDU_FANYI_APPID", "BAIDU_FANYI_KEY"),
    },
}

THINKING_PARAMS = {
    "DeepSeek":  ({"thinking": {"type": "disabled"}},  {"thinking": {"type": "enabled"}}),
    "通义千问":  ({"enable_thinking": False},           {"enable_thinking": True}),
    "Gemini":    ({"reasoning_effort": "none"},         {"reasoning_effort": "high"}),
    "OpenAI":    ({"reasoning_effort": "minimal"},      {"reasoning_effort": "medium"}),
    "Claude":    ({},                                   {"thinking": {"type": "enabled", "budget_tokens": 2048}}),
}

# ─── Key 管理 ──────────────────────────────────────────────────────────────────
def _keys_template():
    tpl = {}
    for name, cfg in BUILTIN_PROVIDERS.items():
        if cfg["style"] == "mt":
            for f in cfg["fields"]:
                tpl[f"{name}_{f}"] = ""
        else:
            tpl[name] = ""
    tpl["自定义服务商"] = {
        "_说明_删除此条": "参照下面示例添加自定义服务商，domestic=true表示国内直连不走代理，vision=true表示支持看图",
        "示例-硅基流动": {
            "base": "https://api.siliconflow.cn/v1",
            "model": "Qwen/Qwen2.5-7B-Instruct",
            "key": "",
            "domestic": True,
            "vision": False,
        },
    }
    return tpl

def ensure_keys_file():
    existing = {}
    if os.path.isfile(KEYS_FILE):
        try:
            existing = json.load(open(KEYS_FILE, encoding="utf-8"))
        except Exception:
            pass
    tpl = _keys_template()
    changed = not os.path.isfile(KEYS_FILE)
    for k, v in tpl.items():
        if k not in existing:
            existing[k] = v
            changed = True
    if changed:
        try:
            with open(KEYS_FILE, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

def _read_keys():
    try:
        return json.load(open(KEYS_FILE, encoding="utf-8"))
    except Exception:
        return {}

def get_all_providers():
    """
    返回完整的服务商配置字典（内置 + JSON 自定义）。
    每次调用都重新读 JSON，确保运行时数据是最新的。
    """
    data = _read_keys()
    providers = {}

    for name, base_cfg in BUILTIN_PROVIDERS.items():
        merged = dict(base_cfg)
        val = data.get(name, "")
        if isinstance(val, dict):
            if "key" in val:    merged["_key"] = val["key"]
            if "vision" in val: merged["vision"] = val["vision"]
        else:
            merged["_key"] = str(val).strip()
        providers[name] = merged

    custom_section = data.get("自定义服务商", {})
    for cname, cfg in custom_section.items():
        if cname.startswith("_") or cname.startswith("示例") or not isinstance(cfg, dict):
            continue  # 跳过说明行和模板里的示例条目
        if not str(cfg.get("base", "")).strip():
            continue  # 没填接口地址的不显示
        providers[cname] = {
            "base":     cfg.get("base", ""),
            "model":    cfg.get("model", ""),
            "env":      "",
            "domestic": cfg.get("domestic", False),
            "style":    "openai",
            "vision":   cfg.get("vision", False),
            "_key":     cfg.get("key", "").strip(),
            "_custom":  True,
        }

    return providers

def _provider_names():
    return list(get_all_providers().keys())

def load_key(provider_name, user_key=""):
    if user_key and user_key.strip():
        return user_key.strip()
    cfg = get_all_providers().get(provider_name, {})
    key = cfg.get("_key", "").strip()
    if key:
        return key
    env = cfg.get("env", "")
    return os.environ.get(env, "").strip() if env else ""

def load_mt_creds(provider_name, user_key=""):
    cfg = BUILTIN_PROVIDERS[provider_name]
    if user_key and user_key.strip():
        parts = [x.strip() for x in re.split(r"[,，\s]+", user_key.strip()) if x.strip()]
        if len(parts) >= 2:
            return parts[0], parts[1]
    data = _read_keys()
    f1, f2 = cfg["fields"]
    a = str(data.get(f"{provider_name}_{f1}", "")).strip() or os.environ.get(cfg["envs"][0], "").strip()
    b = str(data.get(f"{provider_name}_{f2}", "")).strip() or os.environ.get(cfg["envs"][1], "").strip()
    return a, b

# ─── 网络 ──────────────────────────────────────────────────────────────────────
def _opener(provider_name, proxy=""):
    cfg = get_all_providers().get(provider_name, {})
    if cfg.get("domestic", False):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    if proxy and proxy.strip():
        p = proxy.strip()
        if not re.match(r"^[a-zA-Z]+://", p):
            p = "http://" + p
        elif p.lower().startswith("https://"):
            p = "http://" + p[8:]  # 本地代理端口只接受普通连接
        return urllib.request.build_opener(urllib.request.ProxyHandler({"http": p, "https": p}))
    return urllib.request.build_opener(urllib.request.ProxyHandler(_system_proxies()))


def _system_proxies():
    """
    读取系统代理并纠正 Windows 上的已知问题：
    Python 会把注册表里的代理写成 https://127.0.0.1:7890，导致用 TLS 去连本地代理端口，
    Clash 等软件会直接断开，报 SSL UNEXPECTED_EOF_WHILE_READING。这里统一改成 http://。
    """
    fixed = {}
    try:
        for k, v in urllib.request.getproxies().items():
            if k in ("http", "https") and isinstance(v, str) and v.lower().startswith("https://"):
                v = "http://" + v[8:]
            fixed[k] = v
    except Exception:
        pass
    return fixed

def _http_json(opener, url, headers, payload=None, timeout=90):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}：{e.read().decode('utf-8', 'ignore')[:300]}")

# 发给 AI 的图片编码参数。
#
# ⚠ 一定要用 JPEG，不要用 PNG。PNG 不压缩，1024 的一张 base64 就有一两 MB，
#   接三张参考图请求体好几兆，走代理必被掐断（RemoteDisconnected / SSL EOF）。
#   换 JPEG 后体积只剩六分之一，连接问题就解决了。
#
# ⚠ 分辨率上限不要往下调。图片 token 只按【像素尺寸】算，跟文件多大无关——
#   也就是说降分辨率才省 token，换 JPEG 不省。但参考图是拿来认脸、认衣服的，
#   降到 768 以后衣服上的印花、刺绣、首饰细节就糊了，AI 只能瞎写。
#   省那点 token 不值得拿识图精度换。要省就减少参考图张数，别降分辨率。
IMG_MAX_EDGE = 1024
IMG_JPEG_Q   = 85


def img_to_b64(tensor):
    """ComfyUI IMAGE tensor -> (base64 JPEG, 原尺寸, 压缩后尺寸)"""
    from PIL import Image
    import numpy as np
    img_np = (tensor[0].cpu().numpy() * 255).clip(0, 255).astype("uint8")
    img = Image.fromarray(img_np)
    orig = img.size
    if max(img.size) > IMG_MAX_EDGE:
        img.thumbnail((IMG_MAX_EDGE, IMG_MAX_EDGE), Image.LANCZOS)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")          # JPEG 不支持透明通道
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=IMG_JPEG_Q, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("utf-8"), orig, img.size


def _open_direct(req, timeout=30):
    """国内接口直连请求（腾讯/百度翻译用）"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError("HTTP " + str(e.code) + "：" + e.read().decode("utf-8", "ignore")[:300])


# ─── AI 调用 ───────────────────────────────────────────────────────────────────
def call_ai(provider_name, model, key, system, user_text, temperature,
             proxy="", image_b64=None, thinking=False, log=None, timeout_s=None):
    """返回 (文本, 实际使用的模型, usage字典)"""
    cfg   = get_all_providers().get(provider_name, {})
    style = cfg.get("style", "openai")
    base  = cfg.get("base", "").rstrip("/")
    model = (model or "").strip() or cfg.get("model", "")
    if not model:
        raise RuntimeError(provider_name + "：没有配置模型名")

    think_off, think_on = THINKING_PARAMS.get(provider_name, ({}, {}))
    think_params = think_on if thinking else think_off
    base_t  = timeout_s or 90
    timeout = base_t * 2 if thinking else base_t

    # image_b64 可以是单个字符串，也可以是列表（多图，按顺序标注为 图1、图2…）
    # 列表元素可以是 base64 字符串，或 (编号, base64) 元组（编号对应节点上的 图像N 输入口）
    images = [image_b64] if isinstance(image_b64, str) else list(image_b64 or [])
    images = [it if isinstance(it, tuple) else (i, it) for i, it in enumerate(images, start=1)]
    if images:
        user_content = []
        for idx, b64 in images:
            # 编号是数字 → 标成「图N（Picture N）」；是文字 → 原样当标签用。
            # 文字标签用于不占参考图编号的图（比如视频节点的「上一段结尾」）。
            if isinstance(idx, str):
                user_content.append({"type": "text", "text": idx + "："})
            elif len(images) > 1:
                user_content.append({"type": "text", "text": "图" + str(idx) + "（Picture " + str(idx) + "）："})
            if style == "anthropic":
                user_content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}})
            else:
                user_content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}})
        user_content.append({"type": "text", "text": user_text})
    else:
        user_content = user_text

    opener = _opener(provider_name, proxy)

    if style == "anthropic":
        url     = base + "/messages"
        headers = {"Content-Type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"}
        minimal = {"model": model, "max_tokens": 6000 if thinking else 1500,
                   "system": system, "messages": [{"role": "user", "content": user_content}]}
        payload = dict(minimal, temperature=temperature, **think_params)
        parse   = lambda d: "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
        usage_of= lambda d: d.get("usage", {})
    else:
        url     = base + "/chat/completions"
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + key}
        minimal = {"model": model, "stream": False,
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": user_content}]}
        extra   = {k: v for k, v in cfg.get("extra", {}).items() if k not in think_params}
        payload = dict(minimal, temperature=temperature, **think_params, **extra)
        parse   = lambda d: d["choices"][0]["message"].get("content") or ""
        usage_of= lambda d: d.get("usage", {})

    if log:
        show = {k: v for k, v in payload.items() if k != "messages"}
        log.detail("请求参数", json.dumps(show, ensure_ascii=False))
        log.add("\u2192 请求已发送（超时 " + str(timeout) + " 秒）")
        log.flush_now()   # 立刻打出来，别让用户干等时以为卡死

    bodies = [payload, minimal] if payload != minimal else [payload]
    last_err = None
    for i, body in enumerate(bodies):
        try:
            data = _http_json(opener, url, headers, body, timeout=timeout)
            if log:
                log.detail("原始返回", json.dumps(data, ensure_ascii=False)[:1500])
            return parse(data), model, usage_of(data)
        except RuntimeError as e:
            last_err = e
            if not str(e).startswith("HTTP 400"):
                raise
            # ⚠ 不是所有 400 都是参数问题。地区封锁、额度、密钥失效也会返回 400，
            #   这些再怎么精简参数都一样会失败，重发纯属白跑一趟。
            low_e = str(e).lower()
            if any(k in low_e for k in ("location", "region", "country",
                                        "quota", "billing", "api key",
                                        "permission", "unsupported_country")):
                raise
            if log and i + 1 < len(bodies):
                log.warn("参数被拒绝（" + str(e)[:80] + "），已去掉可选参数重新发送")
    if last_err:
        raise last_err


# ─── 模型列表与自动降级 ─────────────────────────────────────────────────────────
_SKIP = ("embed", "tts", "audio", "whisper", "dall-e", "image", "imagen", "veo",
         "moderation", "transcribe", "realtime", "rerank", "speech", "aqa", "sora",
         "lyria", "vision-exp")
_FAST = {"flash", "mini", "haiku", "plus", "turbo", "chat"}
_UNSTABLE = {"preview", "exp", "beta", "test"}
_fallback_cache: dict = {}

def fetch_models(provider_name, key, proxy=""):
    cfg   = get_all_providers().get(provider_name, {})
    style = cfg.get("style", "openai")
    base  = cfg.get("base", "").rstrip("/")
    if style == "mt" or not base:
        return []
    opener = _opener(provider_name, proxy)
    headers = ({"x-api-key": key, "anthropic-version": "2023-06-01"}
               if style == "anthropic" else {"Authorization": f"Bearer {key}"})
    data   = _http_json(opener, base + "/models", headers, timeout=30)
    prefix = cfg.get("prefix", ())
    ids    = []
    for item in data.get("data", []) or data.get("models", []):
        mid = str(item.get("id") or item.get("name") or "").strip()
        if mid.startswith("models/"):
            mid = mid[7:]
        low   = mid.lower()
        words = set(re.split(r"[^a-z0-9]+", low))
        if prefix and not any(mid.lower().startswith(p) for p in prefix):
            continue
        if mid and not any(w in low for w in _SKIP):
            ids.append(mid)
    ids = sorted(set(ids))
    default = cfg.get("model", "")
    if default in ids:
        ids.remove(default)
        ids.insert(0, default)
    return ids

def pick_fallback(provider_name, models):
    default    = get_all_providers().get(provider_name, {}).get("model", "")
    candidates = [m for m in models if m != default]
    if not candidates:
        return ""
    def _score(m):
        words = set(re.split(r"[^a-z0-9]+", m.lower()))
        return (bool(words & _UNSTABLE), not (words & _FAST))
    return sorted(sorted(candidates, reverse=True), key=_score)[0]

# ─── 日志 ──────────────────────────────────────────────────────────────────────
HAS_CN = re.compile(r"[\u4e00-\u9fff]")

ERROR_ADVICE = {
    # 这几条要放在通用的 HTTP 码前面，否则会被笼统的提示盖住
    "location is not supported": (
        "该服务商不向本地区开放：说明请求是用服务器自己的 IP 直连出去的，代理没生效。\n"
        "办法一：在高级输入的“代理地址”里填上代理端口（如 127.0.0.1:7890），不要只靠环境变量；\n"
        "办法二：换成国内服务商（DeepSeek、通义千问），它们没有地区限制"
    ),
    "User location": (
        "该服务商不向本地区开放：代理没生效，请在“代理地址”里填上代理端口，"
        "或换成国内服务商（DeepSeek、通义千问）"
    ),
    "HTTP 401": "API Key 错误或已失效，请检查 ai_prompt_keys.json 里的 Key",
    "HTTP 403": "没有访问权限，可能是 Key 无效、服务未开通或该地区不可用",
    "HTTP 402": "账户余额不足，请到服务商官网充值",
    "HTTP 404": "模型不存在或已下线，点“获取模型列表”重新选一个",
    "HTTP 429": "请求太频繁或超出配额，稍后再试",
    "HTTP 500": "服务商内部错误，稍后重试即可",
    "HTTP 503": "服务商暂时不可用，稍后重试即可",
    # 对端在读完请求前就掐断连接。能刷新模型列表（几 KB 的小请求）却发不出提示词
    # （带图的大请求），基本就是这一条：网络是通的，是请求体太大走不动代理。
    "RemoteDisconnected": (
        "请求发出去一半被掐断了。注意：如果“获取模型列表”能正常刷新，那网络是通的，"
        "问题在请求体太大——带图的请求比刷新列表大上千倍，代理扛不住就会这样。\n"
        "办法一：减少参考图数量（三张改一两张）；\n"
        "办法二：换成国内服务商（DeepSeek、通义千问），不走代理就不会被掐；\n"
        "办法三：代理软件换个节点，或在“代理地址”里填本地代理端口（如 127.0.0.1:7890）"
    ),
    "UNEXPECTED_EOF": (
        "代理连接被断开。如果“获取模型列表”刷得动，说明网络没问题，是带图的请求太大。\n"
        "办法：减少参考图数量，或换国内服务商（DeepSeek、通义千问），"
        "也可以在“代理地址”填本地代理端口（如 127.0.0.1:7890）并确认代理软件已开启"
    ),
    "ConnectionReset": (
        "连接被重置：多半是请求体过大或代理不稳。减少参考图数量，"
        "或换成国内服务商（DeepSeek、通义千问）"
    ),
    "SSL": "加密连接失败：多为代理问题，请在“代理地址”填本地代理端口（如 127.0.0.1:7890），或检查代理软件是否开启",
    "timed out": "连接超时：海外服务商请检查代理，国内服务商请检查网络",
    "URLError": "网络连接失败：海外服务商请检查代理，国内服务商请检查网络",
    "Name or service": "域名解析失败，请检查服务器 DNS 设置",
}


def advice(msg):
    for k, v in ERROR_ADVICE.items():
        if k in msg:
            return v
    return ""


class Log:
    """收集一次调用的全流程日志，最后统一打印"""

    def __init__(self, verbose=False):
        self.verbose = verbose
        self.lines = []
        self.t0 = time.time()

    def head(self, text):
        stamp = time.strftime("%H:%M:%S")
        self.lines.append("[提示词翻译 " + stamp + "] " + "\u2501" * 30)
        for ln in text.splitlines():
            self.lines.append("  " + ln)

    def sep(self):
        self.lines.append("  " + "\u2500" * 37)

    def add(self, text, indent="  "):
        for ln in str(text).splitlines():
            self.lines.append(indent + ln)

    def warn(self, text):
        self.lines.append("  \u26a0 " + str(text))

    def detail(self, title, text):
        if not self.verbose:
            return
        self.lines.append("  \u25b8 " + title + "：")
        for ln in str(text).splitlines():
            self.lines.append("      " + ln)

    def elapsed(self):
        return time.time() - self.t0

    def flush_now(self):
        """把已经攒下的行立刻打出来。

        长请求（识图 + 长指令）要等好几分钟，全程不吭声会让人以为卡死。
        开头那几行先打出去，用户能看见在干活。
        """
        if self.lines:
            print("\n".join(self.lines), flush=True)
            self.lines = []

    def flush(self):
        self.lines.append("\u2501" * 44)
        print("\n".join(self.lines), flush=True)
        self.lines = []


def write_error_log(content):
    try:
        with open(ERROR_LOG, "w", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n" + content)
    except Exception:
        pass


# ─── 结果处理 ──────────────────────────────────────────────────────────────────
def clean_text(text):
    text = (text or "").strip()
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()
    if len(text) > 1 and text[0] == text[-1] and ord(text[0]) in (34, 39, 0x201c, 0x201d):
        text = text[1:-1].strip()
    text = re.sub(r"^(prompt|提示词|英文|中文|输出|结果)\s*[:：]\s*", "", text, flags=re.I)
    return text.strip()

def parse_zh_en(text):
    text = clean_text(text)
    # 截到最后一个 }，并允许字符串里出现真换行（strict=False）。
    # 分条输出时 AI 经常直接写换行而不是 \n，默认的严格解析会失败，
    # 结果是整段原始 JSON 被当成提示词输出。
    m = re.search(r"\{.*\}", text, flags=re.S)
    if m:
        try:
            obj = json.loads(m.group(0), strict=False)
            en  = clean_text(str(obj.get("en", "")))
            if en:
                return clean_text(str(obj.get("zh", ""))), en
        except Exception:
            pass
    return "", text

def join_parts(prefix, body, suffix):
    parts = [p.strip().strip(",，") for p in (prefix, body, suffix)]
    return ", ".join(p for p in parts if p)

