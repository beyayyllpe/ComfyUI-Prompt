"""
_template.py — 新节点骨架模板（下划线开头，框架不会加载它）

用法：复制本文件 → 改名成英文文件名（如 image_tagger.py）→ 放进 nodes/ → 重启 ComfyUI

菜单命名规则（必须遵守）
    CATEGORY = "自定义/高级提示词/多行提示词"   ← 已有
    CATEGORY = "自定义/<功能大类>/<具体分组>"    ← 新节点
    同类只有一个节点时可以写 "自定义/<功能大类>"，不必再套一层

参数规范
    · 参数名用中文短词；复杂含义写进 tooltip，不要靠加长参数名
    · 不常用的参数放 optional 并加 "advanced": True，界面上会收进"显示高级输入"
    · 只用 Python 标准库（PIL / numpy / torch 是 ComfyUI 自带的，可以用）

可直接复用的公共能力（见 _shared.py 顶部说明）
    from _shared import Log, call_ai, load_key, get_all_providers, clean_text, join_parts, img_to_b64
"""

from _shared import Log        # 需要 AI 能力时再导入 call_ai / load_key 等


class MyNode:

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "文本": ("STRING", {
                    "multiline": True, "default": "",
                    "placeholder": "灰色提示文字，一打字就消失",
                }),
                "模式": (["选项一", "选项二"], {
                    "default": "选项一",
                    "tooltip": "鼠标悬停时显示的说明，可以写多行",
                }),
                "数值": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 2.0, "step": 0.05}),
                "开关": ("BOOLEAN", {"default": True, "label_on": "开", "label_off": "关"}),
            },
            "optional": {
                "图像": ("IMAGE",),                      # 可选输入口
                "高级参数": ("STRING", {
                    "default": "", "advanced": True,     # ← 收进"显示高级输入"
                    "placeholder": "不常用的参数放这里",
                }),
            },
        }

    RETURN_TYPES = ("STRING",)      # 多输出写成 ("STRING", "IMAGE")
    RETURN_NAMES = ("结果",)
    # OUTPUT_IS_LIST = (True,)      # 想让下游逐条运行时打开（见 prompt_list.py）
    FUNCTION  = "run"
    CATEGORY  = "自定义/高级提示词/示例分组"
    DESCRIPTION = "一句话说明这个节点做什么"

    def run(self, 文本, 模式, 数值, 开关, 图像=None, 高级参数=""):
        log = Log()                                  # 统一风格的流程日志
        log.head("模式：" + 模式 + "  数值：" + str(数值))
        log.sep()
        log.add("原文：" + 文本[:200])

        result = 文本.upper() if 开关 else 文本       # ← 这里写实际逻辑

        log.add("结果：" + result[:200])
        log.flush()                                  # 一次性打印，不要零散 print
        return (result,)

    # 需要每次都重新执行（不吃缓存）时打开：
    # @classmethod
    # def IS_CHANGED(cls, **kwargs):
    #     return float("nan")


NODE_CLASS_MAPPINGS        = {"MyNode": MyNode}                  # 类名唯一，别和现有节点重名
NODE_DISPLAY_NAME_MAPPINGS = {"MyNode": "🔧 我的节点"}            # 菜单里显示的名字
