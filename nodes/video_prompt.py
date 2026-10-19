"""
🎬 视频提示词节点
菜单：自定义 → 高级提示词 → 视频提示词

设计原则：能用开关定死的，不让 AI 判断。
· 节点负责决策（切几个镜头、镜头动不动、光变不变、声音几层）——写死在指令里
· AI 只负责三件事：读懂参考图、把大白话翻译成专业描述、按固定骨架排版
· 变化预算：每多一个变化就多一个崩的机会，默认全部最稳，超预算时在日志里警告

· 目标模型由 ai_prompt_templates.json 的「视频模型规范」驱动，加一段就多一个选项
· 五种模式自动判断：文生 / 首帧 / 首尾帧 / 尾帧 / 参考素材
· 锁定要求全部用大白话填写，留空＝交给 AI
· 同时输出「提示词」（给模型）和「中文说明」（完整对照译文 + 决策回执）
"""

import json

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
        "【不属于你的职责】\n"
        "· 不要改动\u300c本次拍摄设置\u300d里的任何一项，那是用户在节点上定好的。\n"
        "· 不要增加设置里没有的变化：没让你切镜头就不许切，没让你动镜头就不许动，"
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
        "6. 信息密度比长度重要，不要为了显得详细而灌水。"
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
        "先逐张看图，把看到的内容抽象成 <Subject N>：这个人的长相发型服装、"
        "这个场景的陈设光线、这件道具的材质。用户在\u300c参考素材说明\u300d里指定了"
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
        "· 用户没写到的格子，照常由你补齐。"
    ),

    "视频_分段续写": (
        "本段要接在上一段后面，形成连续的长视频：\n"
        "1. 人物外貌、发型、服装、妆容必须与上一段完全一致，并在本段提示词里重新完整描述一遍"
        "（视频模型不记得上一段）。\n"
        "2. 场景、光线、色调延续上一段结尾的状态。\n"
        "3. 本段的起始动作要从上一段的结束姿态自然接上。\n"
        "4. 情绪和节奏在上一段基础上推进，不要重复同样的动作。"
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
        "zh 字段分两部分，中间用一行 \u300c—— 我替你补了什么 ——\u300d 分隔：\n"
        "\n"
        "第一部分：把 prompt 字段【完整逐句翻译成中文】，供用户核对。\n"
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
        "第二部分\u300c我替你补了什么\u300d，写 3~6 条，每条一行：\n"
        "· 用户没说、由你决定的关键格子，各写一条（选了什么、为什么）\n"
        "· 如果用户的要求和参考图冲突、或覆盖了拍摄设置，必须单列一条说明\n"
        "· 不要列用户自己写死的东西，那些不是你补的"
    ),

    # ===== 视频模型规范（下拉框内容由此生成）=====
    "视频模型规范": {
        "H3（海螺3）": {
            "版本": 4,
            "语言": "英文",
            "支持声音": True,
            "支持对白": True,
            "支持多镜头": True,
            "用官方格式": True,
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

# 普通模板的版本号。内容改进后在这里 +1，用户 json 里的旧版本会在重启时自动替换。
# 没列在这里的模板一旦写进 json 就永远以用户的为准。
TEMPLATE_VERSIONS = {
    "视频_导演守则": 3,
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
    "视频模式_参考素材": 3,
    "视频_全参考格式": 1,
    "视频_锁定要求": 1,
    "视频_负面默认": 3,
    "视频_负面写法": 3,
    "视频_输出要求": 4,
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
        if k == MODEL_SPEC_KEY:
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
            if k == MODEL_SPEC_KEY and isinstance(v, dict):
                merged = dict(VIDEO_TEMPLATES[MODEL_SPEC_KEY])
                for name, spec in v.items():
                    if isinstance(spec, dict) and str(spec.get("规范", "")).strip():
                        merged[name] = spec
                tpl[k] = merged
            elif isinstance(v, str) and v.strip() and k in VIDEO_TEMPLATES:
                tpl[k] = v
    except Exception:
        pass
    return tpl


def video_model_names():
    """下拉框内容：JSON 里有什么模型就显示什么"""
    try:
        names = list(load_video_templates().get(MODEL_SPEC_KEY, {}).keys())
        return names or list(VIDEO_TEMPLATES[MODEL_SPEC_KEY].keys())
    except Exception:
        return list(VIDEO_TEMPLATES[MODEL_SPEC_KEY].keys())


# ─── 变化预算 ──────────────────────────────────────────────────────────────────
# 每多一个变化就多一个崩的机会。额度随时长走，支出按项计价。
SHOT_COUNT = {"1镜到底": 1, "2镜": 2, "3镜": 3, "4镜": 4}
MOVE_COST = {"镜头不动": 0, "轻微": 1, "明显": 2, "交给AI": 1}
SOUND_COST = {"静音": 0, "环境音": 0, "环境音+动作音": 0, "再加配乐": 1}


def budget_of(时长, 分镜, 运镜, 光线, 表情, 声音):
    """返回 (额度, 支出, 明细列表)"""
    quota = max(1, int(时长) // 3)
    items, spend = [], 0
    n = SHOT_COUNT.get(分镜, 1)
    if n > 1:
        c = (n - 1) * 2
        spend += c
        items.append("切镜头 " + str(n - 1) + " 次（" + str(c) + "）")
    c = MOVE_COST.get(运镜, 0)
    if c:
        spend += c
        items.append("镜头运动 " + 运镜 + "（" + str(c) + "）")
    if 光线 == "允许变化":
        spend += 1
        items.append("光线变化（1）")
    if 表情 == "允许变化":
        spend += 1
        items.append("表情变化（1）")
    c = SOUND_COST.get(声音, 0)
    if c:
        spend += c
        items.append("配乐（" + str(c) + "）")
    return quota, spend, items


def cut_advice(分镜, 运镜, 光线, 表情, 声音):
    """超预算时按固定顺序建议砍哪一项"""
    if SOUND_COST.get(声音, 0):
        return "把「声音」从「再加配乐」降到「环境音+动作音」"
    if 光线 == "允许变化":
        return "把「光线」改回「保持不变」"
    if 表情 == "允许变化":
        return "把「表情」改回「保持不变」"
    if MOVE_COST.get(运镜, 0) >= 2:
        return "把「运镜」从「明显」降到「轻微」"
    if MOVE_COST.get(运镜, 0):
        return "把「运镜」改回「镜头不动」"
    if SHOT_COUNT.get(分镜, 1) > 1:
        return "减少镜头数量，或者把时长加长"
    return "把时长加长"


# ─── 结果解析 ──────────────────────────────────────────────────────────────────
def _parse_prompt_zh(text):
    """解析 {"prompt": ..., "zh": ...}；失败时整段当提示词"""
    text = clean_text(text)
    import re
    m = re.search(r"\{.*\}", text, flags=re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
            p = clean_text(str(obj.get("prompt", "")))
            if p:
                return p, clean_text(str(obj.get("zh", "")))
        except Exception:
            pass
    return text, ""


# ─── 节点 ──────────────────────────────────────────────────────────────────────
class VideoPromptWriter:

    @classmethod
    def INPUT_TYPES(cls):
        providers = _provider_names()
        models = video_model_names()
        return {
            "required": {
                "你的想法": ("STRING", {
                    "multiline": True, "default": "",
                    "placeholder": "用大白话写想要什么，例如：一个女人在地下室跳恰恰舞。\n写得越少 AI 发挥越多，写得越具体越贴近你的想法。",
                }),
                "目标模型": (models if models else ["（请重启ComfyUI）"], {
                    "tooltip": "列表来自 ai_prompt_templates.json 的「视频模型规范」。\n"
                               "想支持新模型，在那里照格式加一段规范，重启后即出现在这里。",
                }),
                "视频时长": ("INT", {
                    "default": 5, "min": 1, "max": 60,
                    "tooltip": "秒。H3 支持 4~15 秒整数。\n时长同时决定了变化预算：越短能承受的变化越少",
                }),

                # ── 变化预算：默认全部最稳 ──
                "分镜": (["1镜到底", "2镜", "3镜", "4镜"], {
                    "default": "1镜到底",
                    "tooltip": "切几个镜头。预算支出：每切一刀 2 点\n"
                               "每切一刀，人物在切口处变样的概率就上升一次。\n"
                               "5 秒基本只能 1 镜到底，10 秒以上才适合多镜头",
                }),
                "运镜": (["镜头不动", "轻微", "明显", "交给AI"], {
                    "default": "镜头不动",
                    "tooltip": "摄影机动不动。预算支出：不动 0 / 轻微 1 / 明显 2 / 交给AI 1\n"
                               "镜头一动，人物要在运动中保持一致，漂移概率上升。\n"
                               "不确定就用「镜头不动」，这是最稳的",
                }),
                "光线": (["保持不变", "允许变化"], {
                    "default": "保持不变",
                    "tooltip": "预算支出：允许变化 1 点\n"
                               "中途变光要在时间轴上插值光照，容易出现闪烁",
                }),
                "表情": (["保持不变", "允许变化"], {
                    "default": "保持不变",
                    "tooltip": "预算支出：允许变化 1 点\n"
                               "表情突变那一帧最容易换脸。选「保持不变」时情绪仍可细微递进，\n"
                               "只是不会在某一秒切换成另一种情绪",
                }),
                "声音": (["静音", "环境音", "环境音+动作音", "再加配乐"], {
                    "default": "环境音",
                    "tooltip": "预算支出：再加配乐 1 点\n"
                               "声音层越多，模型要同时对齐的东西越多，容易糊成一团。\n"
                               "不支持声音的模型会自动忽略此项",
                }),

                "服务商": (providers if providers else ["（请重启ComfyUI）"], {
                    "default": providers[0] if providers else None,
                    "tooltip": "调用哪家 AI 来写提示词。接了图片时需要支持看图的服务商",
                }),
                "模型": ("STRING", {
                    "default": "", "placeholder": "留空用推荐模型，或点下方按钮选择",
                }),
                "创意度": ("FLOAT", {
                    "default": 0.7, "min": 0.0, "max": 1.5, "step": 0.05,
                    "tooltip": "越高画面设计越大胆。接了图片时自动使用低数值，保证忠实于画面",
                }),
                "每次运行": (["固定", "随机"], {
                    "default": "固定",
                    "tooltip": "固定：内容不变时复用上次结果，不重复扣费；随机：每次重新设计一版",
                }),
            },
            "optional": {
                "首帧图": ("IMAGE", {
                    "tooltip": "视频的第一帧。接了它就是图生视频，AI 会先读懂画面再设计运动。\n"
                               "⚠ 不能和「参考素材说明」同时使用",
                }),
                "尾帧图": ("IMAGE", {
                    "tooltip": "视频的最后一帧。和首帧一起接 = 首尾帧模式；只接它 = 尾帧模式。\n"
                               "⚠ 不能和「参考图」同时使用",
                }),
                "参考图1": ("IMAGE", {
                    "tooltip": "全参考模式的素材库。接了任意一张就进入参考素材生视频。\n"
                               "参考图不是画面，是\u300c这个人长什么样\u300d\u300c这个场景长什么样\u300d。\n"
                               "官方上限 9 张，接满一个自动冒出下一个。\n"
                               "⚠ 不能和首帧图／尾帧图同时使用",
                }),
                "参考图2": ("IMAGE", {
                    "tooltip": "全参考模式的素材库。接了任意一张就进入参考素材生视频。\n"
                               "参考图不是画面，是\u300c这个人长什么样\u300d\u300c这个场景长什么样\u300d。\n"
                               "官方上限 9 张，接满一个自动冒出下一个。\n"
                               "⚠ 不能和首帧图／尾帧图同时使用",
                }),
                "参考图3": ("IMAGE", {
                    "tooltip": "全参考模式的素材库。接了任意一张就进入参考素材生视频。\n"
                               "参考图不是画面，是\u300c这个人长什么样\u300d\u300c这个场景长什么样\u300d。\n"
                               "官方上限 9 张，接满一个自动冒出下一个。\n"
                               "⚠ 不能和首帧图／尾帧图同时使用",
                }),
                "参考图4": ("IMAGE", {
                    "tooltip": "全参考模式的素材库。接了任意一张就进入参考素材生视频。\n"
                               "参考图不是画面，是\u300c这个人长什么样\u300d\u300c这个场景长什么样\u300d。\n"
                               "官方上限 9 张，接满一个自动冒出下一个。\n"
                               "⚠ 不能和首帧图／尾帧图同时使用",
                }),
                "参考图5": ("IMAGE", {
                    "tooltip": "全参考模式的素材库。接了任意一张就进入参考素材生视频。\n"
                               "参考图不是画面，是\u300c这个人长什么样\u300d\u300c这个场景长什么样\u300d。\n"
                               "官方上限 9 张，接满一个自动冒出下一个。\n"
                               "⚠ 不能和首帧图／尾帧图同时使用",
                }),
                "参考图6": ("IMAGE", {
                    "tooltip": "全参考模式的素材库。接了任意一张就进入参考素材生视频。\n"
                               "参考图不是画面，是\u300c这个人长什么样\u300d\u300c这个场景长什么样\u300d。\n"
                               "官方上限 9 张，接满一个自动冒出下一个。\n"
                               "⚠ 不能和首帧图／尾帧图同时使用",
                }),
                "参考图7": ("IMAGE", {
                    "tooltip": "全参考模式的素材库。接了任意一张就进入参考素材生视频。\n"
                               "参考图不是画面，是\u300c这个人长什么样\u300d\u300c这个场景长什么样\u300d。\n"
                               "官方上限 9 张，接满一个自动冒出下一个。\n"
                               "⚠ 不能和首帧图／尾帧图同时使用",
                }),
                "参考图8": ("IMAGE", {
                    "tooltip": "全参考模式的素材库。接了任意一张就进入参考素材生视频。\n"
                               "参考图不是画面，是\u300c这个人长什么样\u300d\u300c这个场景长什么样\u300d。\n"
                               "官方上限 9 张，接满一个自动冒出下一个。\n"
                               "⚠ 不能和首帧图／尾帧图同时使用",
                }),
                "参考图9": ("IMAGE", {
                    "tooltip": "全参考模式的素材库。接了任意一张就进入参考素材生视频。\n"
                               "参考图不是画面，是\u300c这个人长什么样\u300d\u300c这个场景长什么样\u300d。\n"
                               "官方上限 9 张，接满一个自动冒出下一个。\n"
                               "⚠ 不能和首帧图／尾帧图同时使用",
                }),

                # ── 锁定要求：全部大白话，留空＝AI 发挥 ──
                "画面要求": ("STRING", {
                    "multiline": True, "default": "", "advanced": True,
                    "placeholder": "用大白话写，留空＝AI决定。例如：\n我要脸的大特写\n从背后拍她\n背景糊掉",
                    "tooltip": "锁定景别、机位高度、机位方位、构图、景深。\n填了就钉死，AI 不许改写",
                }),
                "运镜要求": ("STRING", {
                    "multiline": True, "default": "", "advanced": True,
                    "placeholder": "用大白话写，留空＝按上面的「运镜」下拉。例如：\n跟着她走\n先不动，最后慢慢拉远",
                    "tooltip": "填了会覆盖上面的「运镜」下拉，以你写的为准",
                }),
                "动作与表演": ("STRING", {
                    "multiline": True, "default": "", "advanced": True,
                    "placeholder": "用大白话写，留空＝AI决定。例如：\n她要甩一下头发\n眼神一直盯着镜头\n手里一直拿着酒杯",
                    "tooltip": "锁定动作、表情眼神、服装道具",
                }),
                "光线与色调": ("STRING", {
                    "multiline": True, "default": "", "advanced": True,
                    "placeholder": "用大白话写，留空＝AI决定。例如：\n昏暗的红光\n像傍晚那种暖\n只有一盏顶灯",
                    "tooltip": "锁定光线方向质感和整体色板",
                }),
                "声音要求": ("STRING", {
                    "multiline": True, "default": "", "advanced": True,
                    "placeholder": "用大白话写，留空＝AI决定。例如：\n要有高跟鞋踩地的声音\n不要配乐\n外面有雨声",
                    "tooltip": "锁定环境音、动作音效、配乐",
                }),
                "分镜节奏": ("STRING", {
                    "multiline": True, "default": "", "advanced": True,
                    "placeholder": "多镜头时用，留空＝AI分镜。例如：\n第1个镜头她推门进来\n第2个镜头特写她的手\n第3个镜头拉远看全场",
                    "tooltip": "逐个镜头写发生什么。镜头个数以上面的「分镜」下拉为准",
                }),

                "台词": ("STRING", {
                    "multiline": True, "default": "", "advanced": True,
                    "placeholder": "留空=无对白。多镜头时可以写「第1个镜头：……」",
                    "tooltip": "台词保持你写的语言原样不翻译，AI 会设计音色并要求口型同步",
                }),
                "负面要求": (["关闭", "使用默认", "自定义"], {
                    "default": "使用默认", "advanced": True,
                    "tooltip": "提示词末尾的排除列表，是 H3 最主要的风格控制手段，建议保持开启。\n"
                               "选「使用默认」时 AI 还会根据本片内容自动补几条具体的",
                }),
                "负面内容": ("STRING", {
                    "multiline": True, "default": "", "advanced": True,
                    "placeholder": "负面要求选「自定义」时填这里，可用中文写，例如：不要现代建筑、不要其他人入镜",
                }),
                "参考素材说明": ("STRING", {
                    "multiline": True, "default": "", "advanced": True,
                    "placeholder": "可留空。留空时 AI 自己看图决定锁什么。\n想指定就写，例如：\n图1是女主角，锁住脸和发型，别的不要\n图2只取这个亭子，人物不要",
                    "tooltip": "给参考图补充说明，告诉 AI 每张图「只取什么、不沿用什么」。\n"
                               "AI 会转成官方的 <Subject N> / <Picture N> 定义并在每个镜头重复引用。\n"
                               "没接参考图时，也可以只用文字描述素材（AI 看不到图，效果打折）",
                }),
                "上一段提示词": ("STRING", {
                    "multiline": True, "default": "", "advanced": True,
                    "placeholder": "做长视频时，把上一段的输出接进来或粘贴进来",
                    "tooltip": "填了就进入分段续写：人物、服装、场景、情绪自动衔接上一段",
                }),
                "风格方向": ("STRING", {
                    "default": "", "advanced": True,
                    "placeholder": "如：电影感 / 纪录片 / 日系清新 / 赛博朋克 / 老港片",
                }),
                "给AI的额外要求": ("STRING", {
                    "multiline": True, "default": "", "advanced": True,
                    "placeholder": "上面几个框装不下的，写在这里",
                }),
                "思考模式": (["关闭", "开启"], {
                    "default": "关闭", "advanced": True,
                    "tooltip": "开启后 AI 先推理再回答，多镜头和复杂要求更稳，但更慢更贵",
                }),
                "API密钥": ("STRING", {
                    "default": "", "advanced": True,
                    "placeholder": "建议留空，在 ai_prompt_keys.json 里填写",
                }),
                "代理地址": ("STRING", {
                    "default": "", "advanced": True,
                    "placeholder": "海外服务商用，如 127.0.0.1:7890；留空用系统代理",
                }),
                "自定义指令": ("STRING", {
                    "multiline": True, "default": "", "advanced": True,
                    "placeholder": "填了会完全替换本次的系统指令，临时试验用",
                }),
                "详细日志": (["关闭", "开启"], {
                    "default": "关闭", "advanced": True,
                    "tooltip": "额外打印完整系统指令、请求参数和 API 原始返回",
                }),
            },
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("提示词", "中文说明")
    FUNCTION = "run"
    CATEGORY = "自定义/高级提示词/视频提示词"
    OUTPUT_NODE = False

    # ── 图像对齐指令：官方固定措辞，由节点直接生成，不让 AI 写 ──────────────
    def _align_line(self, mode_key, 时长, shot_n):
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

    # ── 拍摄设置 → 硬命令 ────────────────────────────────────────────────────
    def _shoot_block(self, spec, 时长, 分镜, 运镜, 光线, 表情, 声音, 运镜被覆盖):
        """把节点上的开关翻译成不容商量的指令。AI 没有发挥空间。"""
        n = SHOT_COUNT.get(分镜, 1)
        multi = spec.get("支持多镜头", False)
        lines = ["【本次拍摄设置】以下每一条都是用户在节点上定好的，"
                 "必须照做，不许更改，不许增加这里没有的变化。"]

        if not multi or n == 1:
            lines.append("· 分镜：只有 [Shot 1] 一个连续镜头，全程不许切镜头，"
                         "不要出现第二个 [Shot N]。")
        else:
            lines.append("· 分镜：正好 " + str(n) + " 个镜头，不多不少。"
                         "[Shot 1] 不带时间戳，之后每个镜头写 [Shot N] At MM:SS.mmm, ...，"
                         "切点严格递增且都落在 " + str(时长) + " 秒之内，每个镜头至少 2 秒。")

        if 运镜被覆盖:
            lines.append("· 运镜：以下面「用户钉死的要求」里写的为准。")
        elif 运镜 == "镜头不动":
            lines.append("· 运镜：固定机位。必须在镜头正文里写出一句 "
                         "the camera holds a static shot，不许有任何镜头运动。")
        elif 运镜 == "轻微":
            lines.append("· 运镜：只允许一个幅度很小的慢速运动，官方词表里选一种运动类型，"
                         "写成 ... with small amplitude at slow speed，全片只用这一个运动。")
        elif 运镜 == "明显":
            lines.append("· 运镜：允许一个明确的镜头运动，从官方词表里选运动类型，"
                         "写清类型＋幅度＋速度。一个镜头里只用一种运动方式，不许中途换成另一种。")
        else:
            lines.append("· 运镜：由你决定，但只能用官方词表里的运动类型，"
                         "一个镜头里只用一种，且优先考虑 Static Shot——不动是最稳的选择。")

        if 光线 == "保持不变":
            lines.append("· 光线：全程不变。不要设计任何光线变化，"
                         "Light 字段结尾写 unchanged throughout。")
        else:
            lines.append("· 光线：允许一次光线变化，写清从什么变到什么、在第几秒。")

        if 表情 == "保持不变":
            lines.append("· 表情：不要设计表情突变。情绪可以细微递进，"
                         "但不许在某一秒切换成另一种情绪。")
        else:
            lines.append("· 表情：允许一次情绪转变，写清在第几秒、从什么变到什么，"
                         "并让它在 1 秒内渐变完成，不要瞬间切换。")

        if not spec.get("支持声音", False) or 声音 == "静音":
            lines.append("· 声音：本次全程静音，overall_soundscape 与 non_diegetic_music 都写 N/A。")
        elif 声音 == "环境音":
            lines.append("· 声音：overall_soundscape 只写环境底噪，不要动作音效；"
                         "non_diegetic_music 写 N/A。")
        elif 声音 == "环境音+动作音":
            lines.append("· 声音：overall_soundscape 写环境底噪 + 每个明显动作最多 1 个音效；"
                         "non_diegetic_music 写 N/A。")
        else:
            lines.append("· 声音：overall_soundscape 写环境底噪 + 动作音效，"
                         "non_diegetic_music 写画外配乐的配器、速度、节奏和力度变化。")

        lines.append("· 以上设置是为了保证画面稳定。每增加一个变化，"
                     "人物漂移和画面崩坏的概率就上升一次，所以不要自作主张加戏。")
        return "\n".join(lines)

    # ── 锁定要求 → 硬约束 ────────────────────────────────────────────────────
    def _locks_block(self, tpl, locks):
        if not locks:
            return ""
        body = [tpl["视频_锁定要求"], ""]
        for name, text in locks:
            body.append("· " + name + "：" + text)
        return "\n".join(body)

    # ── 组装系统指令 ─────────────────────────────────────────────────────────
    def _build_system(self, tpl, spec, 时长, mode_key, shoot, locks_text, align,
                      has_prev, has_lines, neg_text, ref_text,
                      声音, 风格方向, 额外要求, 自定义指令):
        if 自定义指令 and 自定义指令.strip():
            parts = [自定义指令.strip()]
        else:
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

            parts.append("时长：本段视频 " + str(时长) + " 秒，"
                         "动作总量要与这个长度匹配，不要安排超出时长的情节。")
            parts.append(shoot)
            if locks_text:
                parts.append(locks_text)

            if spec.get("支持声音", False) and 声音 != "静音":
                parts.append(tpl["视频_声景守则"])
                if 声音 == "再加配乐":
                    parts.append(tpl["视频_配乐守则"])

            if has_prev:
                parts.append(tpl["视频_分段续写"])
            if has_lines and spec.get("支持对白", False):
                parts.append(tpl["视频_对白"])
            if neg_text:
                parts.append(tpl["视频_负面写法"])

        if 风格方向 and 风格方向.strip():
            parts.append("风格方向：整体视觉风格按 " + 风格方向.strip()
                         + " 来统一（用户原文写了的风格优先）。")
        if 额外要求 and 额外要求.strip():
            parts.append("额外要求：" + 额外要求.strip())

        parts.append(tpl["视频_输出要求"])
        return "\n\n".join(p for p in parts if p and p.strip())

    # ── 主流程 ────────────────────────────────────────────────────────────────
    def run(self, 你的想法, 目标模型, 视频时长, 分镜, 运镜, 光线, 表情, 声音,
            服务商, 模型, 创意度, 每次运行,
            首帧图=None, 尾帧图=None,
            参考图1=None, 参考图2=None, 参考图3=None, 参考图4=None, 参考图5=None,
            参考图6=None, 参考图7=None, 参考图8=None, 参考图9=None,
            画面要求="", 运镜要求="", 动作与表演="", 光线与色调="",
            声音要求="", 分镜节奏="",
            台词="", 负面要求="使用默认", 负面内容="", 参考素材说明="",
            上一段提示词="", 风格方向="",
            给AI的额外要求="", 思考模式="关闭", API密钥="", 代理地址="",
            自定义指令="", 详细日志="关闭"):

        log = Log(verbose=(详细日志 == "开启"))
        tpl = load_video_templates()
        specs = tpl.get(MODEL_SPEC_KEY, {})
        spec = specs.get(目标模型) or next(iter(specs.values()), {})

        idea = (你的想法 or "").strip()
        prev = (上一段提示词 or "").strip()
        lines_txt = (台词 or "").strip()
        ref = (参考素材说明 or "").strip()

        # 参考图（全参考模式的素材库）
        ref_imgs = [(i, im) for i, im in enumerate(
            [参考图1, 参考图2, 参考图3, 参考图4, 参考图5,
             参考图6, 参考图7, 参考图8, 参考图9], start=1) if im is not None]

        # 模式判断
        has_fl = (首帧图 is not None) or (尾帧图 is not None)
        if (ref_imgs or ref) and has_fl:
            msg = ("参考素材模式不能和首帧／尾帧混用（MiniMax 官方限制）。\n"
                   "要么断开参考图和「参考素材说明」，要么断开首帧图／尾帧图。")
            write_error_log(msg)
            raise RuntimeError(msg)

        if ref_imgs:
            mode_key, mode_name = "参考素材", "参考素材生视频"
        elif 首帧图 is not None and 尾帧图 is not None:
            mode_key, mode_name = "首尾帧", "首尾帧生视频"
        elif 首帧图 is not None:
            mode_key, mode_name = "首帧", "首帧生视频"
        elif 尾帧图 is not None:
            mode_key, mode_name = "尾帧", "尾帧生视频"
        elif ref:
            mode_key, mode_name = "参考素材", "参考素材生视频"
        else:
            mode_key, mode_name = "文生视频", "文生视频"

        if ref_imgs:
            images = ref_imgs
        else:
            images = [(i, im) for i, im in [(1, 首帧图), (2, 尾帧图)] if im is not None]
        has_image = bool(images)

        if not idea and not has_image and not prev and not ref:
            raise RuntimeError("请在「你的想法」里写点什么，或接入一张图片")

        # 服务商检查
        cfg = get_all_providers().get(服务商, {})
        if cfg.get("style") == "mt":
            raise RuntimeError(服务商 + " 是机器翻译，不能用来设计视频提示词，请换 AI 服务商")
        if has_image and not cfg.get("vision", False):
            msg = (服务商 + " 不支持图像输入（vision=false）。\n"
                   "解决办法：在 ai_prompt_keys.json 里把该服务商改成字典形式并加上 \"vision\": true，"
                   "或换用通义千问、Gemini、OpenAI、Claude 等支持视觉的服务商。")
            write_error_log(msg)
            raise RuntimeError(msg)

        key = load_key(服务商, API密钥)
        if not key:
            msg = "没有找到 " + 服务商 + " 的 API Key，请填写 ai_prompt_keys.json"
            write_error_log(msg)
            raise RuntimeError(msg)

        # 图片
        img_b64, img_info = [], []
        for idx, im in images:
            b64, o, n = img_to_b64(im)
            img_b64.append((idx, b64))
            if ref_imgs:
                label = "参考图" + str(idx)
            else:
                label = "首帧" if idx == 1 else "尾帧"
            img_info.append(label + " " + str(o[0]) + "×" + str(o[1])
                            + ("" if o == n else "→" + str(n[0]) + "×" + str(n[1])))

        # 模型能力检查
        ignored = []
        if 声音 != "静音" and not spec.get("支持声音", False):
            ignored.append("声音")
        if lines_txt and not spec.get("支持对白", False):
            ignored.append("台词")
        shot_n = SHOT_COUNT.get(分镜, 1)
        if shot_n > 1 and not spec.get("支持多镜头", False):
            ignored.append("多镜头")
            分镜 = "1镜到底"
            shot_n = 1
        if mode_key == "首尾帧" and shot_n > 1:
            ignored.append("多镜头（首尾帧模式不能切镜头）")
            分镜 = "1镜到底"
            shot_n = 1

        # 变化预算
        quota, spend, spend_items = budget_of(视频时长, 分镜, 运镜, 光线, 表情, 声音)
        over = spend > quota

        # 锁定要求
        locks = []
        for name, text in (("画面", 画面要求), ("运镜", 运镜要求),
                           ("动作与表演", 动作与表演), ("光线与色调", 光线与色调),
                           ("声音", 声音要求), ("分镜节奏", 分镜节奏)):
            t = (text or "").strip()
            if t:
                locks.append((name, t))
        locks_text = self._locks_block(tpl, locks)

        neg_text = ""
        if 负面要求 == "使用默认":
            neg_text = tpl["视频_负面默认"]
        elif 负面要求 == "自定义":
            neg_text = (负面内容 or "").strip()

        shoot = self._shoot_block(spec, 视频时长, 分镜, 运镜, 光线, 表情, 声音,
                                  bool((运镜要求 or "").strip()))
        align = self._align_line(mode_key, 视频时长, shot_n) if spec.get("用官方格式", False) else ""

        system = self._build_system(
            tpl, spec, 视频时长, mode_key, shoot, locks_text, align,
            bool(prev), bool(lines_txt), neg_text, ref,
            声音, 风格方向, 给AI的额外要求, 自定义指令,
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
        elif ref or ref_imgs:
            user_parts.append("用户没有写文字要求，请根据参考素材自行设计这段视频。")
        if prev:
            user_parts.append("上一段的提示词（本段要与它衔接）：\n" + prev)
        if lines_txt:
            user_parts.append("本段台词（原文保留，不要翻译）：\n" + lines_txt)
        if ref_imgs:
            user_parts.append("本次接入了 " + str(len(ref_imgs)) + " 张参考图，编号依次为 "
                              + "、".join("参考图" + str(i) for i, _ in ref_imgs)
                              + "，分别对应 "
                              + "、".join("<Picture " + str(i) + ">" for i, _ in ref_imgs)
                              + "。请逐张看图后再写 subject_definitions。")
        if ref:
            user_parts.append("参考素材说明：\n" + ref)
        if neg_text:
            user_parts.append("不希望出现的内容：\n" + neg_text)
        user_msg = "\n\n".join(user_parts)

        # 日志头
        model_show = (模型.strip() or _fallback_cache.get(服务商) or cfg.get("model", ""))
        head = ("目标模型：" + 目标模型 + "　模式：" + mode_name + "\n"
                + "服务商：" + 服务商 + "　模型：" + model_show
                + ("（手动指定）" if 模型.strip() else "（推荐）") + "\n"
                + "时长：" + str(视频时长) + " 秒　分镜：" + 分镜
                + "　运镜：" + 运镜 + "　光线：" + 光线
                + "　表情：" + 表情 + "　声音：" + 声音 + "\n"
                + "运行方式：" + 每次运行
                + ("　风格：" + 风格方向.strip() if 风格方向.strip() else ""))

        head += ("\n变化预算：" + str(spend) + " / " + str(quota)
                 + ("　" + "，".join(spend_items) if spend_items else "　（全部默认，最稳）"))
        if locks:
            head += "\n已锁定：" + "、".join(n for n, _ in locks)
        extras = []
        if prev:
            extras.append("分段续写")
        if lines_txt and spec.get("支持对白", False):
            extras.append("对白")
        if ref:
            extras.append("参考素材")
        if neg_text:
            extras.append("负面要求")
        if extras:
            head += "\n启用：" + "、".join(extras)
        if has_image:
            head += "\n识图：" + "，".join(img_info)
        if ignored:
            head += "\n⚠ 该模型／模式不支持：" + "、".join(ignored) + "，已忽略"
        if 自定义指令.strip():
            head += "\n⚠ 使用了自定义指令（已替换默认指令）"
        log.head(head)

        if over:
            log.warn("变化预算超了 " + str(spend - quota) + " 点（" + str(视频时长)
                     + " 秒只够 " + str(quota) + " 点）。画面崩坏和人物漂移的概率会明显上升。")
            log.warn("建议：" + cut_advice(分镜, 运镜, 光线, 表情, 声音))

        log.sep()
        log.add("想法：" + (idea[:300] if idea else "（由图片／素材决定）"))
        log.add("→ 组装指令：" + 目标模型 + " 规范，共 " + str(len(system)) + " 字")
        log.detail("系统指令全文", system)

        temperature = 0.3 if has_image else 创意度

        # 调用
        last_err, prompt, zh, used, usage = None, "", "", model_show, {}
        for attempt in range(3):
            try:
                use_model = 模型.strip() or _fallback_cache.get(服务商, "")
                try:
                    raw, used, usage = call_ai(服务商, use_model, key, system, user_msg,
                                               temperature, 代理地址,
                                               (img_b64 or None), 思考模式 == "开启", log)
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
                                               (img_b64 or None), 思考模式 == "开启", log)

                prompt, zh = _parse_prompt_zh(raw)
                if not zh:
                    log.warn("未按 JSON 格式返回，已按纯文本处理（无中文说明）")
                if not prompt:
                    last_err = "模型返回了空内容"
                    log.warn(last_err + "，正在重试")
                    continue

                if spec.get("语言") == "英文" and not lines_txt and HAS_CN.search(prompt):
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

        # 事后自检：官方格式的几处硬要求
        if spec.get("用官方格式", False):
            import re as _re
            got = len(_re.findall(r"\[Shot\s*\d+\]", prompt))
            if got and got != shot_n:
                log.warn("要求 " + str(shot_n) + " 个镜头，实际写出 " + str(got)
                         + " 个。可以重跑一次，或把「分镜」改成 " + str(got) + " 镜。")
            if align and align[:40] not in prompt:
                log.warn("官方要求的图像对齐指令没有原样出现在提示词开头，模型可能对不准参考图。"
                         "建议重跑一次。")
            if mode_key == "参考素材":
                need = ["subject_definitions", "summary", "retention_analysis",
                        "detailed_description", "overall_soundscape", "non_diegetic_music"]
            else:
                need = ["integrated_multimodal_description", "overall_soundscape",
                        "non_diegetic_music"]
            miss = [f for f in need if f not in prompt]
            if miss:
                log.warn("官方字段缺失：" + "、".join(miss) + "，建议重跑一次。")
            for bad in ("[Style]", "[Exclude]", "[Music]", "Framing:", "Camera:"):
                if bad in prompt:
                    log.warn("出现了官方格式里没有的写法 " + bad + "，建议重跑一次。")
                    break

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

        return (prompt, zh)

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        if kwargs.get("每次运行") == "随机":
            return float("nan")
        return json.dumps(kwargs, sort_keys=True, ensure_ascii=False, default=str)


ensure_video_templates()

NODE_CLASS_MAPPINGS = {"VideoPromptWriter": VideoPromptWriter}
NODE_DISPLAY_NAME_MAPPINGS = {"VideoPromptWriter": "🎬 视频提示词（AI当导演）"}
