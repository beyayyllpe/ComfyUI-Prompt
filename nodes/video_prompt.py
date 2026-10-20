"""
🎬 视频提示词节点
菜单：自定义 → 高级提示词 → 视频提示词

设计原则：用户只说人话，专业判断交给 AI，但判断规则写死在指令里。
· 五个拍摄开关默认全是「交给AI」——运镜、光线、表情、台词都由它按内容决定
· 判断规则不是放任：什么内容配什么运镜、光变必须有光源解释、
  情绪转变要有触发点，这些都在导演守则里写死了
· 用户想干预时才去拨开关，拨了就是硬命令，AI 不许改
· 设置不合理时直接算出具体数字提醒（"每镜 1.5 秒，人脸会一镜一个样"），
  不用用户学任何新概念

· 目标模型由 ai_prompt_templates.json 的「视频模型规范」驱动，加一段就多一个选项
· 五种模式自动判断：文生 / 首帧 / 首尾帧 / 尾帧 / 参考素材
· 锁定要求全部用大白话填写，留空＝交给 AI
· 同时输出「提示词」（给模型）和「中文说明」（完整对照译文 + 决策回执）
"""

import json

# ComfyUI V3 schema。参考图用它的 io.Autogrow 实现动态增减——
# 和官方 MiniMaxH3ReferenceToVideo 同一套机制，由前端原生处理，
# 不会像自己在 JS 里 addInput() 那样触发重排把节点撑高。
try:
    from comfy_api.latest import io
except ImportError as _e:  # ComfyUI 太旧，没有 V3 schema
    raise ImportError(
        "视频提示词节点需要支持 V3 schema 的 ComfyUI（官方 MiniMax H3 节点同样需要）。"
        "请更新 ComfyUI 后重试。原始错误：" + str(_e)
    )

from _shared import (
    TEMPLATES_FILE, ERROR_LOG,
    Log, HAS_CN, advice, write_error_log,
    get_all_providers, _provider_names, load_key,
    call_ai, fetch_models, pick_fallback, _fallback_cache,
    img_to_b64, clean_text, join_parts,
)

# ─── 默认模板（可被 ai_prompt_templates.json 覆盖）─────────────────────────────

