"""
📝 多行提示词（逐条出图）节点
菜单：自定义 → 高级提示词 → 多行提示词

一个文本框写多行提示词，一次运行按行逐张出图。
主要为 Qwen-Image-Edit-2511 多角度 LoRA 设计，支持中文简写角度自动转英文。
"""

import re

# 中文简写 → 多角度 LoRA 英文词（整词替换，按空格分词）
_CN_MAP = {
    "正面": "front view",
    "右前": "front-right quarter view",
    "右侧": "right side view",
    "右后": "back-right quarter view",
    "背面": "back view",
    "左后": "back-left quarter view",
    "左侧": "left side view",
    "左前": "front-left quarter view",
    "仰拍": "low-angle shot",
    "平视": "eye-level shot",
    "微俯": "elevated shot",
    "俯拍": "high-angle shot",
    "特写": "close-up",
    "中景": "medium shot",
    "远景": "wide shot",
}

_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _translate_cn(line: str) -> str:
    return " ".join(_CN_MAP.get(word, word) for word in line.split())


def _to_filename(text: str, max_len: int = 80) -> str:
    text = text.replace("<sks>", "")
    text = _BAD_CHARS.sub("", text)
    text = re.sub(r"[\s,，]+", "_", text.strip()).strip("_.")
    return text[:max_len] or "prompt"


class PromptLineList:

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "提示词": ("STRING", {
                    "multiline": True,
                    "default": "背面 平视 中景\n左侧 平视 中景\n正面 仰拍 中景",
                    "placeholder": "一行一条，写几行就出几张。空行和 # 开头的行会被忽略。\n支持中文简写：正面/右前/右侧/右后/背面/左后/左侧/左前，仰拍/平视/微俯/俯拍，特写/中景/远景",
                }),
                "开头补充": ("STRING", {
                    "default": "<sks>",
                    "tooltip": "自动加在每行开头；该行已经以它开头就不重复加。不需要就清空",
                }),
                "结尾补充": ("STRING", {
                    "default": "",
                    "tooltip": "自动加在每行末尾，逗号连接。如：she looks straight ahead",
                }),
                "从第几条开始": ("INT", {
                    "default": 1, "min": 1, "max": 9999,
                    "tooltip": "只想重跑某几条时用（从 1 开始计数）",
                }),
                "最多跑几条": ("INT", {
                    "default": 0, "min": 0, "max": 9999,
                    "tooltip": "0 = 全部",
                }),
                "保存子文件夹": ("STRING", {
                    "default": "angles",
                    "tooltip": "图片保存到 output/这个文件夹 里，文件名带编号和角度名",
                }),
            },
        }

    RETURN_TYPES  = ("STRING",   "STRING",   "INT")
    RETURN_NAMES  = ("提示词列表", "文件名列表", "条数")
    OUTPUT_IS_LIST= (True, True, False)
    FUNCTION      = "build"
    CATEGORY      = "自定义/高级提示词/多行提示词"
    DESCRIPTION   = "多行提示词逐条出图，支持中文角度简写，输出带角度名的文件名便于打标"

    def build(self, 提示词, 开头补充, 结尾补充, 从第几条开始, 最多跑几条, 保存子文件夹):
        lines = []
        for raw in 提示词.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            lines.append(line)

        lines = lines[从第几条开始 - 1:]
        if 最多跑几条 > 0:
            lines = lines[:最多跑几条]
        if not lines:
            raise ValueError("【多行提示词】没有可用的提示词：检查是否全是空行，或“从第几条开始”超出了总行数")

        head   = (开头补充 or "").strip()
        tail   = (结尾补充 or "").strip()
        folder = _BAD_CHARS.sub("", (保存子文件夹 or "").strip()).strip() or ""

        prompts, names = [], []
        for i, line in enumerate(lines, start=从第几条开始):
            body = _translate_cn(line)
            if head and not body.startswith(head):
                body = f"{head} {body}"
            if tail:
                body = f"{body}, {tail}"
            prompts.append(body)

            fname = f"{i:03d}_{_to_filename(body)}"
            names.append(f"{folder}/{fname}" if folder else fname)

        print(f"[多行提示词] 共 {len(prompts)} 条：")
        for p in prompts:
            print(f"    {p}")

        return (prompts, names, len(prompts))


NODE_CLASS_MAPPINGS        = {"PromptLineList": PromptLineList}
NODE_DISPLAY_NAME_MAPPINGS = {"PromptLineList": "📝 多行提示词（逐条出图）"}
