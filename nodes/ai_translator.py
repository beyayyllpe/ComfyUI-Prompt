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
        "2. 把抽象词转成具体视觉细节 —— \u300c高级感\u300d要落成具体的材质、光线和构图，不能原样搬进提示词。\n"
        "3. 只补用户没有指定的维度，给出一种最协调的具体设定。\n"
        "4. 全篇只有一种明确的视觉风格。\n"
        "5. 按下面【画面维度】的五层来组织，不要套固定顺序 —— 主体是建筑或静物时，\u300c外貌服装\u300d这类人物专用的维度根本不适用。\n"
        "6. 颜色一律带修饰语，不要写裸色名：深藏青、灰蓝、米白、暖陶土色、"
        "黯橄榄绿，而不是只写\u300c蓝\u300d\u300c绿\u300d。\n"
        "7. 写材质不只写东西名：拉丝金属、哑光塑料、粗麻、磨砂玻璃、做旧木头，"
        "让模型知道表面长什么样。\n"
        "8. 人物年龄写人生阶段，不写具体岁数：孩童、少年、二十出头、三十多岁、"
        "中年、年长。具体数字对画面没有意义。\n"
        "9. 画面要在物理上立得住：影子背着光源走，反射要和面前的东西对得上，"
        "相邻物体的大小比例要合理，放东西的表面要有相应的反应。\n"
        "⚠ 画面里本来没有文字时，不要凭空加招牌、标语、字幕。"
        "很多画面根本没有可读文字，硬加是错的；只有用户要求写字时才写。"
    ),
    "画面维度": (
        "【开头第一句：20~30 字内回答四件事】\n"
        "  1 这是什么图 —— 媒介和风格（照片 / 插画 / 渲染 / 水彩 / 海报）\n"
        "  2 画的是什么 —— 主体。【可数的主体，数量必须明确】：多个时写明几个\n"
        "  3 在哪 —— 环境\n"
        "  4 处于什么状态 —— 有生命的写动作，无生命的写状态或关系"
        "（立着 / 摞着 / 倒扣着 / 正在冒热气 / 半塌着）\n"
        "  例：写实人像摄影，一位年轻女性在霓虹灯下的雨后街头独舞，冷调夜色。\n"
        "  例：建筑摄影，一座清水混凝土美术馆立在缓坡草地上，黄昏逆光。\n"
        "  例：静物摄影，三只手工陶杯摞在深色木桌上，侧窗光。\n"
        "  ⚠ 主体出现得越晚，模型抓住它的可能性越低 —— "
        "【不要先用一整段讲相机参数，再提画面里有什么】。\n"
        "\n"
        "【然后按五层展开细节】\n"
        "\n"
        "⚠ 下面每一项列的词都是【常见示例，不是选项菜单】。"
        "碰到更合适的说法就用更合适的，不要被这些词框住。\n"
        "⚠ 但不管用什么词，都要【具体到可验证】："
        "焦段给数字、光圈给 f 值、光线给方向和色温，"
        "不许写\u300c合适的焦段\u300d\u300c良好的光线\u300d这类废话。\n"
        "\n"
        "第一层 · 拍摄 —— 这张图是怎么被拍下来的\n"
        "  媒介：数码、135 胶片、中画幅、手机、拍立得、CG 渲染、插画、水彩……\n"
        "  景别：大特写 / 特写 / 近景 / 半身 / 全身 / 中景 / 全景 / 远景……\n"
        "  机位：高度（仰拍、平视、略俯、俯拍、顶拍）＋ 方位（正面、四分之三侧、正侧、背侧）\n"
        "  焦段：给具体数值和它带来的画面特征 —— 24mm 透视夸张空间开阔、"
        "50mm 接近人眼、85mm 压缩背景面部自然、135mm 强压缩。"
        "鱼眼、微距、移轴、超长焦同样可用\n"
        "  光圈景深：⚠ 写【效果】不写器材参数 —— 模型认得\u300c浅景深、背景柔化、bokeh\u300d，"
        "不认得 f/2.8 这串字符。要写也是效果在前、f 值最多作为补充\n"
        "  快门与运动：同理，写\u300c动作被定格、发丝清晰不拖影\u300d或\u300c运动模糊\u300d，"
        "不写\u300c1/1000 秒\u300d\n"
        "  构图：三分法、中心对称、负空间、引导线、框架式、对角线……\n"
        "    ⚠ 构图法的【名称】模型执行得很不可靠。真想控制构图，"
        "直接描述物体在画面里的位置关系，比写术语名有用得多\n"
        "  ⚠ 画幅（横构图 / 竖构图）由生成节点的宽高决定，【写进提示词基本没用】，不要占字数\n"
        "\n"
        "第二层 · 主体 —— 先判断主体是哪一类，【只走那一类该写的维度】，"
        "不适用的跳过，不要硬套\n"
        "  人物：年龄阶段·体型·族裔特征 → 五官·发型·妆容 → "
        "服装（款式·材质·颜色·纹样·新旧）·配饰 → "
        "姿态（身体朝向·重心·手部·头部角度）→ 表情·视线方向 → "
        "【用具体动词写动作】，不写\u300c优雅地\u300d\u300c性感地\u300d这类评价词 → 持物与交互\n"
        "  动物：物种品种 → 体型比例 → 毛羽鳞的质感·花纹·颜色 → "
        "姿态 → 头部朝向·眼睛·耳朵位置（这是动物的表情）→ "
        "动态细节（肌肉紧张、毛发飘动）→ 与环境的交互\n"
        "  建筑：类型与功能 → 年代与流派 → 体量几何（层数·比例·对称性）→ "
        "材料与表面（清水混凝土、红砖、玻璃幕墙、木构、石材）＋新旧风化 → "
        "结构元素（柱·拱·屋顶形式·开窗节奏）→ 细部装饰 → "
        "【尺度参照】（有没有人或车给出大小感）→ 与地形周边的关系\n"
        "  静物与产品：形状·尺寸·材质·表面处理（拉丝·磨砂·抛光）→ "
        "摆放与堆叠关系 → 承托面材质 → 道具陪衬 → 反射与投影\n"
        "  食物：以上加熟度·质地（酥脆·多汁·绵密）·热气·酱汁流动·新鲜度·切面\n"
        "  载具：车型年代 → 车身线条比例 → 漆面状态（新·氧化·泥污）→ "
        "轮毂·格栅·灯组细部 → 静止还是运动（轮子虚化、扬尘）\n"
        "  风景：地貌 → 植被 → 水体状态 → 天空与云 → 季节 → 前中后景的层次\n"
        "  群像：人数 → 主次关系 → 站位与间距 → 彼此之间在干什么\n"
        "  抽象图形：形状语言 → 配色方案 → 构成方式 → 质感（平涂·渐变·颗粒）\n"
        "\n"
        "  ★ 画面里有多个同类主体时（几个人、几只杯子、一排建筑），"
        "按这四条写，否则属性会串到一起、最后糊成一个：\n"
        "    (1) 【每个主体独立成句】，以锚点开头："
        "从左到右 —— 左一……；左二……；右二……；右一……\n"
        "    (2) 【一个主体的属性必须连续写完】，"
        "中间不许插入别人的信息，写完一个再写下一个\n"
        "    (3) 【锚点在块内至少重复一次】："
        "\u300c左一梳黑色长直发，左一身穿炭黑漆皮挂脖背心，左一正单手叉腰\u300d —— "
        "重复锚点能显著拉紧绑定\n"
        "    (4) ⚠ 【不要在分述之前写共有属性】。"
        "\u300c四人身材都很修长\u300d这种句子悬在\u300c四位\u300d和\u300c左一\u300d之间，"
        "不属于任何人，是串位的温床 —— 共有属性要么并进各自的块里，要么放到最后统一说\n"
        "    多主体时【每个主体仍要写满该类型的全部维度】，"
        "不许因为人多就简写。人多就写长，不要牺牲单个主体的完整度。\n"
        "\n"
        "  ⚠ 以上【不是全部类型】。碰到没列到的主体（机械、昆虫、服装平铺、"
        "界面截图、地图、图表、神话生物……），按同样的思路自己定该写哪些维度："
        "这个东西的形态、材质、状态、细部，以及它和环境的关系。\n"
        "\n"
        "第三层 · 环境 —— 主体之外的空间\n"
        "  场所类型·室内外 → 时间（时刻·季节·天气）→ 环境陈设与元素 → "
        "空间分层（前景·中景·远景）→ 与主体的关系（虚化程度、是否呼应主体）\n"
        "\n"
        "第四层 · 光线 —— 同时作用于主体和环境，所以单独算一层\n"
        "  光源类型（自然光·窗光·影棚闪光·实用光源如台灯霓虹屏幕·火光）→ "
        "方向（顺光·伦勃朗 45°·侧光·侧逆光·逆光轮廓光·顶光·底光）→ "
        "质感（硬光边缘锐利 / 柔光过渡平滑）→ 光比（高反差 / 平光）→ "
        "色温（钨丝暖黄 / 中性 / 阴天冷蓝）→ 投影和高光落在哪\n"
        "\n"
        "第五层 · 风格与质感\n"
        "  整体风格 → 色彩处理（胶片色·高对比·低饱和·单色）→ "
        "画面质感（颗粒·锐度·噪点·笔触）→ 气氛\n"
    ),

    "篇幅_简洁": (
        "【本档：简洁】五层都要出现，但每层只取最关键的一两项。\n"
        "  拍摄：定下景别和机位高度即可，焦段景深可以不写\n"
        "  主体：该类型最要紧的 2~3 项（人物＝身份·服装主色·在做什么）\n"
        "  环境：一句话说清在哪\n"
        "  光线：方向 + 冷暖\n"
        "  风格：一个词\n"
        "篇幅（按你最终要输出的格式对号入座）：\n"
        "  · 中文：60~100 字\n"
        "  · 英文自然语言：60~90 词\n"
        "  · 英文标签：25~32 个词\n"
        "宁可少写也不要含糊 —— 写到的每一项都要具体。"
    ),
    "篇幅_适中": (
        "【本档：适中】五层都要覆盖到主要维度，每项用一两个短语讲清。\n"
        "  拍摄：景别 + 机位 + 【具体焦段】 + 景深\n"
        "  主体：该类型的主要维度都写到，每项一两个短语\n"
        "  环境：场所 + 时间或天气 + 前后景的空间层次\n"
        "  光线：光源类型 + 方向 + 软硬 + 色温\n"
        "  风格：整体风格 + 色彩处理\n"
        "篇幅（按你最终要输出的格式对号入座）：\n"
        "  · 中文：150~250 字\n"
        "  · 英文自然语言：160~230 词\n"
        "  · 英文标签：38~45 个词\n"
        "每一句都要带新信息，不要把同一件事换个说法再说一遍。"
    ),
    "篇幅_详细": (
        "【本档：详细】五层全部逐项展开，这是信息量最大的一档。\n"
        "  拍摄：媒介 + 景别 + 机位高度和方位 + 具体焦段及其画面特征 + "
        "光圈与景深 + 快门或运动表现 + 主体在画面里的【位置和留白】\n"
        "  主体：该类型的每一个维度都写到，并深入到"
        "【材质、纹样、新旧状态、细部】这一层\n"
        "  环境：前景·中景·远景分层交代，陈设要具体到物件\n"
        "  光线：光源 + 方向 + 软硬 + 光比 + 色温，"
        "并说明投影和高光【落在哪里】\n"
        "  风格：整体风格 + 色彩处理 + 画面质感（颗粒·锐度·笔触）+ 气氛\n"
        "篇幅（按你最终要输出的格式对号入座）：\n"
        "  · 中文：300~450 字\n"
        "  · 英文自然语言：300~420 词\n"
        "  · 英文标签：45~52 个词\n"
        "⚠ 标签那个数字【不跟着中文一起膨胀】，它受一个硬性的技术上限约束，"
        "具体原因和取舍办法写在标签格式自己的要求里。\n"
        "⚠ 篇幅长不等于可以灌水：【每一句都必须带新的视觉信息】。"
        "换个说法重复同一件事、堆砌没有画面的形容词，都算没写。"
    ),
    "输出_中文": "输出：中文自然语言，逗号连接短句。颜色带修饰语，材质写明。",
    "输出_英文句子": "英文版要求：把中文扩写结果转成流畅具体的英文自然语言描述，总词数按上面的篇幅档来。"
                          "【句子长短随你，不用凑句数】—— 该用长复合句就用长句，"
                          "不要为了凑数把一句劈成三句，也不要为了压句数把两件事硬塞进一句。"
                          "判断标准只有一个：总词数到了，而且每个词都带画面。\u3010保留全部视觉细节\u3011，一个都不要丢，不要逐字硬译。颜色用带修饰语的说法（deep navy / blue-grey / pale cream），材质写明（brushed metal / coarse linen）。",
    "输出_英文标签": "英文版要求：把中文扩写结果转成 SDXL 习惯的英文标签，短语用逗号分隔，每个短语 1~4 个词，使用 SD 社区通用标签，总长度按上面的篇幅档来。颜色标签带修饰语（deep navy 而不是 blue），材质单独成标签（brushed metal、matte plastic）。\n"
                          "⚠ 五层里【只取标签体系真有对应词的部分】：景别（close-up / full body）、"
                          "机位（from below / from above）、景深（depth of field / bokeh）、"
                          "光线（rim light / backlit / soft lighting / neon lighting）、"
                          "材质、颜色、风格质感 —— 这些都有社区通用标签。\n"
                          "【没有通用标签的维度不要硬写成一串词】：光比、投影落在哪、构图法名称、"
                          "具体 f 值和快门速度，写了也不起作用，只会挤掉有效标签的权重。\n"
                          "⚠ 可数主体的数量必须写明。【单人也要写 solo / 1girl 这类独占标签】，"
                          "否则模型很容易自己多加人。\n"
                          "\n"
                          "⚠ 标签格式【不跟着中文一起膨胀】，它受一个硬性的技术上限约束。\n"
                          "  SDXL 的 CLIP 编码器每 77 个 token 切一段，"
                          "【每段单独编码一次，各自带自己的开始和结束标记，段与段之间互相看不见】。"
                          "实测一个英文词约合 1.37 个 token，所以 50 个词 ≈ 70 token，"
                          "刚好在一段之内还留一点余量 —— 这才是真实上限。\n"
                          "  写超了有两个后果，都很严重：\n"
                          "    一、切到第二段往后的词，模型基本不执行，字数白花；\n"
                          "    二、更要紧的是【修饰语会和被修饰的词被切开】。"
                          "假如 deep burgundy 落在第一段末尾、satin slip dress 被切到第二段开头，"
                          "这两组词在编码时永远碰不上面，模型就不知道是谁 burgundy —— "
                          "这是属性串位最常见的成因。\n"
                          "  所以压到篇幅档给的词数【不是少写内容】，是让写下的每个词都真的生效。"
                          "标签的信息密度本来就比散文高三倍："
                          "\u300c身穿亮面漆皮炭黑挂脖背心\u300d二十个字，"
                          "写成 black patent leather halter top 只要五个词。"
                          "\n"
                          "  ⚠ 标签格式下，详细档和适中档【只差几个词，区别在取舍不在长度】。"
                          "详细档把额度全花在画面本身；适中档留出余量，"
                          "方便用户自己再加 LoRA 触发词和质量词。\n"
                          "  ⚠ 【用户自己写死的内容不参与压缩】：用户指定的主体、服装、颜色、数量、"
                          "LoRA 触发词、特殊语法，一律先占位，压缩只砍你自己补的那部分。"
                          "用户写死了 15 个词，你自己补的部分就只剩 35 个词的额度。\n"
                          "\n"
                          "⚠⚠ 标签格式【不适用「保留全部视觉细节」那一条】。"
                          "中文草稿有三四百字，压进篇幅档给的那几十个词物理上做不到，"
                          "所以这里必须【按优先级取舍】，不是全量转写。\n"
                          "  先保，丢了画面就直接错：\n"
                          "    1. 主体类型 + 数量（solo / 1girl / two women / a lone tree）\n"
                          "    2. 媒介和风格大类（photo / illustration / 3d render / oil painting）\n"
                          "    3. 景别 + 机位\n"
                          "    4. 主体最醒目的 3~4 项：服装或外观主色 + 材质、发型或轮廓、在做什么\n"
                          "    5. 环境，一到两个词组说清在哪\n"
                          "    6. 光线的方向 + 冷暖\n"
                          "    7. 画面质感（film grain / bokeh / sharp focus）\n"
                          "  超了额度就【从下面这些开始砍】，顺序从先砍到后砍：\n"
                          "    纹样和细部装饰 → 远景的陈设物件 → 投影落在哪 → 光比 → "
                          "构图法 → 表情的细微差别 → 五个词以上的长短语\n"
                          "  ⚠ 砍的只能是你自己补的内容。用户写死的部分一律先占位，不参与取舍。",
    "JSON格式要求": ' 输出格式：只输出一个 JSON 对象，不要代码块，形如 {"zh": "中文扩写内容", "en": "英文内容"}',
    "不扩写_中文": "你是 AI 绘画提示词整理师。将用户的描述整理成流畅规范的中文提示词，保持原意，修正语法，不增加内容，用逗号连接短句。",
    "不扩写_英文句子": "You are an AI image prompt translator. Translate the user description into natural English sentences for AI image generation. Do NOT add content beyond what the user specified. Output English only.",
    "不扩写_英文标签": "You are an AI image prompt translator. Translate the user description into English comma-separated tags for SDXL, using standard Stable Diffusion community tags. Do NOT add content beyond what the user specified. Output English only.",
    "负面词": "你是 AI 绘画提示词工程师。用户描述的是画面中不想要的内容。转写为英文负面提示词，逗号分隔短语，只补充与用户意图直接相关的负面词，不堆砌无关的通用词。Output English only.",
    "图像_中文": (
        "你是 AI 绘画提示词提取师。分析图片的全部视觉特征，"
        "生成能复现这张画面的中文提示词。\n"
        "\n"
        "开头第一句（20~30 字）先回答四件事：这是什么图（媒介风格）→ "
        "画的是什么（可数主体写明数量）→ 在哪 → 处于什么状态。\n"
        "然后按【画面维度】里的五层逐层写，并遵守其中\u300c按主体类型走\u300d"
        "和\u300c多主体锚定\u300d的规则 —— "
        "主体是建筑、静物、动物时，不要套人物那套维度。\n"
        "\n"
        "⚠ 和扩写模式的唯一区别：【只写图里真实存在、肉眼可见的东西】。\n"
        "· 看不见的部分（被遮挡的下半身、画面外的鞋子）一律不写，不要脑补。\n"
        "· 不确定的东西要说成不确定：\u300c像是笔记本或平板\u300d、"
        "\u300c木质或深色层压板\u300d。只有确实看清的才写死。\n"
        "· 画面上没有文字就不要编文字；有文字就原样照抄，放进引号里，保持原字种。\n"
        "\n"
        "输出一整段，用逗号连接短句，不要编号、不要小标题、不要解释。"
    ),

    "图像_英文句子": "You are an AI image prompt extractor. Analyze every visual feature and write an English natural-language prompt that could recreate this image.\n\nOpen with ONE sentence (about 20~30 words) answering four things in order: what kind of image this is (medium and style) -> what is depicted (state the count for countable subjects) -> where -> in what state or action.\nThen work through the five layers defined in the shot-dimensions section, and follow its rules for 'dimensions by subject type' and 'anchoring multiple subjects'. If the subject is a building, a still life, an animal or a landscape, do NOT apply the human checklist (facial features, hair, makeup, pose) to it.\n\nThe one difference from expansion mode: write ONLY what is actually there and visible.\n- Anything hidden (an occluded lower body, shoes outside the frame) is left out. Never infer it.\n- State uncertainty as uncertainty: 'what looks like a notebook or tablet', 'wood or dark laminate'. Commit only to what you can genuinely see.\n- If there is no text in the image, do not invent any. If there is, copy it verbatim in quotes and keep the original script.\n\nOutput English only, as flowing prose. No numbering, no headings, no explanations.",
    "图像_英文标签": "You are an AI image prompt extractor. Analyze every visual feature and generate a positive prompt as English comma-separated tags for SDXL.\n\nLead with the subject type and its count (solo / 1girl / two women / a lone tree), then the medium and style class, then work through the five layers defined in the shot-dimensions section. Follow its rules for 'dimensions by subject type' -- if the subject is a building, a still life, an animal or a landscape, do NOT apply the human checklist (facial features, hair, makeup, pose) to it.\nUse standard Stable Diffusion community tags (solo, looking at viewer, upper body, rim light, bokeh, highly detailed). Each tag is 1~4 words. Colors carry a modifier (deep navy, not blue); materials get their own tag (brushed metal, matte plastic).\n\nTake from the five layers ONLY what the tag vocabulary actually has words for: shot type (close-up / full body), camera angle (from below / from above), depth of field (depth of field / bokeh), lighting (rim light / backlit / soft lighting), material, color, style and texture.\nDimensions with no community tag -- lighting ratio, where the shadow falls, composition-rule names, f-numbers and shutter speeds -- are NOT to be forced into strings of words. They do not take effect and only dilute the tags that do.\n\nHARD CEILING. SDXL's CLIP encoder splits the prompt every 77 tokens and encodes each chunk separately, with its own start and end markers, so the chunks cannot see each other. One English word runs about 1.37 tokens, so roughly 50 words is about 70 tokens -- just inside one chunk. Going over costs you twice: words past the first chunk are largely not executed, and worse, a modifier can be split from the word it modifies (deep burgundy at the end of chunk one, satin slip dress at the start of chunk two -- the model can no longer tell what is burgundy). This is the most common cause of attribute bleed. Stay inside the word count given for your detail level.\n\nIf you run out of room, cut by priority. Keep first, in this order:\n  1. subject type + count\n  2. medium and style class (photo / illustration / 3d render / oil painting)\n  3. shot type + camera angle\n  4. the 3~4 most prominent subject traits: main color + material, hair or silhouette, action\n  5. the setting, in one or two tag phrases\n  6. light direction + warm/cool\n  7. image texture (film grain / bokeh / sharp focus)\nCut in this order when over budget: ornament and fine detail -> distant background props -> where the shadow falls -> lighting ratio -> composition rules -> fine shades of expression -> any phrase longer than four words.\n\nDescribe only what is actually visible; never invent. Output the positive prompt only, no explanations, no negative prompts.",
    "图像_负面词": "You are an AI image prompt engineer. Analyze the image, identify visual defects or undesirable elements, and write them as English negative prompt tags separated by commas. Output English only.",
    "图像详细度_不扩写": "详细程度：只写最核心的信息——主体、服装主要特征、景别、整体风格，控制在 15~25 个词或 30~50 字。",
    "图像详细度_简洁": '详细程度：五层都要出现，但每层只取最关键的一两项 —— 拍摄定下景别和机位，主体取该类型最要紧的 2~3 项，环境一句话说清在哪，光线给方向和冷暖，风格一个词。\n篇幅（按你要输出的格式对号入座）：\n  · 中文：60~100 字\n  · 英文自然语言：60~90 词\n  · 英文标签：25~32 词\n宁可少写也不要含糊 —— 写到的每一项都要具体。',
    "图像详细度_适中": '详细程度：五层的主要维度都要覆盖到，每项用一两个短语讲清。\n篇幅（按你要输出的格式对号入座）：\n  · 中文：150~250 字\n  · 英文自然语言：160~230 词\n  · 英文标签：38~45 词\n每一句都要带新信息，不要把同一件事换个说法再说一遍。',
    "图像详细度_详细": '详细程度：五层逐项展开 —— 材质、纹样、新旧状态、细部、光线层次、前中后景的背景元素都要写清楚。\n篇幅（按你要输出的格式对号入座）：\n  · 中文：300~450 字\n  · 英文自然语言：300~420 词\n  · 英文标签：45~52 词\n⚠ 标签那一档【不跟着中文一起膨胀】：SDXL 的 CLIP 每 77 个 token 切一段，一个英文词约合 1.37 token，50 个词刚好在一段之内。超出去的词不但不执行，还会把修饰语和被修饰的词切开，造成属性串位。\n宁可细也不要漏，但仍然【只写图中真实可见的内容】，不确定的要说成不确定。',
    "编辑读图": (
        "【先读图，再动笔】写指令之前，把每张图按这个顺序完整过一遍。"
        "这一步【不输出】，只是让你真正知道图里有什么 —— "
        "没读透就写指令，只会写出\u300c把图1的衣服换成图2的\u300d这种笼统话，"
        "模型照着做必然跑偏。\n"
        "\n"
        "1. 整体：画面类型、媒介、风格、画幅\n"
        "2. 拍摄：景别、机位高度与方位、景深虚化程度。"
        "【迁移进来的元素必须匹配这几项】，否则贴上去一眼就是假的\n"
        "3. 主体：是什么 → 在画面的什么位置 → 【从上到下逐部位看】\n"
        "     人物：头发 → 面部 → 颈肩 → 上身 → 腰胯 → 下身 → 手与脚\n"
        "     物体：顶部 → 主体 → 底部 → 与承托面的接触处\n"
        "     其它主体按同样思路，从一端到另一端走完，不要跳着看\n"
        "4. 细部：材质、纹样、颜色、新旧状态、配饰、小物件、文字\n"
        "5. 环境：【从远到近】—— 背景 → 中景 → 前景；"
        "以及主体和环境的接触关系（阴影落在哪、谁遮住谁、有没有反射）\n"
        "6. 光线：主光方向、软硬、色温，高光和阴影分别落在什么位置\n"
        "\n"
        "读完之后再写指令。指令里提到的每个部位都要【具体到你刚才看到的那个东西】："
        "不是\u300c图1的衣服\u300d，而是\u300c图1中那件米白色蕾丝镶边吊带，"
        "肩带细、裙摆到膝上\u300d。"
    ),

    "编辑_英文": (
        "You are an expert at writing instructions for image editing models (Qwen-Image-Edit, FLUX Kontext). "
        "The user describes what they want to change, usually referring to the pictures as 图1/图2 (Picture 1/Picture 2).\n"
        "After reading every picture in the order above, write ONE clear editing instruction in English:\n"
        "1. Refer to pictures as \"Picture 1\", \"Picture 2\" and so on, matching the given order.\n"
        "2. State exactly what to change, and describe the transferred elements concretely by what you see "
        "(style, material, color, pattern, accessories), not vaguely like \"the clothes in Picture 1\".\n"
        "3. State explicitly what must stay unchanged (face, identity, hairstyle, pose, background, lighting) unless the user wants it changed.\n"
        "4. The blend must match: lighting direction, color temperature, perspective, scale "
        "and depth of field must agree with the target picture. No two light sources at the seam.\n"
        "5. Keep it one coherent instruction, imperative voice. No explanations, no bullet points.\n"
        "Output English only."
    ),
    "编辑_中文": (
        "你是图像编辑模型（Qwen-Image-Edit、FLUX Kontext 等）的指令撰写专家。用户会描述想怎么改图，通常用 图1、图2 指代图片。\n"
        "按上面的顺序把每张图读完之后，写出一条清晰的中文编辑指令：\n"
        "1. 用\"图1\"\"图2\"等称呼图片，与给出的顺序一致。\n"
        "2. 明确要改什么，要迁移的元素按你看到的具体写出来（款式、材质、颜色、纹样、配饰），不要笼统写成\"图1的衣服\"。\n"
        "3. 明确写出必须保持不变的部分（脸、身份特征、发型、姿势、背景、光线），除非用户要改它们。\n"
        "4. 融合必须对得上：光线方向、色温、透视、比例、景深要和目标图一致，"
        "接缝处不能出现两套光。\n"
        "5. 写成一条连贯的祈使句指令，不要解释、不要分点。"
    ),
    "编辑详细度_不扩写": "详细程度：只做必要的规范化，一两句话说清改什么、保留什么，不展开描述细节。",
    "编辑详细度_简洁": '详细程度：简要说明要改什么、改成什么样，保留部分只列最重要的一两项。30~50 词或 50~80 字。',
    "编辑详细度_适中": '详细程度：写清改动涉及的主要特征（款式、颜色、材质，或者要调整的光线、环境、构图），并列出需要保留的部分。50~90 词或 80~150 字。',
    "编辑详细度_详细": '详细程度：逐项展开改动的细节 —— 如果是迁移元素，写清款式、材质、颜色、纹样、配饰、位置关系；如果是删除、替换背景、改光线或改姿态，就写清改前改后各是什么样。完整列出需要保留的部分，并说明融合要自然（光线、比例、透视一致）。90~150 词或 150~250 字。',
    "多图融合": "本次提供了多张参考图，按顺序称为图1、图2……。请按用户文字要求，从各图中提取对应元素，组合成描述**一个统一画面**的提示词；用户没有说明分工时，以图1为主体，其他图作为补充参考（服装、风格、场景等）。不要把多张图分别描述。",
    "随机提示": "补充：请给出与上次不同的具体场景、光线、构图设定，紧扣原意但换一种视觉方案。",
    "英文重试强化": "IMPORTANT: Your previous answer contained Chinese characters. Output MUST be in English ONLY. Do not include any Chinese character.",
}