VIDEO_TEMPLATES = {

    # ===== 共用：AI 的职责范围 =====
    "视频_导演守则": (
        "你是资深影视导演兼提示词工程师，为一个不懂镜头语言的用户写视频提示词。\n"
        "\n"
        "【你的职责只有三件事】\n"
        "1. 读懂用户给的参考图，如实复述画面里已经存在的东西。\n"
        "2. 把用户的大白话翻译成专业的拍摄描述"
        "（\u300c镜头别动\u300d译成 holds a static shot，\u300c跟着她走\u300d译成 tracking shot）。\n"
        "3. 把用户没提到的格子补齐，按目标模型的官方格式排版输出。\n"
        "\n"
        "【关于标\u300c你来定\u300d的项目】\n"
        "拍摄设置里标了\u300c你来定\u300d的，是用户把判断权交给了你。"
        "这时候你是真的在当导演，不是在填表：\n"
        "· 先问\u300c这个场景需要吗\u300d，不要问\u300c这样会不会更丰富\u300d。"
        "需要才给，不需要就不给——克制本身就是专业判断。\n"
        "· 每一个你决定加上的变化，心里都要能答出\u300c它在为什么服务\u300d。"
        "答不出来的，就是多余的。\n"
        "· 变化要有因果：先有事发生，才有人反应。"
        "表情转变压在触发点后面半拍，光线变化要有光源上的解释，"
        "切镜头要有换视角的必要。无因无果的变化，一眼就看得出假。\n"
        "· 拿不准时选少的那个。少一个变化顶多平淡，多一个变化可能让整条片子崩掉。\n"
        "\n"
        "【运镜怎么选】标了\u300c你来定\u300d时按这张表对照，从内容推手法：\n"
        "· 人基本不动、靠表情和细微动作传情绪 → 固定机位。"
        "镜头一动就抢戏，观众注意力会从脸上被拉走。\n"
        "· 人在走、跑、移动 → Tracking Shot 跟随，保持她在画面里的相对位置。\n"
        "· 要从环境切进人物内心 → Push In 推近。越近越私密，这是\u300c进入\u300d的语法。\n"
        "· 要从人物退回她的处境 → Pull Back 拉远，交代她身处什么境地。\n"
        "· 要看清空间的立体关系，或制造不安 → Arc Shot 环绕。\n"
        "· 突然的发现、惊吓 → 快速 Push In，模拟瞳孔收缩那一下。\n"
        "\n"
        "⚠ 压在这张表之上的一条铁律：【默认不动，运动是要挣来的】。\n"
        "  专业片子里大量镜头本来就是固定的，动起来的是少数。"
        "而且对视频模型来说，人在动、镜头也在动，两套位移叠加，"
        "是画面崩坏和人物漂移的主要来源。\n"
        "  每一个你决定动起来的镜头，都要能答出\u300c不动会少掉什么\u300d。"
        "答不出来就别动。\n"
        "\n"
        "【光什么时候该变】标了\u300c你来定\u300d时，唯一的判断标准是"
        "【这个变化在物理上说得通吗】：\n"
        "· 说得通的：开灯关灯、车灯／警灯扫过、云遮住太阳、人走进或走出阴影、"
        "火光、闪电、屏幕或手机的光打在脸上。\n"
        "· 说不通的：\u300c情绪到了所以光变暖\u300d\u300c为了更有电影感\u300d、"
        "没有任何光源地凭空换色。\n"
        "  情绪转折可以【配合】光变，但光变本身必须有物理来源 —— "
        "不能因为她伤心了就把光调暗。\n"
        "  找不到来源就全程不变，这不是偷懒，是对的。\n"
        "\n"
        "【让画面活起来】\n"
        "· 写看得见的具体动作，不写笼统状态："
        "不要写 she dances gracefully，要写重心怎么移、哪只手先抬、裙摆往哪边甩。\n"
        "· 给身体留呼吸：真人不会一刻不停地动。"
        "动作之间要有停顿、有重心转移、有一次吸气——这些空隙才让人显得是活的。\n"
        "· 让环境跟着动：头发、衣料、流苏、耳环会随动作晃，光会落在皮肤和布料上。"
        "这些次级运动是\u300c真实感\u300d的来源。\n"
        "· 情绪要落在身体上，不要只写在脸上："
        "紧张是肩膀收紧，放松是下巴抬起来——这比写 nervous 有用得多。\n"
        "\n"
        "【不属于你的职责】\n"
        "· 不要改动\u300c本次拍摄设置\u300d里标【必须】的项，那是用户在节点上定好的。\n"
        "· 标【必须】的项：没让你切镜头就不许切，没让你动镜头就不许动，"
        "没让你变光就不许变。\u300c这样更有电影感\u300d不是加东西的理由。\n"
        "· 不要用自己的想法替换用户写死的要求。\n"
        "· 不要擅自改动官方格式：字段名、标签、固定措辞一个字都不能变。\n"
        "\n"
        "【写法原则】\n"
        "1. 用户写到的主体、动作、情绪、场景必须全部保留并作为核心。\n"
        "2. 每一处只给一种明确方案，不要写\u300c或者\u300d\u300c可以是\u300d。\n"
        "3. 每一个细节都必须对应画面上看得见、或声音里听得见的东西。\n"
        "4. 用动词不用形容词：写 unfolds、snaps、drifts，不要写 modern、sleek、beautiful。\n"
        "5. 动作要符合物理规律。一个镜头里只安排一个主要动作，多了画面会崩。\n"
        "6. 信息密度比长度重要，不要为了显得详细而灌水。\n"
        "7. ⚠ 不要用抽象的评价词。cinematic、beautiful、stunning、epic、high quality 这类词"
        "只能出现在 [Shot 1] 开头的风格声明里（如 Live-action, cinematic, ...），"
        "正文其余部分一律换成具体的画面和声音细节。\n"
        "8. ⚠ 正文是逐秒看得见听得见的拍摄描述，不是剧情梗概。"
        "不要写\u300c她回忆起往事\u300d\u300c两人关系变得紧张\u300d这种镜头拍不出来的东西，"
        "要写她做了什么动作、脸上什么变化、听见什么声音。\n"
        "9. ⚠ 用到的每一个引用标签（<Subject N>、<Picture N>、<Video N>、<Audio N>）"
        "都必须先在定义段里出现过，不许凭空使用没定义的编号。"
    ),

    # ===== 共用：三十格骨架（内部检查清单，不是输出格式）=====
    "视频_骨架": (
        "【拍摄清单的三十格】这是你写之前要在心里过一遍的检查清单，"
        "不是输出格式——输出一律按目标模型的官方格式。\n"
        "用户没说的你来补，但一格都不能漏掉不想。\n"
        "\n"
        "全片层：1 时长　2 画幅　3 媒介与质感　4 风格锚定　5 色板影调\n"
        "        6 身份锁定　7 连贯性约束　8 声音总基调　9 排除项\n"
        "镜头层·怎么拍：10 景别　11 机位高度　12 机位方位　13 焦距感\n"
        "               14 构图留白　15 景深焦点　16 运镜\n"
        "镜头层·拍什么：17 场景与时间　18 主体　19 走位　20 动作\n"
        "               21 表演与眼神　22 服装道具　23 光线\n"
        "镜头层·听什么：24 台词　25 画面内音效　26 环境底噪　27 配乐\n"
        "镜头层·怎么接：28 转场　29 节奏\n"
        "另外：         30 画面内文字（没有就不写，有就必须用英文双引号原样打出）"
    ),

    # ===== 共用：大白话 → 专业词 =====
    "视频_大白话翻译": (
        "用户只会用大白话。把他的话翻译成官方词表里的专业表达，"
        "不要原样照抄，也不要擅自升级成别的意思。常见对照：\n"
        "· \u300c镜头别动\u300d\u300c不要晃\u300d → the camera holds a static shot\n"
        "· \u300c慢慢靠近\u300d → the camera pushes in with small amplitude at slow speed\n"
        "· \u300c快速拉远\u300d → the camera pulls out with large amplitude at fast speed\n"
        "· \u300c跟着她走\u300d → the camera follows her in a tracking shot\n"
        "· \u300c绕着拍\u300d → the camera moves around her in an arc shot\n"
        "· \u300c平移过去\u300d → the camera trucks right\n"
        "· \u300c镜头转过去看\u300d → the camera pans right\n"
        "· \u300c从上往下移\u300d → the camera pedestals down\n"
        "· \u300c抖一下\u300d → the camera shakes slightly\n"
        "· \u300c她的视角\u300d → a POV shot\n"
        "· \u300c从背后拍\u300d → framed from behind, her back to the camera\n"
        "· \u300c只拍脸\u300d → a close-up on her face\n"
        "· \u300c把全身拍进去\u300d → a wide shot framing her full body\n"
        "· \u300c背景糊掉\u300d → shallow depth of field, the background falls out of focus\n"
        "· \u300c显得她很强\u300d → a low-angle shot from below her eye line\n"
        "· \u300c显得她很小\u300d → a high-angle shot looking down at her\n"
        "· \u300c昏暗\u300d → low-key lighting with deep shadows from a single source\n"
        "· \u300c像傍晚\u300d → warm low-angle golden light from the side\n"
        "· \u300c像老电影\u300d → vintage film style with heavy grain and a muted palette\n"
        "· \u300c看着镜头\u300d → she looks directly into the lens\n"
        "遇到对照表里没有的，按同样思路翻译：说清摄影机看到什么，不要保留评价性词汇。"
    ),

    "视频_运镜术语表": (
        "【官方运镜词表】只能用下面这些运动类型，不要自创术语"
        "（dolly、crane、orbit、handheld、rack focus 都不是官方词）：\n"
        "Zoom In / Zoom Out —— 机身不动，焦距变化\n"
        "Push In / Pull Out —— 摄影机前进／后退\n"
        "Pan Left / Pan Right —— 机位不动，镜头水平转\n"
        "Truck Left / Truck Right —— 摄影机水平平移\n"
        "Tilt Up / Tilt Down —— 机位不动，镜头垂直转\n"
        "Pedestal Up / Pedestal Down —— 整个摄影机升／降\n"
        "Arc Shot —— 绕主体弧形移动　　Tracking Shot —— 跟随移动的主体\n"
        "Static Shot —— 机位和镜头都不动\n"
        "Shake Slightly / Shake Strongly —— 轻微／强烈晃动\n"
        "POV —— 主体的主观视角\n"
        "Roll Clockwise / Roll Counterclockwise —— 绕镜头轴顺／逆时针旋转\n"
        "\n"
        "幅度：with small amplitude / with large amplitude（中等幅度省略不写）\n"
        "速度：at slow speed / at fast speed（正常速度省略不写）\n"
        "格式＝运动类型 + 幅度 + 速度，只在有意义时才加幅度和速度。\n"
        "\n"
        "⚠ 运镜必须写成镜头内一句自然的英文动作句，不能当标签堆在句尾：\n"
        "  对：The camera pushes in with small amplitude at slow speed toward the folded letter in her hands.\n"
        "  对：The camera holds a static shot as the runner exits the frame.\n"
        "  错：... , static shot, no movement.\n"
        "\n"
        "景别参考（写进句子里，不是字段）：extreme close-up / close-up / medium shot /\n"
        "medium-wide shot / wide shot；机位高度 low-angle / eye level / high-angle / overhead。"
    ),

    # ===== 共用：声音 =====
    "视频_声景守则": (
        "overall_soundscape 的写法（官方规则）：\n"
        "1. 用 1~4 句英文，写成【一个连续的段落】，不要分行、不要列点。\n"
        "2. 内容只包括三类：环境音、物理动作音、非语言的人声"
        "（风、雨、车流、脚步、衣料、撞击、呼吸、笑声、喘息）。\n"
        "3. 台词、唱歌、画内音乐不写在这里——它们属于正文描述。\n"
        "4. 只有用户明确要求全程静音时才写 N/A。\n"
        "例：Steady rain taps against the caf\u00e9 windows while low room ambience continues underneath. "
        "The entrance bell rings once, followed by wet footsteps and the soft scrape of a chair."
    ),
    "视频_配乐守则": (
        "non_diegetic_music 的写法（官方规则）：\n"
        "1. 用 1~3 句英文，只写角色听不见、只有观众听得见的背景配乐。\n"
        "2. 只写【配器、速度、节奏、力度变化】。\n"
        "3. ⚠ 不要用抽象的情绪词，不要解释这段音乐起什么情感作用。\n"
        "   错：tense and mysterious score that builds emotional tension\n"
        "   对：Sparse piano notes at a slow tempo, joined by sustained low strings "
        "that gradually increase in volume before fading out.\n"
        "4. 角色听得见的歌声、乐器、收音机、电视、手机音乐都是画内声音，写进正文，不写这里。\n"
        "5. 没有配乐就写 N/A。"
    ),

    # ===== 共用：镜头与切点 =====
    "视频_镜头规则": (
        "【镜头与切点】（官方规则，一个字都不要改）\n"
        "1. 第一个镜头写 [Shot 1]，【不加时间戳】。\n"
        "2. 之后每个镜头写成 [Shot N] At MM:SS.mmm, ... ，"
        "切点时间严格递增且落在总时长之内。例：[Shot 2] At 00:03.500, the camera cuts to...\n"
        "3. 普通切换用这几种措辞之一：the camera cuts to / the shot cuts to / "
        "the shot transitions to / the shot changes to / the shot switches to。\n"
        "   只有用户明确要求时才用 cross-dissolve、fade、wipe。\n"
        "4. 切镜头的唯一理由是引入新信息（新主体、新空间、新状态、新视角、新时间）。"
        "只是想改变距离或微调角度，用运镜解决，不要切。\n"
        "5. 风格写在 [Shot 1] 正文开头，不要单独开一个区块。\n"
        "   例：[Shot 1] Live-action, cinematic, a medium-wide shot frames..."
    ),

    # ===== 共用：说话人与台词 =====
    "视频_对白": (
        "【说话人与台词】（官方规则，标签和措辞不许改）\n"
        "1. 会说话、唱歌或发出画外人声的主体，用稳定 ID：(S1)、(S2)。"
        "多人同时说用复合 ID：(S1,S2)。同一个人跨镜头保持同一个 ID。"
        "从不发声的角色不给 ID。\n"
        "2. 说话人第一次出现时，要交代足以建立稳定身份的信息："
        "角色类型、年龄、性别、是否在画面内、音高、音色、语速、口音。\n"
        "3. 身份描述、ID、动作、语气都写在 <d> 外面；<d> 里面只放语言标签和用户给的原话。\n"
        "   原话的每一个字和标点都要原样保留，不翻译、不改写。\n"
        "   例：The young woman with a quiet, breathy voice (S1) says: "
        "<d>[Chinese] \u4e0b\u4e00\u7ad9\u6211\u5c31\u4e0b\u8f66\u3002</d>\n"
        "4. 画外音必须用固定措辞 says in an off-screen voiceover，"
        "并在每个画外音 <d> 之后紧接着说明对应角色的嘴唇保持闭合。\n"
        "   例：The man (S1) says in an off-screen voiceover: <d>[English] I still remember that road.</d> "
        "while his lips remain completely closed.\n"
        "5. 同一句台词跨越切点时，在两段的衔接处都写 <scenetrans>，"
        "并明确说明声音跨切点继续（continues seamlessly across the cut 等）。\n"
        "6. 台词被视频结尾截断时写 <cutoff>。\n"
        "7. 台词时长要与镜头长度匹配，中文一秒约 3~4 个字。"
    ),

    "视频_画面文字": (
        "【画面文字】画面上真实可见的横幅、招牌、标签、字幕、霓虹字，"
        "一律用英文双引号括起来，原文原标点照抄，不翻译。\n"
        "例：A red neon sign reading \"\u8425\u4e1a\u4e2d\" glows above the doorway.\n"
        "画面上没有文字就不要提这件事，不需要专门写一句\u300c没有文字\u300d。"
    ),

    # ===== 共用：模式 =====
    "视频模式_文生视频": (
        "本次是【T2VA 文生视频】：没有参考图，所有格子都由你设计。\n"
        "不需要写图像对齐指令，直接从正文第一个字段开始。\n"
        "在 [Shot 1] 正文开头先定下整体风格和初始构图"
        "（常用风格词：Cinematic、live-action、2D-animated、3D CG、claymation、watercolor、vintage film）。"
    ),
    "视频模式_首帧": (
        "本次是【I2VA 首帧生视频】：<Picture 1> 就是视频 0.00 秒的真实第一帧，属于 [Shot 1]。\n"
        "\n"
        "推荐结构：首帧锚定 → 动作起始 → 连续发展 → 结果或反应。\n"
        "（这是【开场段】的结构。如果本段是续写，见下面的\u300c续写起手\u300d，那里的规则优先。）\n"
        "先在 [Shot 1] 开头确立图中的风格、主体、构图和场景锚点，再描述接下来发生什么。\n"
        "人物身份、服装、颜色、关键物件和空间关系必须保持一致。\n"
        "\n"
        "⚠ 这些格子已经被图片拍板了，只能如实复述并保持，绝对不许写成别的值：\n"
        "  画幅、媒介质感、色板、身份（脸／发型／服装）、场景、光线、\n"
        "  以及第一个镜头的景别／机位高度／机位方位／构图／景深。\n"
        "写了和图不符的内容，模型会在前几帧硬改画面，造成闪烁和人物漂移。\n"
        "如果用户的要求和图片里的事实冲突（图里是白裙子，用户写红裙子），"
        "以图片为准，并在中文说明里明确告诉用户这条冲突被忽略了。\n"
        "\n"
        "你真正要设计的只有：画面接下来怎么动（运镜、走位、动作、表演）、声音、节奏。\n"
        "动作从图中当前姿态自然延续，只用图里已有的元素，不要新增人物或物体。"
    ),
    "视频模式_首尾帧": (
        "本次是【FL2VA 首尾帧生视频】：Picture 1 是开头，Picture 2 是结尾。\n"
        "\n"
        "推荐结构：首帧状态 → 可观察的中间变化 → 差异逐步收窄 → 尾帧状态。\n"
        "正文【不要变成两张静态图的描述】，要写出连接两者的运动路径："
        "主体怎么移动、姿态怎么变、物件怎么被操作、构图怎么演进、场景或光线怎么过渡。\n"
        "最后一帧必须由最后一个 [Shot N] 在视频结尾处抵达。\n"
        "\n"
        "⚠ FL2VA 原则上用单个镜头，好让模型从首帧连续插值到尾帧；"
        "只有用户明确要求时才分镜。"
    ),
    "视频模式_尾帧": (
        "本次是【L2VA 尾帧生视频】：<Picture 1> 是视频的最后一帧，属于最后一个 [Shot N]，"
        "它不天然属于 Shot 1。\n"
        "\n"
        "推荐结构：合理的先前状态 → 明确的动作与过渡路径 → 在最后一个镜头里逐步收敛 → 落在尾帧。\n"
        "根据用户意图和这张尾帧，反推一个说得通的更早状态，"
        "再描述人物、物件、摄影机和场景如何逐步逼近这张参考图。"
    ),
    "视频模式_参考素材": (
        "本次是【全参考模式 Ref2VA】：给你的图片不是画面，是素材库。\n"
        "\u300c参考图1\u300d对应 <Picture 1>，\u300c参考图2\u300d对应 <Picture 2>，编号严格一一对应。\n"
        "\n"
        "参考图给的是\u300c这个人长什么样\u300d\u300c这个场景长什么样\u300d，"
        "不是\u300c第一帧长这样\u300d。所以景别、机位、构图、景深、走位全部由你自由设计，"
        "不受素材本身构图的限制。\n"
        "\n"
        "⚠ subject_definitions 里的每一条，必须如实描述你在那张图里【真正看见】的东西："
        "真实的服装款式和颜色、真实的场景、真实的长相。\n"
        "绝对不要照抄用户想法里的词，更不要编造图中没有的衣服、场景或人物。"
        "用户那句话只说明\u300c从哪张图取什么\u300d，不说明\u300c那张图长什么样\u300d——"
        "长什么样只能由你看图得出。\n"
        "如果你看不到图片，立刻在 summary 里写一句 "
        "\u300cNOTE: reference images were not visible\u300d，不要靠猜编一套出来。\n"
        "\n"
        "先逐张看图，把看到的内容抽象成 <Subject N>：这个人的长相发型服装、"
        "这个场景的陈设光线、这件道具的材质。用户在\u300c你的想法\u300d或\u300c我的具体要求\u300d里指定了"
        "\u300c只取什么、不沿用什么\u300d的，以他写的为准；没指定的由你判断。\n"
        "\n"
        "⚠ 身份锁定是本模式最重要的一格，而且必须【每个镜头重复引用一次】——"
        "只在开头声明一次是不够的，中途会漂移。\n"
        "\n"
        "输出格式与其它四种模式【完全不同】，见下方六段结构说明。\n"
        "本模式不需要图像对齐指令那一行。"
    ),

    # ===== 全参考模式：六段结构 =====
    "视频_全参考格式": (
        "【全参考模式的六段输出结构】按顺序写满六段，段名原样保留，全部用英文：\n"
        "\n"
        "subject_definitions:\n"
        "  定义每一份后面需要单独追踪的被引用内容，一行一条，说清这个标签指什么、"
        "担任什么引用角色、要遵循哪些主要特征。四种标签：\n"
        "  <Subject N> 可复用的可见内容（人、动物、物件、场景、服装、道具、风格、动作、表情、姿势）\n"
        "  <Picture N> 作为具体目标帧或分镜锚点的参考图\n"
        "  <Video N>   提供剪辑源、续写起点或整片时间结构的参考视频\n"
        "  <Audio N>   被复制或被参考的音频\n"
        "  例：<Subject 1> is the young woman in <Picture 1>, with long dark hair and a blue cardigan.\n"
        "  ⚠ 一张图如果只是用来定义角色／场景／服装／风格，不要单独开 <Picture N> 条目，"
        "把图源写进对应的 <Subject N> 定义里。\n"
        "\n"
        "summary:\n"
        "  一小段英文，开头是方括号任务类型前缀，多个类型用 + 连接且不重复。\n"
        "  可选类型：keyframe completion（图作为具体帧锚点）、reference generation（提供生成引导）、\n"
        "  video editing（直接改源视频）、video continuation（从源视频续写）、\n"
        "  audio reuse（音频信号被复用）、audio reference（只参考风格音色内容）。\n"
        "  例：[reference generation + audio reference] The target video shows <Subject 3> ...\n"
        "  只能使用上面已定义的标签，不要在这里引入新标签。\n"
        "\n"
        "retention_analysis:\n"
        "  每个标签一行，写明它在哪些镜头出现、保留关系如何。关系标记是固定英文值：\n"
        "  可见内容：fully_preserved / partially_preserved / attribute_transfer / weak_reference\n"
        "  音频：    fully_copy / partially_copy / reference / weak_reference\n"
        "  例：<Subject 1> (appears in [Shot 1], [Shot 3]): fully_preserved - ...\n"
        "  ⚠ 这一段里不要写 (Sx) 说话人 ID。\n"
        "\n"
        "detailed_description:\n"
        "  正文主体，按播放顺序逐镜头描述画面、动作、镜头、声音、台词，"
        "并在引用关系生效处插入标签。\n"
        "  ⚠ 与其它模式的区别：风格用一到两句英文写在 [Shot 1] 【之前】，不是写在 [Shot 1] 里面。\n"
        "  重要的 <Subject N> 首次清晰出现时，描述它被引用的特征、在画面里的位置和当前动作；"
        "后续镜头继续用同一个标签，不要重新定义。\n"
        "  具体帧锚点用自然措辞：the shot begins from <Picture 1> / "
        "the shot ends on <Picture 3>。\n"
        "  被引用主体说话时，视觉标签和说话人 ID 都要保留：<Subject 2> (S1) says, <d>...</d>\n"
        "  生成类任务正文通常 350~500 个英文词。\n"
        "\n"
        "overall_soundscape:\n"
        "non_diegetic_music:\n"
        "  规则与其它模式相同。"
    ),

    # ===== 共用：用户钉死的要求 =====
    "视频_锁定要求": (
        "【用户钉死的要求】下面这些是用户自己写的，必须原样落实：\n"
        "· 不许改写成你认为更好的处理，不许用近义的做法替换。\n"
        "· 用户用大白话写的，你负责翻译成官方词表里的专业表达，但意思一个字都不能变。\n"
        "· 如果某条和上面的拍摄设置冲突，以用户写的这条为准，"
        "并在中文说明里说清楚哪一条覆盖了哪个设置。\n"
        "· 用户没写到的格子，照常由你补齐。\n"
        "· ⚠ 用户在这里明确要的东西，优先级【高于】后面那份默认的负面清单。比如用户说要叠化转场，就照给，不要因为默认清单里写了\u300c不要叠化\u300d而拒绝。"
    ),

    "视频_负面默认": (
        "不要柔和叠化或变形融化的转场，不要额外字幕或说明文字，不要画面撕裂和黑帧，"
        "不要明显的合成接缝，不要多余的肢体或手指，不要扭曲的手，不要乱码文字和水印，"
        "不要曝光闪烁，不要人物中途变样"
    ),
    "视频_负面写法": (
        "【关于排除项】官方格式里【没有】独立的负面列表区块，所以不要自己造一个 [Exclude] 段。\n"
        "正确做法是把\u300c不要什么\u300d翻译成\u300c是什么\u300d，直接写进正文的肯定描述里。\n"
        "  例：\u300c不要柔和叠化\u300d → 在切点处明确写 the shot cuts to（硬切）\n"
        "  例：\u300c不要人物变样\u300d → 在镜头里明确复述她的外貌并写 her appearance remains consistent\n"
        "  例：\u300c不要其他人入镜\u300d → 明确写 she is the only person in the frame\n"
        "实在无法转成肯定描述的，才在正文主体字段的末尾"
        "用一句否定句收尾，不要分行列点，不要加段名。\n"
        "另外，针对本片内容自己补 2~4 条具体的排除项，具体的比通用的管用得多。"
    ),

    # ===== 共用：输出 =====
    "视频_输出要求": (
        "输出格式：只输出一个 JSON 对象，不要代码块，不要任何额外文字，形如\n"
        '{"prompt": "给模型的提示词", "zh": "中文对照译文＋决策回执"}\n'
        "\n"
        "prompt 字段：严格按上面目标模型的官方格式写成的提示词，直接喂给视频模型。\n"
        "段名、标签、固定措辞一个字都不能改，段与段之间空一行。\n"
        "\n"
        "zh 字段必须是【一个字符串】，不是对象、不是数组。\n"
        "⚠ 下面说的\u300c三部分\u300d指的是这一个字符串【内部】依次写三段，中间空一行分隔。\n"
        "  不要写成 {\"镜头清单\": ..., \"完整译文\": ...} 这样的嵌套对象。\n"
        "三段依次是：【镜头清单】、【完整译文】、【我替你补了什么】。\n"
        "\n"
        "第一部分\u300c镜头清单\u300d —— 写在最前面，让用户一眼看完整条片子的节奏。\n"
        "每个镜头一行，严格按这个格式，列对齐：\n"
        "镜头清单\n"
        "[1] 00:00.000-00:02.500  中景·固定机位    她发现画外目标，表情从平静转为警觉\n"
        "[2] 00:02.500-00:05.000  中全景·固定机位  跳下车，挂回手铐，向前冲刺\n"
        "规则：\n"
        "· 时间段按你在 prompt 里写的切点算，最后一个镜头到视频结尾。\n"
        "· 中间一列写\u300c景别·运镜\u300d，用中文，例如 特写·推近、全景·跟随。\n"
        "· 最后一列用一句话说这一镜发生了什么，不超过 25 个字。\n"
        "· 只有一个镜头时也要写这张表，只是只有一行。\n"
        "· 写完空一行，再开始第二部分。\n"
        "\n"
        "第二部分\u300c完整译文\u300d：把 prompt 字段【完整逐句翻译成中文】，供用户核对。\n"
        "1. 不要概括、不要摘要、不要省略任何一句，prompt 有几段就有几段，一一对应。\n"
        "2. 英文字段名和段名、镜头标记 [Shot N]、时间戳、"
        "以及 <Subject N>、<Picture N>、(S1)、<d>、<scenetrans> 这类标签，一律原样保留不翻译。\n"
        "3. 保持原有的分段和顺序，让用户能左右对照着看。\n"
        "4. 专业术语照常译出，后面紧跟括号用大白话解释效果，例如：\n"
        "   pushes in（镜头缓慢靠近主体，让人注意力集中到她身上）\n"
        "   holds a static shot（机器完全不动，画面稳）\n"
        "   同一个术语只解释一次，后面重复出现时不再加括号。\n"
        "5. <d> 里的台词和画面内文字保留原文，后面括号注明中文意思。\n"
        "\n"
        "第三部分\u300c我替你补了什么\u300d，前面加一行 \u300c—— 我替你补了什么 ——\u300d，写 3~6 条，每条一行：\n"
        "· 用户没说、由你决定的关键格子，各写一条（选了什么、为什么）\n"
        "· 如果用户的要求和参考图冲突、或覆盖了拍摄设置，必须单列一条说明\n"
        "· 不要列用户自己写死的东西，那些不是你补的"
    ),

    # ===== 视频风格预设（下拉框内容由此生成；提炼自 MiniMax 官方风格技能）=====
    "视频风格预设": {
        "不使用": {
            "版本": 1,
            "说明": "不注入任何风格约束，风格由你的想法和你的想法决定",
            "约束": "",
        },
        "3D动画（皮克斯风）": {
            "版本": 1,
            "说明": "C4D+Octane 质感的三维卡通，夸张表演、弹性运动",
            "约束": (
                "【风格锁定：皮克斯风 3D 卡通】\n"
                "渲染：Pixar-inspired 3D cartoon rendering, C4D and Octane renderer look, "
                "high-end animated feature quality.\n"
                "角色设计：夸张的几何化简约 + 高质量材质细节。避免 100% 写实人体解剖，"
                "用高度概括的形体语言、大而清晰的剪影；可爱或童稚角色常用 2.5~3 头身，"
                "大头、紧凑身体、高辨识度。\n"
                "毛发：结实的雕塑感发块 + 边缘细碎飞发和绒毛轮廓，受光时既有设计感又有触感。\n"
                "皮肤：warm subsurface scattering，耳朵、脸颊、鼻尖、指尖透出柔和的红色透光；"
                "绝不要硬塑料质感的皮肤。\n"
                "表演：迪士尼／皮克斯式夸张生动的角色动画，squash and stretch，"
                "眉毛、眼角、瞳孔、嘴唇、脸颊形变都要有戏。\n"
                "运动：高能量姿态、清晰的动作线、前倾、强预备动作、快而可读的节奏、"
                "弹性身体力学、生动的微表情。\n"
                "负面：no photorealistic live-action, no flat 2D anime, no plastic toy skin, "
                "no stiff mannequin posing, no realistic anatomical stiffness, no lifeless facial expressions."
            ),
        },
        "手绘发光×实拍融合": {
            "版本": 1,
            "说明": "粗糙手绘发光动画出现在实拍空间里，接触、变形、逃跑，镜头慢半拍追",
            "约束": (
                "【风格锁定：手绘发光动画 × 实拍融合】\n"
                "影像结构必须保留：实拍空间中出现平面的手绘发光动画；"
                "动画与真实的手或真实物体发生清晰接触；"
                "它作为同一个实体连续变形并逃跑；手持镜头总是慢半拍追赶。\n"
                "质感：像蜡笔、粉笔、彩色铅笔、粉彩、粗糙笔刷；线条轻微抖动，"
                "有涂抹不均、毛边和逐帧重画的手工感。\n"
                "开场 0~3 秒必须出现实拍的手与手绘动画的清晰接触"
                "（缠住手指、落在掌心、被抓时逃跑、从指尖诞生）。\n"
                "变形要连续：可在线条、生物、记号、植物、交通工具、生活小物之间变化，"
                "但要保留前一形态的痕迹，不允许突然冒出另一个全新角色。\n"
                "全片在同一空间或相邻范围内连续展开，像拍摄者边走边追，不要跳到别的地点。\n"
                "拍摄者要参与：伸手、抓、追、开门、接住、后退、被恶作剧。\n"
                "调性是可爱、生活感、怀旧、温柔，略带切感，不是恐怖喜剧。\n"
                "结尾要有空间级变形：线条扩散到墙面、地板、天花板、窗户，"
                "变成巨大的花、星空、夕阳、云或涂鸦小镇，留一点余韵和可爱笑点。\n"
                "负面：no 3DCG, no plush toy look, no uniform vector lines, no smooth neon, "
                "no horror monster, no giant eyes, no gaping mouth, no teeth, no threatening lunge, "
                "no sudden blackout, no jump scare."
            ),
        },
        "纸艺定格（立体纸偶）": {
            "版本": 1,
            "说明": "层叠硬纸、纸偶、立体书场景，逐帧手工摆拍的停格动画",
            "约束": (
                "【风格锁定：纸艺定格动画】\n"
                "所有可见物体都要像真的用纸做的：层叠硬纸板、裁切边缘、纸纤维、折痕、接缝、"
                "插片、铆钉、关节、拉条、滑块、旋转圆盘，以及看得见的厚度。\n"
                "运动要像逐帧手工摆拍：小步进式移动、细微停顿、轻微回弹、铰链式动作、"
                "纸机关滑动、翻页、拉条揭示、纸片落定。\n"
                "背景也尽量是纸机关：纸云沿轨道滑动、纸月亮在轨道上移动、"
                "层叠景片视差位移、纸叶飘落、纸灯摇晃、纸板门开启。\n"
                "画面层次：用前景遮挡物（纸叶、纸云、画框、帘子、剪影）制造纵深，"
                "物体之间要有真实的纸张投影。\n"
                "负面：no smooth CG motion, no plastic 3D surfaces, no flat vector backdrops, "
                "no static background, no large character motion that breaks the miniature stop-motion feel."
            ),
        },
        "纸拼贴（半调编辑风）": {
            "版本": 1,
            "说明": "平涂色块 + 黑白半调照片剪贴，定格拼装，带纸张音效",
            "约束": (
                "【风格锁定：半调纸拼贴】\n"
                "视觉语言：premium editorial halftone paper collage —— 平涂的大胆色块、"
                "黑白半调照片剪影、选择性的彩色卡纸点缀、温暖的米色描边、柔和的纸张投影。\n"
                "运动语言：触感十足的定格拼装 —— 元素一片片拼上来、落定、轻微回弹。"
                "不要缓慢推镜、不要泛泛的漂移、不要平滑的数字图层位移。\n"
                "声音默认只保留纸张音效：纸片滑动、弹跳、按压、轻微窸窣、轻点，"
                "除非明确要求，不加背景音乐、不加旁白、不加字幕。\n"
                "负面：no smooth digital layer motion, no photographic realism, no 3D rendering, "
                "no gradient-heavy illustration, no drifting camera."
            ),
        },
        "极简产品广告（苹果风）": {
            "版本": 1,
            "说明": "干净背景、高级运镜、画面内英文短文案的产品片",
            "约束": (
                "【风格锁定：极简产品广告】\n"
                "画面：干净到极致的背景，没有杂物，产品是唯一主角。"
                "白色科技风（高亮背景 + 柔和大面积漫射光）或暗色轮廓光风（深色背景 + 边缘勾光）二选一，全片统一。\n"
                "运镜：高级克制的产品片镜头语言，缓慢精确，一个镜头只做一种运动。\n"
                "产品本体颜色绝对不许改动。背景、光环境、构图可以调整。\n"
                "画面内文案：每句 3~5 个英文单词，单行，总长不超过 32 个字符。"
                "语气像苹果产品片——感官的、讲好处的、讲材质的，"
                "不要促销口号，不要电商式功能标签，不要只有 1~2 个词的孤立标签。\n"
                "文案配色：白色科技风里前半句用黑或深灰（白底禁止白字），"
                "暗色轮廓光风里前半句可用白色；后半句一律用产品本身的颜色。\n"
                "文字由视频模型直接生成在画面里并控制出现时机，不要留空位等后期叠字。\n"
                "负面：no clutter, no busy background, no split screen, no collage layout, "
                "no promotional slogan, no ecommerce feature tags, no product recolor."
            ),
        },
        "音乐MV（节拍字幕）": {
            "版本": 1,
            "说明": "歌词文字作为视觉层随节拍运动，不是普通字幕",
            "约束": (
                "【风格锁定：音乐 MV 节拍字幕】\n"
                "画面内文字是设计过的视觉层，不是普通字幕——"
                "它有自己的字体质感、排版比例、空间位置和运动语言，会随节拍反应。\n"
                "文字必须用英文双引号原样写出完整字符串，并写清它在第几秒出现、怎么运动。\n"
                "表演、运镜、转场都要和音乐的节奏对齐，强拍上给硬切或定格。\n"
                "全片保持同一个 groove、同一种速度感、同一套灯光语言和文字运动规则。\n"
                "不要因为\u300c这是 MV\u300d就机械地堆表演、堆转场、堆运镜；每一样都要有节拍上的理由。\n"
                "负面：no ordinary burned-in subtitles, no caption bar at the bottom, "
                "no random text motion unrelated to the beat, no style drift between shots."
            ),
        },
    },

    # ===== 视频模型规范（下拉框内容由此生成）=====
    "视频模型规范": {
        "H3（海螺3）": {
            "版本": 4,
            "语言": "英文",
            "支持声音": True,
            "支持对白": True,
            "支持多镜头": True,
            "用官方格式": True,
            "时长下限": 4,
            "时长上限": 15,
            "时长范围": "4~15 秒整数；文生／图生建议 5~10 秒，全参考模式可到 15 秒",
            "规范": (
                "目标模型：MiniMax H3（海螺 3）。下面是官方提示词格式，严格照做。\n"
                "\n"
                "【整体结构】提示词分两部分：\n"
                "第一部分是图像对齐指令，只有一行，后面空一行。它会由系统直接给你，"
                "你【原样放在最前面】，一个字都不要改；没给你就说明本次不需要（文生视频）。\n"
                "第二部分是三个核心字段，字段名原样保留，字段之间空一行：\n"
                "\n"
                "integrated_multimodal_description: [Shot 1] ...\n"
                "\n"
                "overall_soundscape: ...\n"
                "\n"
                "non_diegetic_music: ...\n"
                "\n"
                "【三个字段各写什么】\n"
                "· integrated_multimodal_description —— 正文主体。沿时间线描述画面、动作、镜头、"
                "说话人、台词、唱歌和画内声音。每个细节都要对应看得见或听得见的东西：\n"
                "  视觉风格、初始构图、主体外貌与位置、场景与关键道具、动作与反应、"
                "镜头变化、说话语言、同步的画内音效。\n"
                "  在 [Shot 1] 开头先说明整体风格和初始构图。\n"
                "· overall_soundscape —— 全片的环境音、物理动作音、非语言人声，1~4 句一个连续段落。\n"
                "· non_diegetic_music —— 只有观众听得见的背景配乐，1~3 句。\n"
                "\n"
                "【其他硬性规则】\n"
                "1. 除 <d> 里的台词和画面上可见的文字外，全部用英文书写。字段名必须是英文原文。\n"
                "2. ⚠ 正文是连贯的英文叙述，不是字段表。"
                "不要把 Framing:／Setting:／Camera: 这种自造字段名写进正文，"
                "景别、机位、运镜、光线都要融进自然的英文句子里。\n"
                "3. 不要自己造 [Style]、[Exclude]、[Music] 这类官方没有的区块。\n"
                "4. 本地运行的 H3 开源权重没有官方的上下文预处理层，"
                "所以必须给出完整的官方结构化提示词，不要简化成一段随意的自然语言。\n"
                "5. 总长度 7000 字符以内。"
            ),
            "全参考格式": True,
        },
        "WAN2.2": {
            "版本": 3,
            "语言": "中文",
            "支持声音": False,
            "支持对白": False,
            "支持多镜头": False,
            "用官方格式": False,
            "时长范围": "5 秒左右",
            "规范": (
                "目标模型：阿里 WAN 2.2。\n"
                "1. 用中文书写，一整段连贯的描述，不要分镜、不要字段名、不要分行列点。\n"
                "2. 顺序：主体与外貌 → 主体动作 → 所处场景 → 运镜 → 光线氛围 → 画面风格。\n"
                "3. 只有一个连续镜头，不要写转场、不要写切镜头。\n"
                "4. 不要写声音、不要写台词，该模型只出画面。\n"
                "5. 动作描述具体到身体部位和幅度，避免\u300c优雅地\u300d这类评价词。\n"
                "6. 控制在 200 字以内，信息密度比长度重要。"
            ),
            "全参考格式": False,
        },
        "通用（其它视频模型）": {
            "版本": 3,
            "语言": "英文",
            "支持声音": False,
            "支持对白": False,
            "支持多镜头": False,
            "用官方格式": False,
            "时长范围": "5~10 秒",
            "规范": (
                "目标视频模型未指定，按通用规范写：\n"
                "1. 用英文书写，一段连贯的描述，不要字段名。\n"
                "2. 顺序：主体与外貌 → 动作 → 场景 → 运镜 → 光线 → 风格质感。\n"
                "3. 一个连续镜头，不写转场、不写声音。\n"
                "4. 用动词不用形容词，描述摄影机拍得出来的东西。\n"
                "5. 控制在 150 个英文词以内。"
            ),
            "全参考格式": False,
        },
    },
}