# 模板版本号。内容改进后在这里 +1，用户 json 里的旧版本会在重启时自动替换。
# 没列在这里的模板一旦写进 json 就永远以用户的为准。
# ⚠ 和视频节点共用同一个「模板版本」字典（VERSION_KEY），两边 key 不能重名。
TEMPLATE_VERSIONS = {
    "画面维度": 2,
    "图像_中文": 2,
    "编辑读图": 1,
    "编辑_英文": 2,
    "编辑_中文": 2,
    "扩写核心": 3,
    "篇幅_简洁": 5,
    "篇幅_适中": 5,
    "篇幅_详细": 6,
    "输出_中文": 2,
    "输出_英文句子": 3,
    "输出_英文标签": 5,
    "图像_英文句子": 2,
    "图像_英文标签": 2,
    "图像详细度_简洁": 2,
    "图像详细度_适中": 2,
    "图像详细度_详细": 2,
    "编辑详细度_简洁": 2,
    "编辑详细度_适中": 2,
    "编辑详细度_详细": 2,
}

VERSION_KEY = "模板版本"


def ensure_templates_file():
    """生成/补全指令模板文件；用户改坏或删除时自动用内置默认值"""
    data = {}
    if os.path.isfile(TEMPLATES_FILE):
        try:
            data = json.load(open(TEMPLATES_FILE, encoding="utf-8"))
        except Exception:
            data = {}
    changed = not os.path.isfile(TEMPLATES_FILE)

    # 哪些带版本的模板本来就不在文件里：它们是新写进去的，不算"升级"，
    # 否则全新安装会误报一堆"已升级"。
    fresh = {k for k in TEMPLATE_VERSIONS if k not in data}

    for k, v in DEFAULT_TEMPLATES.items():
        if k not in data:
            data[k] = v
            changed = True

    # 版本升级：内置版本号比 json 里的新时，替换那一条。
    # 用户自己改过而版本没变的，一个字都不动。
    vers = data.get(VERSION_KEY)
    if not isinstance(vers, dict):
        vers = {}
    upgraded = []
    for k, v in TEMPLATE_VERSIONS.items():
        if k not in DEFAULT_TEMPLATES:
            continue
        try:
            old_v = int(vers.get(k, 1))
        except Exception:
            old_v = 1
        if k in fresh:
            vers[k] = v
            changed = True
        elif v > old_v:
            data[k] = DEFAULT_TEMPLATES[k]
            vers[k] = v
            changed = True
            upgraded.append(k)
        elif vers.get(k) != v:
            vers[k] = v
            changed = True
    if vers != data.get(VERSION_KEY):
        data[VERSION_KEY] = vers
        changed = True
    if upgraded:
        print("[高级提示词] 翻译指令模板已升级到新版：" + "、".join(upgraded))

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
        parts.append(tpl["编辑读图"])
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
        if actual != MODE_NEG:
            parts.append(tpl["画面维度"])
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
        parts.append(tpl["画面维度"])
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
                        "\n"
                        "⚠ SDXL / Pony / Illustrious 的编码器【完全看不懂中文】，"
                        "给它们用必须选「英文标签」。选中文喂进去等于喂噪声，出图基本随机。\n"
                        "⚠ 多个外观不同的主体（如“4个人各穿各的”），SDXL 这代架构做不到，"
                        "描述会糊成一个人。这种需求用 Qwen-Image / FLUX，"
                        "或者配区域提示词、ControlNet。\n"
                        "\n"
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

        # 编辑指令必然要引用「图1」「图2」。一张图都没有时，AI 只能凭空编造
        # 这些图里有什么，输出百分百是假的 —— 宁可报错也不能交出去。
        # 最常见的原因：图片节点被 Bypass 了，但目标格式还停在编辑指令。
        if 目标格式 in EDIT_MODES and not has_image:
            msg = ("「" + 目标格式 + "」需要接入图片，现在一张图都没有。\n"
                   "检查是不是把「加载图像」节点 Bypass／Mute 了，或者线没接上。\n"
                   "没有图的话，AI 只能凭空编造「图1」「图2」里有什么，"
                   "写出来的编辑指令看着像真的，实际和你的图毫无关系。")
            write_error_log(msg)
            raise RuntimeError(msg)
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