MODEL_SPEC_KEY = "视频模型规范"
STYLE_PRESET_KEY = "视频风格预设"
NESTED_KEYS = (MODEL_SPEC_KEY, STYLE_PRESET_KEY)

# 普通模板的版本号。内容改进后在这里 +1，用户 json 里的旧版本会在重启时自动替换。
# 没列在这里的模板一旦写进 json 就永远以用户的为准。
TEMPLATE_VERSIONS = {
    "视频_导演守则": 6,
    "视频_骨架": 2,
    "视频_大白话翻译": 2,
    "视频_运镜术语表": 3,
    "视频_声景守则": 3,
    "视频_配乐守则": 3,
    "视频_镜头规则": 1,
    "视频_对白": 3,
    "视频_画面文字": 1,
    "视频模式_文生视频": 3,
    "视频模式_首帧": 3,
    "视频模式_首尾帧": 3,
    "视频模式_尾帧": 3,
    "视频模式_参考素材": 5,
    "视频_全参考格式": 1,
    "视频_锁定要求": 2,
    "视频_负面默认": 3,
    "视频_负面写法": 3,
    "视频_输出要求": 6,
}
VERSION_KEY = "模板版本"


# ─── 模板读写 ──────────────────────────────────────────────────────────────────
def ensure_video_templates():
    """把本节点的默认模板补进共用的模板文件。

    已有内容不覆盖，唯一例外：内置的版本号比 JSON 里的新时自动替换那一条。
    自己改过的人不受影响——只要版本号没变就不会被动。
    """
    data = {}
    try:
        with open(TEMPLATES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}

    # 先记下哪些带版本的模板本来就不在文件里：它们是新写进去的，不算"升级"
    fresh = {k for k in TEMPLATE_VERSIONS if k not in data}

    changed, upgraded = False, []
    for k, v in VIDEO_TEMPLATES.items():
        if k in NESTED_KEYS:
            specs = data.get(k)
            if not isinstance(specs, dict):
                specs = {}
                changed = True
            for name, spec in v.items():
                old = specs.get(name)
                if not isinstance(old, dict):
                    specs[name] = spec
                    changed = True
                    continue
                try:
                    old_ver = int(old.get("版本", 1))
                except Exception:
                    old_ver = 1
                if int(spec.get("版本", 1)) > old_ver:
                    specs[name] = spec
                    changed = True
                    upgraded.append(name)
            data[k] = specs
        elif k not in data:
            data[k] = v
            changed = True

    # 普通模板的版本升级
    vers = data.get(VERSION_KEY)
    if not isinstance(vers, dict):
        vers = {}
    tpl_upgraded = []
    for k, v in TEMPLATE_VERSIONS.items():
        if k not in VIDEO_TEMPLATES:
            continue
        try:
            old_v = int(vers.get(k, 1))
        except Exception:
            old_v = 1
        if k in fresh:
            vers[k] = v
            changed = True
        elif v > old_v:
            data[k] = VIDEO_TEMPLATES[k]
            vers[k] = v
            changed = True
            tpl_upgraded.append(k)
        elif vers.get(k) != v:
            vers[k] = v
            changed = True
    if vers != data.get(VERSION_KEY):
        data[VERSION_KEY] = vers
        changed = True

    if upgraded:
        print("[高级提示词] 视频模型规范已升级到新版：" + "、".join(upgraded))
    if tpl_upgraded:
        print("[高级提示词] 指令模板已升级到新版：" + "、".join(tpl_upgraded))

    if changed:
        try:
            with open(TEMPLATES_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


def load_video_templates():
    """每次调用都重新读，改完文件不用重启"""
    tpl = dict(VIDEO_TEMPLATES)
    try:
        with open(TEMPLATES_FILE, "r", encoding="utf-8") as f:
            user = json.load(f)
        for k, v in user.items():
            if k in NESTED_KEYS and isinstance(v, dict):
                merged = dict(VIDEO_TEMPLATES[k])
                need = "规范" if k == MODEL_SPEC_KEY else "说明"
                for name, spec in v.items():
                    if isinstance(spec, dict) and str(spec.get(need, "")).strip():
                        merged[name] = spec
                tpl[k] = merged
            elif isinstance(v, str) and v.strip() and k in VIDEO_TEMPLATES:
                tpl[k] = v
    except Exception:
        pass
    return tpl


def style_preset_names():
    """风格预设下拉：JSON 里有什么就显示什么，「不使用」永远排第一"""
    try:
        names = list(load_video_templates().get(STYLE_PRESET_KEY, {}).keys())
    except Exception:
        names = list(VIDEO_TEMPLATES[STYLE_PRESET_KEY].keys())
    names = names or list(VIDEO_TEMPLATES[STYLE_PRESET_KEY].keys())
    if "不使用" in names:
        names = ["不使用"] + [n for n in names if n != "不使用"]
    return names


def video_model_names():
    """下拉框内容：JSON 里有什么模型就显示什么"""
    try:
        names = list(load_video_templates().get(MODEL_SPEC_KEY, {}).keys())
        return names or list(VIDEO_TEMPLATES[MODEL_SPEC_KEY].keys())
    except Exception:
        return list(VIDEO_TEMPLATES[MODEL_SPEC_KEY].keys())


# ─── 稳定性提醒 ────────────────────────────────────────────────────────────────
# 每多一个变化就多一个崩的机会。额度随时长走，支出按项计价。



MOVE_WORD = {
    "跟着主体": "Tracking Shot（跟随移动的主体）",
    "推近":    "Push In（向主体推近）",
    "拉远":    "Pull Back（从主体拉远）",
    "环绕":    "Arc Shot（绕主体弧形移动）",
}

CREATIVITY = {"保守": 0.3, "平衡": 0.7, "大胆": 1.1}


# ─── 结果解析 ──────────────────────────────────────────────────────────────────
def _parse_prompt_zh(text):
    """解析 {"prompt": ..., "zh": ...}

    返回 (提示词, 中文说明, 失败原因)。失败原因非空时调用方要报出来 ——
    以前这里是静默降级：整段原始回复（含"好的，这是提示词："之类的开场白）
    被当成提示词输出、中文说明变成空的，日志里一个字都不提，
    用户只能看到"中文说明怎么没了"却不知道为什么。
    """
    text = clean_text(text)
    import re
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        return text, "", "回复里找不到 JSON 对象"
    try:
        obj = json.loads(m.group(0))
    except Exception as e:
        return text, "", "JSON 解析失败：" + str(e)[:80]
    p = clean_text(str(obj.get("prompt", "")))
    if not p:
        return text, "", "JSON 里没有 prompt 字段"
    zh = obj.get("zh", "")
    # ⚠ 指令里说 zh"分三部分"，模型很容易理解成"三个键"而返回一个嵌套对象。
    #   直接 str() 会印出 {'镜头清单': '...', ...} 这种 Python 字典字面量，没法看。
    #   这里按顺序拼回一段文本，顺便兼容它返回列表的情况。
    if isinstance(zh, dict):
        zh = "\n\n".join(str(v).strip() for v in zh.values() if str(v).strip())
    elif isinstance(zh, (list, tuple)):
        zh = "\n\n".join(str(v).strip() for v in zh if str(v).strip())
    return p, clean_text(str(zh)), ""


# ─── 节点 ──────────────────────────────────────────────────────────────────────
class VideoPromptWriter(io.ComfyNode):
    """用 ComfyUI V3 schema 定义。

    参考图走官方的 io.Autogrow —— 动态增减由前端原生处理，
    不会像自己用 addInput() 那样触发重排、把节点撑高。
    """

    @classmethod
    def define_schema(cls):
        providers = _provider_names()
        models = video_model_names()
        presets = style_preset_names()
        adv = dict(optional=True, advanced=True)
        return io.Schema(
            node_id="VideoPromptWriter",
            display_name="🎬 视频提示词（AI当导演）",
            category="自定义/高级提示词/视频提示词",
            description="大白话写想要什么，AI 扮演导演按目标模型的官方格式写提示词",
            inputs=[
                io.String.Input(
                    "你的想法", multiline=True, default="",
                    placeholder="用大白话写想要什么，例如：一个女人在地下室跳恰恰舞。\n写得越少 AI 发挥越多，写得越具体越贴近你的想法。"),
                io.Combo.Input(
                    "目标模型", options=(models or ["（请重启ComfyUI）"]),
                    tooltip="列表来自 ai_prompt_templates.json 的「视频模型规范」。\n"
                            "想支持新模型，在那里照格式加一段规范，重启后即出现在这里。"),
                io.Int.Input(
                    "视频时长", default=5, min=1, max=60,
                    tooltip="秒。H3 支持 4~15 秒整数。\n时长越短，能塞下的变化越少——节点会算给你看"),

                # ── 五个拍摄开关：默认全部交给 AI 判断 ──
                io.Int.Input(
                    "分镜", default=1, min=0, max=10,
                    tooltip="这条片子切几个镜头。\n"
                            "0 = 交给AI，它按内容需不需要换视角自己定（推荐）\n"
                            "1 = 1镜到底，全程不切，最稳\n"
                            "2~10 = 指定切几个\n"
                            "⚠ 每个镜头最好有 2 秒以上。镜头太短模型来不及把人脸对齐，"
                            "会一镜一张脸。节点会算给你看。"),
                io.Combo.Input(
                    "运镜", default="交给AI",
                    options=["交给AI", "镜头不动", "跟着主体", "推近", "拉远", "环绕"],
                    tooltip="摄影机怎么动。默认交给AI——它写的内容它最清楚该配什么手法。\n"
                            "镜头不动：固定机位，最稳\n"
                            "跟着主体：人走到哪拍到哪\n"
                            "推近：从环境推到人，越近越私密\n"
                            "拉远：从人退开，交代她身处什么境地\n"
                            "环绕：绕着拍，看立体关系，也容易产生不安感\n"
                            "多镜头时这是全片基调，AI 会按每一镜的内容微调"),
                io.Combo.Input(
                    "光线", default="交给AI",
                    options=["交给AI", "保持不变", "变亮", "变暗", "换色调"],
                    tooltip="光线变不变、往哪变。默认交给AI。\n"
                            "⚠ AI 只会在光变【说得通】的时候才变——开灯、车灯扫过、\n"
                            "云遮太阳、人走进阴影。没有光源上的解释就不动光，\n"
                            "因为凭空变光一眼看得出假。"),
                io.Combo.Input(
                    "表情", options=["交给AI", "保持不变", "允许变化"], default="交给AI",
                    tooltip="预算支出：允许变化 1 点\n"
                            "选「交给AI」＝它找画面里的触发点，找得到才转，找不到就不硬转。\n"
                            "表情突变那一帧最容易换脸。选「保持不变」时情绪仍可细微递进，\n"
                            "只是不会在某一秒切换成另一种情绪"),
                io.Combo.Input(
                    "声音", options=["静音", "环境音", "环境音+动作音", "再加配乐"], default="环境音",
                    tooltip="预算支出：再加配乐 1 点\n"
                            "声音层越多，模型要同时对齐的东西越多，容易糊成一团。\n"
                            "不支持声音的模型会自动忽略此项"),
                io.Combo.Input(
                    "风格预设", options=(presets or ["不使用"]), default="不使用",
                    tooltip="整条片子的视觉风格锁定，内容提炼自 MiniMax 官方风格技能。\n"
                            "选中后会把该风格的质感、运动语言和负面约束注入指令。\n"
                            "想加新风格：在 ai_prompt_templates.json 的「视频风格预设」里照格式加一段"),

                io.Combo.Input(
                    "服务商", options=(providers or ["（请重启ComfyUI）"]),
                    default=(providers[0] if providers else None),
                    tooltip="调用哪家 AI 来写提示词。接了图片时需要支持看图的服务商"),
                io.String.Input("模型", default="", placeholder="留空用推荐模型，或点下方按钮选择"),
                io.Combo.Input(
                    "创意度", options=["保守", "平衡", "大胆"], default="平衡",
                    tooltip="AI 在你没说的地方发挥到什么程度。\n"
                            "保守：贴着你的字面意思，补最常规的处理\n"
                            "平衡：推荐\n"
                            "大胆：画面设计更有想法，但偏离你本意的概率也更高\n"
                            "接了图片时自动压到保守，保证忠实于画面"),
                io.Combo.Input(
                    "每次运行", options=["固定", "随机", "直通"], default="固定",
                    tooltip="固定：内容不变时复用上次结果，不重复扣费\n"
                            "随机：每次重新设计一版\n"
                            "直通：【不调用 AI】，把「你的想法」框里的内容原样当提示词输出。\n"
                            "　　　用于手改提示词后重跑，或复用之前写好的一条。\n"
                            "　　　节点仍会帮你做格式自检（镜头编号、官方字段、非官方写法），"
                            "结果写在中文说明里。"),

                # ── 素材 ──
                io.Image.Input(
                    "首帧图", optional=True,
                    tooltip="视频的第一帧。接了它就是图生视频，AI 会先读懂画面再设计运动。\n⚠ 不能和「参考图」同时使用"),
                io.Image.Input(
                    "尾帧图", optional=True,
                    tooltip="视频的最后一帧。和首帧一起接 = 首尾帧模式；只接它 = 尾帧模式。\n⚠ 不能和「参考图」同时使用"),
                io.Autogrow.Input(
                    "参考图", optional=True,
                    template=io.Autogrow.TemplateNames(
                        input=io.Image.Input(
                            "参考图",
                            tooltip="全参考模式的素材库。接了任意一张就进入参考素材生视频。\n"
                                    "参考图不是画面，是\u300c这个人长什么样\u300d\u300c这个场景长什么样\u300d。\n"
                                    "编号对应提示词里的 <Picture N>，官方上限 9 张。\n"
                                    "⚠ 不能和首帧图／尾帧图同时使用"),
                        names=["参考图" + str(i) for i in range(1, 10)], min=0)),

                # ── 你想要什么，一个框写完 ──
                io.String.Input(
                    "我的具体要求", multiline=True, default="", **adv,
                    placeholder="大白话随便写，不用分类。例如：\n"
                                "我要脸的大特写\n"
                                "第2个镜头从背后拍她\n"
                                "背景糊掉\n"
                                "手要一直拿着那把伞",
                    tooltip="你写的每一条都【必须】被落实，不许 AI 改写成它认为更好的做法。\n"
                            "和上面的开关冲突时以你写的为准，AI 会在中文说明里告诉你覆盖了哪一项。\n"
                            "留空＝全部由 AI 决定。"),
                io.String.Input(
                    "台词", multiline=True, default="", **adv,
                    placeholder="留空＝无对白。写了就自动按「我来写」处理。\n多镜头可以写「第1个镜头：……」",
                    tooltip="写了台词才会启用官方对白规则：说话人 ID、音色设计、口型同步、"
                            "中文一秒约 3~4 字的时长校验。\n"
                            "留空时 AI 按场景判断——有交流对象、时间够说完、"
                            "镜头看得见嘴，三条都满足它才会写台词。\n"
                            "完全不想有人说话，写进「不要出现的东西」。"),
                io.String.Input(
                    "不要出现的东西", multiline=True, default="", **adv,
                    placeholder="留空＝只用默认那套（不要多余肢体、扭曲的手、水印、画面撕裂……）\n"
                                "写了就在默认之上【追加】，例如：\n不要红色\n不要出现其他人",
                    tooltip="这里写的是【追加】，不会顶掉默认那套保命约束。"),
                io.Combo.Input(
                    "思考模式", options=["关闭", "开启"], default="关闭", **adv,
                    tooltip="开启后 AI 先推理再回答，多镜头和复杂要求更稳，但更慢更贵"),
                io.String.Input(
                    "API密钥", default="", **adv,
                    placeholder="建议留空，在 ai_prompt_keys.json 里填写"),
                io.String.Input(
                    "代理地址", default="", **adv,
                    placeholder="海外服务商用，如 127.0.0.1:7890；留空用系统代理"),
                io.Combo.Input(
                    "详细日志", options=["关闭", "开启"], default="关闭", **adv,
                    tooltip="额外打印完整系统指令、请求参数和 API 原始返回"),
            ],
            outputs=[
                io.String.Output(display_name="提示词"),
                io.String.Output(display_name="中文说明"),
            ],
        )

    # ── 图像对齐指令：官方固定措辞，由节点直接生成，不让 AI 写 ──────────────
    @classmethod
    def _align_line(cls, mode_key, 时长, shot_n):
        """返回官方要求的第一行；文生视频没有这一行，返回空字符串。

        S.SS 是有效时长，必须保留两位小数；N 是最后一个镜头的序号。
        """
        dur = "%.2f" % float(时长)
        last = "[Shot " + str(shot_n) + "]"
        if mode_key == "首帧":
            return ("For the target video, at 0.00 seconds into the target video, "
                    "<Picture 1> (from [Shot 1]) is fully referenced.")
        if mode_key == "首尾帧":
            return ("How the reference pictures align with the target video — "
                    "Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; "
                    "Picture 2 (from Shot " + str(shot_n) + ") aligns with the "
                    + dur + "-second mark of the target video.")
        if mode_key == "尾帧":
            return ("How the reference pictures align with the target video — "
                    "<Picture 1> (from " + last + ") aligns with the "
                    + dur + "-second mark of the target video.")
        return ""

    # ── 稳定性提醒：不记账，直接算出具体数字说人话 ────────────────────────
    @classmethod
    def _sanity_warnings(cls, 时长, shot_n, 运镜, 光线, 表情,
                         dur_bad, 目标模型, dur_lo, dur_hi):
        """返回一串中文提醒。

        以前这里是一套"变化预算 / 额度 / 点数"的记账法，是我自己发明的概念，
        用户得先学规则才看得懂。现在改成直接把算出来的数字摆给他看 ——
        "每镜 1.5 秒"比"超支 4 点"有用得多，因为前者可以自己验证。
        """
        w = []
        if dur_bad:
            w.append(dur_bad + "。提示词会照写，但视频模型那边可能直接报错或自动截断。"
                     + ("建议改到 %d~%d 秒之间。" % (dur_lo, dur_hi)
                        if dur_lo and dur_hi else ""))
        if shot_n > 1:
            per = 时长 / float(shot_n)
            if per < 1.5:
                w.append("%d 秒切 %d 个镜头，平均每镜只有 %.1f 秒。"
                         "这个长度模型根本来不及把人脸对齐，基本每个镜头都会是另一张脸。"
                         "强烈建议降到 %d 镜以内，或者把时长加到 %d 秒。"
                         % (时长, shot_n, per, max(1, int(时长 // 2)), shot_n * 2))
            elif per < 2.0:
                w.append("%d 秒切 %d 个镜头，平均每镜 %.1f 秒。"
                         "镜头短于 2 秒时人脸和服装细节容易在切口处变样，"
                         "降到 %d 镜会稳不少。"
                         % (时长, shot_n, per, max(1, int(时长 // 2))))
            elif shot_n >= 5:
                w.append("%d 个镜头，平均每镜 %.1f 秒。镜头越多，人物在切口处漂移的"
                         "机会就越多——每一刀都是一次重新生成这个人的机会。"
                         % (shot_n, per))
        moving = 运镜 not in ("镜头不动", "交给AI")
        changing = [n for n, v in (("光线", 光线), ("表情", 表情))
                    if v not in ("保持不变", "交给AI")]
        if shot_n > 1 and moving and changing:
            w.append("同时开了：切镜头、%s、%s变化。三件事叠在一起，"
                     "模型要同时对齐位移和状态变化，崩坏概率明显上升。"
                     "建议先只留最重要的那一个，看稳不稳再加。"
                     % (运镜, "和".join(changing)))
        elif 时长 <= 5 and shot_n > 2:
            w.append("%d 秒的片子切 %d 个镜头偏碎了。短片用 1~2 个镜头把一件事说清楚，"
                     "通常比多镜头好看。" % (时长, shot_n))
        return w

    # ── 拍摄设置 → 指令 ──────────────────────────────────────────────────────
    @classmethod
    def _shoot_block(cls, spec, 时长, shot_n, 运镜, 光线, 表情, 声音, has_lines):
        """把节点上的开关翻译成指令。

        选了「交给AI」的项不是放任不管 —— 导演守则里写了判断规则，
        这里只负责说清楚"这一项归你定"。
        """
        multi = spec.get("支持多镜头", False)
        lines = ["【本次拍摄设置】标【必须】的是用户定死的，照做不许改；"
                 "标【你来定】的是用户交给你的，按导演守则里的规则自己判断。"]

        # ── 分镜 ──
        if shot_n == 0 and multi:
            lines.append("· 分镜【你来定】：几个镜头你自己定。"
                         "唯一的判断标准是【有没有必要换视角】——"
                         "要看清一个表情、要交代一个新的空间关系、要把节奏推上去，"
                         "这些才值得切一刀；为了「不单调」而切是最糟的理由。\n"
                         "  官方还有一条：如果只是远近或者角度略微变一下，"
                         "【用运镜，不要切镜头】。\n"
                         "  不确定就 1 镜到底。每个镜头至少 2 秒，"
                         + str(时长) + " 秒最多排 " + str(max(1, int(时长) // 2)) + " 个。")
        elif not multi or shot_n <= 1:
            lines.append("· 分镜【必须】：只有 [Shot 1] 一个连续镜头，全程不许切，"
                         "不要出现第二个 [Shot N]。")
        else:
            lines.append("· 分镜【必须】：正好 " + str(shot_n) + " 个镜头，不多不少。"
                         "[Shot 1] 不带时间戳，之后每个写 [Shot N] At MM:SS.mmm, ...，"
                         "切点严格递增且都落在 " + str(时长) + " 秒之内。")

        # ── 运镜 ──
        n_for_move = shot_n if shot_n > 0 else 0
        if 运镜 == "交给AI":
            base = ("· 运镜【你来定】：按导演守则里的「运镜怎么选」逐条对照，"
                    "从内容推出手法。记住默认是固定机位，运动要挣来。")
            if n_for_move != 1:
                base += ("\n  多镜头时【逐镜决定】，不要全片一个动作："
                         "静止抒情的镜头就固定，人在移动的镜头才跟随。"
                         "一条片子里动起来的镜头应该是少数。")
            lines.append(base)
        elif 运镜 == "镜头不动":
            lines.append("· 运镜【必须】：全片固定机位。每个镜头正文里都要写出一句 "
                         "the camera holds a static shot，不许有任何镜头运动。")
        else:
            word = MOVE_WORD[运镜]
            lines.append("· 运镜【必须】：用户指定了「" + 运镜 + "」，"
                         "对应官方词表里的 " + word + "。\n"
                         "  这是全片的基调：在最适合它的那个镜头上用。\n"
                         "  写法＝运动类型（+ 幅度 + 速度）。幅度和速度【只在有意义时才加】，"
                         "中等幅度和正常速度按官方规定省略不写。"
                         "一个镜头里只用一种运动。\n"
                         "  其余镜头如果内容不需要运动，就写 the camera holds a static shot，"
                         "不要为了统一而每镜都动。")

        # ── 光线 ──
        if 光线 == "交给AI":
            lines.append("· 光线【你来定】：按导演守则里的「光什么时候该变」判断。"
                         "光变必须有光源上的解释，找不到解释就全程不变，"
                         "在 Light 相关描述结尾写 unchanged throughout。")
        elif 光线 == "保持不变":
            lines.append("· 光线【必须】：全程不变。不要设计任何光线变化，"
                         "结尾写 unchanged throughout。")
        else:
            lines.append("· 光线【必须】：安排一次「" + 光线 + "」的变化。"
                         "⚠ 必须同时给出光源上的解释（开灯、车灯扫过、云遮日、"
                         "人走进阴影之类），写清在第几秒、从什么状态到什么状态。"
                         "没有来源的光变一眼看得出假。")

        # ── 表情 ──
        if 表情 == "交给AI":
            lines.append("· 表情【你来定】：人的表情不会无缘无故变，它要么是对画面里"
                         "发生的事的反应，要么是内心活动浮上来的那一刻。"
                         "找得到这个触发点，就把转变压在它后面半拍；"
                         "找不到，就让情绪在一个基调里细微起伏，不要硬转。\n"
                         "  真要转就写清在第几秒、从什么情绪到什么情绪，"
                         "并让它在 1 秒内渐变完成，不许瞬间切换。")
        elif 表情 == "保持不变":
            lines.append("· 表情【必须】：不要设计表情突变。情绪可以细微递进，"
                         "但不许在某一秒切换成另一种情绪。")
        else:
            lines.append("· 表情【必须】：安排一次情绪转变，写清在第几秒、从什么变到什么，"
                         "并让它在 1 秒内渐变完成。")

        # ── 声音 ──
        if not spec.get("支持声音", False) or 声音 == "静音":
            lines.append("· 声音【必须】：全程静音，overall_soundscape 与 "
                         "non_diegetic_music 都写 N/A。")
        elif 声音 == "环境音":
            lines.append("· 声音【必须】：overall_soundscape 只写环境底噪，不要动作音效；"
                         "non_diegetic_music 写 N/A。")
        elif 声音 == "环境音+动作音":
            lines.append("· 声音【必须】：overall_soundscape 写环境底噪 + "
                         "每个明显动作最多 1 个音效；non_diegetic_music 写 N/A。")
        else:
            lines.append("· 声音【必须】：overall_soundscape 写环境底噪 + 动作音效，"
                         "non_diegetic_music 写画外配乐的配器、速度、节奏和力度变化。")

        # ── 台词 ──
        if spec.get("支持对白", False):
            if has_lines:
                lines.append("· 台词【必须】：用户给了台词，原样使用，不许改写、不许翻译、"
                             "不许增删。安排在能看见嘴的镜头里，按官方规则用 <d> 包好。")
            else:
                secs = max(1, int(时长))
                lines.append(
                    "· 台词【你来定】：这个场景要不要有人说话，你自己判断。\n"
                    "  三个前提缺一个就不要写：\n"
                    "  (1) 有说话的理由 —— 有交流对象，或有非说不可的自白冲动。"
                    "一个人安静跳舞、走路、发呆，都不需要台词。\n"
                    "  (2) 时间够 —— 中文一秒约 3~4 个字，这条片子 " + str(secs)
                    + " 秒最多容纳 " + str(secs * 3) + " 字左右，还要留出不说话的呼吸。\n"
                    "  (3) 镜头给得到脸 —— 台词要落在能看见嘴的镜头里。\n"
                    "  判断下来不需要，就一个字都不要写，这不是失职。")

        return "\n".join(lines)

    # ── 锁定要求 → 硬约束 ────────────────────────────────────────────────────
    @classmethod
    def _locks_block(cls, tpl, text):
        """用户在「我的具体要求」里写的一整段，原样交给 AI 自己分类。"""
        if not text:
            return ""
        return tpl["视频_锁定要求"] + "\n\n用户写的要求：\n" + text

    # ── 组装系统指令 ─────────────────────────────────────────────────────────
    @classmethod
    def _build_system(cls, tpl, spec, 时长, mode_key, shoot, locks_text, align,
                      has_lines, neg_text, has_ref_imgs,
                      声音, style_lock="", align_later=False):
        if True:
            official = spec.get("用官方格式", False)
            parts = [tpl["视频_导演守则"], spec.get("规范", "")]

            if official:
                if mode_key == "参考素材" and spec.get("全参考格式", False):
                    parts.append(tpl["视频_全参考格式"])
                parts.append(tpl["视频_镜头规则"])
                parts.append(tpl["视频_画面文字"])
                parts.append(tpl["视频_骨架"])

            parts.append(tpl["视频_大白话翻译"])
            parts.append(tpl["视频_运镜术语表"])
            parts.append(tpl.get("视频模式_" + mode_key, ""))

            if align:
                parts.append("【本次的图像对齐指令】下面这一行是官方固定措辞，"
                             "原样放在提示词最前面，后面空一行再写第一个字段，一个字都不要改：\n"
                             + align)
            elif align_later:
                parts.append("【不要写图像对齐指令】提示词开头那行 "
                             "How the reference pictures align with the target video — ... "
                             "由系统在你写完之后自动补上，因为它要写死最后一个镜头的序号，"
                             "而镜头数是你定的。\n"
                             "你【不要】写这一行，直接从第一个字段开始写。"
                             "镜头数按内容需要自己定，最后一个 [Shot N] 要在视频结尾处"
                             "抵达参考图那一帧。")

            parts.append("时长：本段视频 " + str(时长) + " 秒，"
                         "动作总量要与这个长度匹配，不要安排超出时长的情节。")
            parts.append(shoot)
            if locks_text:
                parts.append(locks_text)

            if spec.get("支持声音", False) and 声音 != "静音":
                parts.append(tpl["视频_声景守则"])
                if 声音 == "再加配乐":
                    parts.append(tpl["视频_配乐守则"])

            if has_lines and spec.get("支持对白", False):
                parts.append(tpl["视频_对白"])
            if neg_text:
                parts.append(tpl["视频_负面写法"])

        if style_lock:
            parts.append(style_lock)

        parts.append(tpl["视频_输出要求"])
        return "\n\n".join(p for p in parts if p and p.strip())

    # ── 主流程 ────────────────────────────────────────────────────────────────
    @classmethod
    def execute(cls, 你的想法, 目标模型, 视频时长, 分镜, 运镜, 光线, 表情, 声音,
            风格预设, 服务商, 模型, 创意度, 每次运行,
            首帧图=None, 尾帧图=None, 参考图=None,
            我的具体要求="", 台词="", 不要出现的东西="",
            思考模式="关闭", API密钥="", 代理地址="", 详细日志="关闭"):

        log = Log(verbose=(详细日志 == "开启"))
        tpl = load_video_templates()
        specs = tpl.get(MODEL_SPEC_KEY, {})
        spec = specs.get(目标模型) or next(iter(specs.values()), {})

        idea = (你的想法 or "").strip()
        # 台词：写了就一定说（并启用官方对白规则），留空就让 AI 自己判断要不要有人开口。
        lines_txt = (台词 or "").strip()
        # ⚠ 老工作流迁移：以前「台词」上面有个「台词模式」下拉（不要台词/交给AI/我来写）。
        #   删掉下拉之后，ComfyUI 会按位置把旧的下拉值塞进这个文本框，
        #   于是提示词里真的出现了 <d>交给AI</d>。这几个词不可能是真台词，拦掉。
        旧值残留 = ""
        if lines_txt in ("交给AI", "不要台词", "我来写"):
            旧值残留 = lines_txt
            lines_txt = ""


        # ── 参考图：走官方 io.Autogrow，execute 里拿到的是 {输入名: 图} 字典 ──
        #   名字形如「参考图1」「参考图2」，编号要抠出来，因为它直接对应
        #   提示词里的 <Picture N>，顺序错了就张冠李戴。
        ref_imgs = []
        for name, im in sorted((参考图 or {}).items(), key=lambda kv: kv[0]):
            if im is None:
                continue
            digits = "".join(c for c in name if c.isdigit())
            ref_imgs.append((int(digits) if digits else len(ref_imgs) + 1, im))
        ref_imgs.sort(key=lambda kv: kv[0])

        # ── 判定模式。参考素材与首／尾帧互斥，这是官方规定，不是我加的限制 ──
        if ref_imgs and (首帧图 is not None or 尾帧图 is not None):
            msg = ("「参考图」不能和「首帧图」／「尾帧图」同时使用。\n"
                   "参考素材生视频（Ref2VA）和首尾帧生视频是两种互斥的官方模式。\n"
                   "要么只接参考图，要么只接首帧／尾帧，断开一边再跑。")
            write_error_log(msg)
            raise RuntimeError(msg)

        if ref_imgs:
            images = list(ref_imgs)
            mode_key, mode_name = "参考素材", "参考素材生视频"
        elif 首帧图 is not None and 尾帧图 is not None:
            images = [(1, 首帧图), (2, 尾帧图)]
            mode_key, mode_name = "首尾帧", "首尾帧生视频"
        elif 首帧图 is not None:
            images = [(1, 首帧图)]
            mode_key, mode_name = "首帧", "首帧生视频"
        elif 尾帧图 is not None:
            images = [(1, 尾帧图)]
            mode_key, mode_name = "尾帧", "尾帧生视频"
        else:
            images = []
            mode_key, mode_name = "文生视频", "文生视频"
        has_image = bool(images)

        # 直通：不调 AI，把「你的想法」原样当提示词输出，只做格式自检。
        # 用于手改提示词后重跑、或复用之前写好的一条，不花调用次数。
        直通 = (每次运行 == "直通")

        if 直通 and not idea:
            msg = ("「每次运行」选了直通，但「你的想法」是空的。\n"
                   "直通模式下这个框装的是【完整提示词】，把你要用的提示词贴进去。")
            write_error_log(msg)
            raise RuntimeError(msg)
        if not 直通 and not idea and not has_image:
            msg = ("什么都没给：「你的想法」是空的，也没接图片。"
                   "至少给一样，AI 才知道要拍什么。")
            write_error_log(msg)
            raise RuntimeError(msg)

        # ── 服务商与密钥 ──（直通不发请求，这两项都不需要）
        cfg = get_all_providers().get(服务商, {})
        if not 直通 and has_image and not cfg.get("vision", False):
            msg = (服务商 + " 不支持图像输入（vision=false）。\n"
                   "解决办法：在 ai_prompt_keys.json 里把该服务商改成字典形式并加上 "
                   "\"vision\": true，或者换一个支持看图的服务商。")
            write_error_log(msg)
            raise RuntimeError(msg)

        key = load_key(服务商, API密钥)
        if not 直通 and not key:
            msg = "没有找到 " + 服务商 + " 的 API Key，请填写 ai_prompt_keys.json"
            write_error_log(msg)
            raise RuntimeError(msg)

        # ── 图片编码 ──
        img_b64, img_info = [], []
        for idx, im in ([] if 直通 else images):
            b64, o, n = img_to_b64(im)
            img_b64.append((idx, b64))
            if ref_imgs:
                label = "参考图" + str(idx)
            else:
                label = "首帧" if idx == 1 else "尾帧"
            img_info.append(label + " " + str(o[0]) + "×" + str(o[1])
                            + ("" if o == n else "→" + str(n[0]) + "×" + str(n[1])))
        # 请求体大小要打出来。图片走 base64，体积直接决定这次请求能不能走通代理：
        # 太大了对端会在读完之前就掐断连接（RemoteDisconnected / SSL EOF），
        # 看着像"网络坏了"，其实刷新模型列表那种小请求照样通。
        img_mb = sum(len(b) for _, b in img_b64) / 1048576.0

        # 模型能力检查
        ignored = []
        if 声音 != "静音" and not spec.get("支持声音", False):
            ignored.append("声音")
        if lines_txt and not spec.get("支持对白", False):
            ignored.append("台词")
        # 分镜现在是数字：0 = 交给AI，1~10 = 指定。
        # 首尾帧／尾帧两种模式例外：官方对齐指令要写死「最后一个镜头是第几个」，
        # 这一行必须在发请求之前生成好，等不到 AI 的决定，所以这两种模式下 0 按 1 处理。
        shot_n = int(分镜)
        分镜_降级 = ""
        align_later = False
        if shot_n == 0 and mode_key == "首尾帧":
            # 官方原文：FL2VA generally favors a single shot so the model can
            # interpolate continuously ... Use multiple shots only when they are
            # explicitly specified. —— 填 0 就是"没有明确指定"，按单镜头走才对。
            分镜_降级 = ("首尾帧模式官方推荐单镜头（这样模型才能从首帧到尾帧连续插值），"
                        "「分镜」填 0 时按 1 镜处理。想要多镜头直接填数字，节点会照给。")
            shot_n = 1
        elif shot_n == 0 and mode_key == "尾帧":
            # L2VA 官方没有"倾向单镜头"这条，<Picture 1> 属于最后一个 [Shot N]，
            # N 本来就是变量。卡点只在于那行对齐指令要写死 N，而节点得在问 AI
            # 之前生成它 —— 解法是【等 AI 答完再补这一行】，见下面 align_later。
            align_later = True
        if shot_n > 1 and not spec.get("支持多镜头", False):
            ignored.append("多镜头")
            shot_n = 1

        # 时长是否在目标模型支持的范围内
        dur_lo, dur_hi = spec.get("时长下限"), spec.get("时长上限")
        dur_bad = ""
        if dur_lo and 视频时长 < dur_lo:
            dur_bad = ("时长 " + str(视频时长) + " 秒低于 " + 目标模型 + " 支持的下限 "
                       + str(dur_lo) + " 秒")
        elif dur_hi and 视频时长 > dur_hi:
            dur_bad = ("时长 " + str(视频时长) + " 秒超出 " + 目标模型 + " 支持的上限 "
                       + str(dur_hi) + " 秒")

        # 稳定性提醒：不记账，直接算出具体数字说人话
        warns = cls._sanity_warnings(视频时长, shot_n, 运镜, 光线, 表情, dur_bad,
                                     目标模型, dur_lo, dur_hi)

        # 用户钉死的要求：现在就一个框，AI 自己分类
        locks_text = ""
        my_req = (我的具体要求 or "").strip()
        if my_req:
            locks_text = cls._locks_block(tpl, my_req)

        # 负面：默认那套保命约束永远在，用户写的是【追加】
        neg_text = tpl["视频_负面默认"]
        extra_neg = (不要出现的东西 or "").strip()
        if extra_neg:
            neg_text += "\n另外，用户特别指明不要出现：" + extra_neg

        shoot = cls._shoot_block(spec, 视频时长, shot_n, 运镜, 光线, 表情, 声音,
                                 bool(lines_txt))
        if not spec.get("用官方格式", False):
            align = ""
        elif align_later:
            # 这一行等 AI 答完、数出实际镜头数之后，由节点贴到最前面。
            # 让 AI 自己写的话，官方固定措辞抄错了只能靠自检警告，不如不让它碰。
            align = ""
        else:
            align = cls._align_line(mode_key, 视频时长, shot_n)

        preset_cfg = tpl.get(STYLE_PRESET_KEY, {}).get(风格预设) or {}
        style_lock = str(preset_cfg.get("约束", "")).strip()
        system = cls._build_system(
            tpl, spec, 视频时长, mode_key, shoot, locks_text, align,
            bool(lines_txt), neg_text, bool(ref_imgs),
            声音, style_lock, align_later,
        )
        if 每次运行 == "随机":
            system += ("\n\n补充：请给出与上次不同的具体方案，紧扣用户原意和上面的拍摄设置，"
                       "但在景别、构图、光线方向、环境细节上换一种处理方式。")

        # 用户消息
        user_parts = []
        if idea:
            user_parts.append("用户想要：" + idea)
        elif has_image:
            user_parts.append("用户没有写文字要求，请根据图片自行设计这段视频。")
        elif ref_imgs:
            user_parts.append("用户没有写文字要求，请根据参考图自行设计这段视频。")
        if lines_txt:
            user_parts.append("本段台词（原文保留，不要翻译）：\n" + lines_txt)
        if ref_imgs:
            user_parts.append("本次接入了 " + str(len(ref_imgs)) + " 张参考图，编号依次为 "
                              + "、".join("参考图" + str(i) for i, _ in ref_imgs)
                              + "，分别对应 "
                              + "、".join("<Picture " + str(i) + ">" for i, _ in ref_imgs)
                              + "。请逐张看图后再写 subject_definitions。\n"
                              "⚠ 如果你实际上看不到这些图片，不要凭空编造外貌、服装或场景，"
                              "直接只回复一行 IMAGES_NOT_VISIBLE 然后停止。"
                              "编造的描述会让用户白跑一次视频生成，比报错严重得多。")
        if neg_text:
            user_parts.append("不希望出现的内容：\n" + neg_text)
        user_msg = "\n\n".join(user_parts)

        # 日志头
        model_show = (模型.strip() or _fallback_cache.get(服务商) or cfg.get("model", ""))
        head = ("目标模型：" + 目标模型 + "　模式：" + mode_name + "\n"
                + "服务商：" + 服务商 + "　模型：" + model_show
                + ("（手动指定）" if 模型.strip() else "（推荐）") + "\n"
                + "时长：" + str(视频时长) + " 秒　分镜："
                + ("交给AI" if shot_n == 0 else str(shot_n) + " 镜")
                + "　运镜：" + 运镜 + "　光线：" + 光线
                + "　表情：" + 表情 + "　声音：" + 声音 + "\n"
                + ("【直通】不调用 AI，提示词原样输出"
                   if 每次运行 == "直通"
                   else "创意度：" + 创意度 + "　运行方式：" + 每次运行))
        if 风格预设 and 风格预设 != "不使用":
            head += "　风格：" + 风格预设
        extras = []
        if my_req:
            extras.append("我的具体要求")
        if spec.get("支持对白", False):
            extras.append("台词（用户写的）" if lines_txt else "台词（AI判断）")
        if extra_neg:
            extras.append("追加负面")
        if extras:
            head += "\n启用：" + "、".join(extras)
        if has_image and 直通:
            head += "\n接了图片，但直通模式不发请求，图没有上传"
        elif has_image:
            head += ("\n识图：" + "，".join(img_info)
                     + "　上传 %.2f MB" % img_mb)
        if ignored:
            head += "\n⚠ 该模型／模式不支持：" + "、".join(ignored) + "，已忽略"
        log.head(head)

        if 分镜_降级:
            log.warn(分镜_降级)
        for w in warns:
            log.warn(w)
        if has_image and img_mb > 3.0 and not cfg.get("domestic", False):
            log.warn("上传 %.2f MB 偏大，走代理有被掐断的风险（报 RemoteDisconnected）。" % img_mb)
            log.warn("真被掐断了就减少参考图数量，或换 DeepSeek／通义千问（国内直连，不受影响）。")

        log.sep()
        log.add("想法：" + (idea[:300] if idea else "（由图片／素材决定）"))
        log.add("→ 组装指令：" + 目标模型 + " 规范，共 " + str(len(system)) + " 字")
        log.detail("系统指令全文", system)
        log.flush_now()   # 开头信息先打出来：识图、预算、警告，用户一眼知道在跑什么

        # 接了图就压到最低，保证忠实于画面
        temperature = 0.3 if has_image else CREATIVITY.get(创意度, 0.7)

        # 超时自己算：接了图就要上传 base64，图越多越慢，识图模型本身也慢。
        # 不做成参数——用户不该为这种事操心。
        timeout_s = 90 + 30 * len(img_b64)

        # 调用。超时属于"网络不行"，重试同样会超时，所以只多试一次就收手，
        # 不让用户干等三轮（90×3＝四分半）。其它错误照常重试三次。
        last_err, prompt, zh, used, usage = None, "", "", model_show, {}
        timeouts = 0
        for attempt in ([] if 直通 else range(3)):
            try:
                use_model = 模型.strip() or _fallback_cache.get(服务商, "")
                try:
                    raw, used, usage = call_ai(服务商, use_model, key, system, user_msg,
                                               temperature, 代理地址,
                                               (img_b64 or None), 思考模式 == "开启", log,
                                               timeout_s)
                except RuntimeError as e:
                    msg = str(e)
                    model_problem = (msg.startswith("HTTP 404")
                                     or (msg.startswith("HTTP 400") and "model" in msg.lower()))
                    if 模型.strip() or not model_problem:
                        raise
                    log.warn("推荐模型不可用，正在查询可用模型…")
                    new_m = pick_fallback(服务商, fetch_models(服务商, key, 代理地址))
                    if not new_m:
                        raise
                    log.warn("已自动改用 " + new_m)
                    _fallback_cache[服务商] = new_m
                    raw, used, usage = call_ai(服务商, new_m, key, system, user_msg,
                                               temperature, 代理地址,
                                               (img_b64 or None), 思考模式 == "开启", log,
                                               timeout_s)

                prompt, zh, parse_err = _parse_prompt_zh(raw)
                if parse_err:
                    if attempt + 1 < 3:
                        log.warn("输出格式不对（" + parse_err + "），正在重试")
                        last_err = parse_err
                        continue
                    log.warn("输出格式始终不对（" + parse_err + "）。"
                             "下面的提示词是未经解析的原始回复，可能夹带多余文字，"
                             "用之前请先看一眼。")
                if not zh:
                    log.warn("未按 JSON 格式返回，已按纯文本处理（无中文说明）")
                if not prompt:
                    last_err = "模型返回了空内容"
                    log.warn(last_err + "，正在重试")
                    continue

                # ⚠ 台词留空时 AI 可能自己判断要加台词，<d> 里出现中文是合法的，
                #   所以这个检查只在明确没有台词、且模型要求全英文时才做。
                has_d_tag = "<d>" in prompt
                if (spec.get("语言") == "英文" and not lines_txt
                        and not has_d_tag and HAS_CN.search(prompt)):
                    if attempt == 0:
                        system += ("\n\nIMPORTANT: The prompt field MUST be in English only. "
                                   "Do not include any Chinese character in the prompt field.")
                        log.warn("该模型要求英文但返回了中文，已加强指令并重试")
                        continue
                    log.warn("重试后仍有中文，按现状输出")
                break

            except RuntimeError as e:
                last_err = str(e)
                adv = advice(last_err)
                log.warn("第 " + str(attempt + 1) + " 次失败：" + last_err[:150]
                         + ("　建议：" + adv if adv else ""))
                if any(("HTTP " + str(c)) in last_err for c in (400, 401, 402, 403, 404)):
                    break
            except Exception as e:
                last_err = repr(e)
                log.warn("第 " + str(attempt + 1) + " 次异常：" + last_err[:150])
                low = last_err.lower()
                if ("timed out" in low or "timeout" in low
                        or "remotedisconnected" in low or "unexpected_eof" in low
                        or "connectionreset" in low):
                    timeouts += 1
                    if timeouts >= 2:
                        log.warn("连续两次连接失败（超时或被对端掐断），不再重试——"
                                 "重试还是一样，白等。")
                        log.warn("多半是请求体太大走不动代理。办法：减少参考图数量，"
                                 "或换成国内服务商（DeepSeek、通义千问）。")
                        break

        if 直通:
            # 原样输出，一个字不动。后面的自检照常跑 —— 帮你查手改有没有把格式改坏。
            prompt, zh, used = idea, "", "（直通，未调用 AI）"
            log.add("→ 直通：不调用 AI，提示词原样输出，共 " + str(len(prompt)) + " 字")

        if not prompt:
            adv = advice(last_err or "")
            hint = "" if cfg.get("domestic", False) else "（海外服务商需要能访问外网）"
            msg = (服务商 + " 调用失败：" + str(last_err) + " " + hint
                   + ("\n建议：" + adv if adv else "")
                   + "\n详细信息已写入：" + ERROR_LOG)
            write_error_log("目标模型：" + 目标模型 + "\n服务商：" + 服务商
                            + "\n错误：" + str(last_err)
                            + "\n\n系统指令：\n" + system
                            + "\n\n用户输入：\n" + user_msg[:2000])
            log.warn("最终失败")
            log.flush()
            raise RuntimeError(msg)

        # ── 瞎编检查：模型没看到图却照写不误 ────────────────────────────────
        # 不是所有模型都能看图，而看不到图的模型【不会报错】——它会编一套
        # 外貌和场景出来，读着煞有介事，实际跟参考图毫无关系。用户拿去跑视频
        # 才发现不对，白烧一次生成。所以这里宁可报错，也不能把编的东西交出去。
        if has_image and not 直通:
            blind = [
                "IMAGES_NOT_VISIBLE",
                "reference images were not visible", "images were not visible",
                "no images were provided", "image was not provided",
                "cannot see the image", "unable to see the image",
                "i cannot view", "i can't see the image",
                "参考图片不可见", "图片不可见", "未提供图片", "无法看到图",
                "看不到图片", "未能看到图",
            ]
            low_p = prompt.lower() + "\n" + (zh or "").lower()
            hit = next((b for b in blind if b.lower() in low_p), None)
            if hit:
                msg = ("模型「" + str(used) + "」看不到图片，但它没报错，而是【编了一套】"
                       "外貌和场景——这份提示词和你接的参考图毫无关系，不能用。\n"
                       "证据：输出里出现了「" + hit + "」。\n"
                       "办法一：换一个真正支持看图的模型（同一家服务商里也分，"
                       "不是所有型号都能看图）；\n"
                       "办法二：换一个服务商。\n"
                       "⚠ 别直接重跑——同一个模型再跑十次也还是看不到图。")
                write_error_log(msg + "\n\n产出的提示词：\n" + prompt)
                log.warn("模型看不到图却编造了内容，已拦下")
                log.flush()
                raise RuntimeError(msg)

        # ── 补上对齐指令：尾帧模式把镜头数交给 AI 时走这条 ──
        # AI 已经按内容定好了镜头数，现在数出来，由节点生成那行官方措辞贴到最前面。
        # 措辞不经 AI 的手，保证一个字都不错。
        if align_later and prompt:
            import re as _re2
            _nums = {int(m) for m in _re2.findall(r"\[Shot\s*(\d+)\]", prompt)}
            _last = max(_nums) if _nums else 1
            # AI 要是没忍住自己写了一行，先去掉，避免重复
            _body = _re2.sub(r"^\s*How the reference pictures align[^\n]*\n+", "",
                             prompt, count=1)
            align = cls._align_line(mode_key, 视频时长, _last)
            prompt = align + "\n\n" + _body.lstrip()
            shot_n = _last          # 让后面的自检按实际镜头数来，不再误报
            log.add("→ 已补上官方对齐指令（AI 定了 " + str(_last) + " 个镜头）")

        直通问题 = []

        # 事后自检。发现【硬错误】（镜头数不符）时自己补跑一次 ——
        # 以前这里只打一句"你可以重跑一次"，但用户人不一定在旁边看着。
        def _selfcheck(prompt):
            hard_err = ""
            if spec.get("用官方格式", False):
                import re as _re
                # ⚠ 数镜头要数【不重复的编号】，不能数 [Shot N] 出现了几次。
                #   retention_analysis 里每个 <Subject> 都会把自己出现的镜头列一遍，
                #   4 个主体 × 4 个镜头 = 16 次，按出现次数数就会误报"写出 16 个镜头"。
                got = len({int(m) for m in _re.findall(r"\[Shot\s*(\d+)\]", prompt)})
                if shot_n and got and got != shot_n:
                    hard_err = ("镜头数不符：要求 " + str(shot_n) + " 个，实际写出 "
                                + str(got) + " 个")
                if align and align[:40] not in prompt:
                    _m = "开头缺少官方的图像对齐指令，模型可能对不准参考图"
                    log.warn(_m + "。" + ("请补上这一行：\n" + align if 直通 else "建议重跑一次。"))
                    if 直通:
                        直通问题.append(_m + "。应该是：" + align)
                if mode_key == "参考素材":
                    need = ["subject_definitions", "summary", "retention_analysis",
                            "detailed_description", "overall_soundscape", "non_diegetic_music"]
                else:
                    need = ["integrated_multimodal_description", "overall_soundscape",
                            "non_diegetic_music"]
                miss = [f for f in need if f not in prompt]
                if miss:
                    log.warn("官方字段缺失：" + "、".join(miss)
                             + ("。" if 直通 else "，建议重跑一次。"))
                    if 直通:
                        直通问题.append("官方字段缺失：" + "、".join(miss))
                if mode_key == "参考素材":
                    head_txt = prompt.split("detailed_description", 1)[0]
                    used = set(_re.findall(r"<(Subject|Picture|Video|Audio)\s*(\d+)>", prompt))
                    defined = set(_re.findall(r"<(Subject|Picture|Video|Audio)\s*(\d+)>", head_txt))
                    undef = sorted(used - defined)
                    if undef:
                        log.warn("正文用到了没在 subject_definitions 里定义的标签："
                                 + "、".join("<" + a + " " + b + ">" for a, b in undef)
                                 + "，建议重跑一次。")
                for bad in ("[Style]", "[Exclude]", "[Music]", "Framing:", "Camera:"):
                    if bad in prompt:
                        log.warn("出现了官方格式里没有的写法 " + bad
                                 + ("。" if 直通 else "，建议重跑一次。"))
                        if 直通:
                            直通问题.append("出现了官方格式里没有的写法：" + bad)
                        break

            return hard_err

        hard_err = _selfcheck(prompt)
        if hard_err and 直通:
            # 直通没有"重跑"可言，提示词是你手写的。把问题摆出来，由你决定改不改。
            log.warn("自检：" + hard_err)
            直通问题.append(hard_err)
        elif hard_err:
            log.warn(hard_err + "，正在自动重跑一次")
            try:
                fix = system + ("\n\n⚠ 上一次的输出" + hard_err
                                + "。重写一遍，镜头数必须正好是 " + str(shot_n)
                                + " 个，不多不少。")
                raw2, used2, usage2 = call_ai(服务商, used, key, fix, user_msg,
                                              temperature, 代理地址,
                                              (img_b64 or None), 思考模式 == "开启",
                                              log, timeout_s)
                p2, z2, e2 = _parse_prompt_zh(raw2)
                if not e2 and not _selfcheck(p2):
                    prompt, zh, usage = p2, z2, (usage2 or usage)
                    log.add("✓ 重跑后镜头数正确")
                else:
                    log.warn("重跑后仍不对，按第一次的结果输出。"
                             "可以把「分镜」直接改成实际写出的个数。")
            except Exception as _e:
                log.warn("重跑失败（" + str(_e)[:60] + "），按第一次的结果输出")

        # 节点自己算出来的提醒，附到中文说明末尾 —— 用户只看输出不看日志时也能看到
        notes = list(warns)
        if 直通:
            if 直通问题:
                notes = ["【直通自检】" + x for x in 直通问题] + notes
            else:
                notes.insert(0, "【直通自检】格式检查通过，没发现问题。")
        if 旧值残留:
            notes.append("「台词」框里是「" + 旧值残留 + "」——这是旧版本遗留的下拉值，"
                         "不是台词，已自动忽略。建议删掉这个节点重新拖一个，"
                         "否则别的输入项也可能串位。")
        if 分镜_降级:
            notes.append(分镜_降级)
        if ignored:
            notes.append("该模型／模式不支持：" + "、".join(ignored) + "，已忽略。")
        if notes:
            block = "\n\n—— ⚠ 需要注意 ——\n" + "\n".join("· " + n for n in notes)
            zh = (zh + block) if zh else block.lstrip("\n")

        tok = ""
        if usage:
            i_t = usage.get("prompt_tokens") or usage.get("input_tokens") or "?"
            o_t = usage.get("completion_tokens") or usage.get("output_tokens") or "?"
            tok = "　用量：输入 " + str(i_t) + " / 输出 " + str(o_t) + " token"
        log.add("← 收到响应 " + ("%.1f" % log.elapsed()) + " 秒" + tok)
        log.sep()
        if zh:
            log.add("中文说明：")
            log.add(zh, indent="    ")
            log.sep()
        log.add("提示词：")
        log.add(prompt, indent="    ")
        log.flush()

        return io.NodeOutput(prompt, zh)

    @classmethod
    def fingerprint_inputs(cls, **kwargs):
        if kwargs.get("每次运行") == "随机":
            return float("nan")
        return json.dumps(kwargs, sort_keys=True, ensure_ascii=False, default=str)


ensure_video_templates()

NODE_CLASS_MAPPINGS = {"VideoPromptWriter": VideoPromptWriter}
NODE_DISPLAY_NAME_MAPPINGS = {"VideoPromptWriter": "🎬 视频提示词（AI当导演）"}
