from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.core.models import AgentLLMOutput, AgentSessionState, ChatRequest


@dataclass(frozen=True)
class QingmengTrigger:
    npc_id: str
    keywords: tuple[str, ...]
    content: str
    clue_id: str = ""
    clue_title: str = ""
    clue_content: str = ""
    requires_any_clue: tuple[str, ...] = ()


TRUTH_CHAIN = (
    "春风阁被焚，是因为姐姐们无意中知道了皇室血脉被调换的秘密。",
    "刘容景是被调换的皇长子，真正的刘怀琛早已死去；苏宴卿借用了他的名字和身份。",
    "安佑帝发现了冒名顶替的秘密，被苏宴卿用蛇鞭草慢性毒杀。",
    "梵音为掩盖血脉真相，在朱雀门神谕之宴上鸩杀皇子。",
    "刘容景和幻心兄长幻情都因追查同一条真相链而被灭口。",
)


CASE_TRUTH = (
    "大安王朝，神权与皇权交织。小离被遗弃在春风阁门口，由仙姝、粉桃等姐姐养大。"
    "春风阁被焚不是意外，姐姐们因掌握皇室血脉与神子罪行的秘密被灭口。"
    "小离被刘容景救下后改名苏清梦，进入宰相府与皇家私堂。"
    "刘容景是被调换的皇长子；真正的皇长子刘怀琛早已死去，苏宴卿只是冒名顶替。"
    "安佑帝发现此事后，被苏宴卿用蛇鞭草慢性毒杀。"
    "梵音为维护神权和掩盖血脉真相，在朱雀门神谕之宴前独入朱雀楼，在酒中下毒鸩杀皇子。"
    "刘祈之装结巴十六年自保，并目睹梵音独入朱雀楼；刘昭月为保护宋家军卷入争斗，也注意到梵音异常；"
    "幻心与兄长幻情证明刘容景追查的同一真相链导致多人被灭口。"
)


NPC_SKILL_FILES = {
    "xianshu": "仙姝.json",
    "fentao": "粉桃.json",
    "liurongjing": "刘容景.json",
    "liuqizhi": "刘祈之.json",
    "liuzhaoyue": "刘昭月.json",
    "huanxin": "幻心.json",
    "suyanqing": "苏宴卿.json",
    "fanyin": "梵音.json",
}


GUIDE_REPLIES: dict[str, tuple[str, ...]] = {
    "xianshu": (
        "都自家姐妹，还问好啊。来，摸一下这碗黄豆粉，帮我看看磨得够不够细。",
        "傻站着干什么？过来帮我看看这碗黄豆粉，我手抖得厉害，磨不均匀。",
        "你来得正好。这碗黄豆粉我磨了半天，你试试手感。",
    ),
    "fentao": (
        "小离来啦。来，姐姐这儿有颗糖丸，刚买的，你尝尝甜不甜。",
        "别站着了，过来坐。你看这颗糖丸，是不是和你小时候吃的一个味？",
        "我正想找你呢。你看这颗糖丸，我记得阿鸢那孩子也爱吃这个。",
    ),
    "liurongjing": (
        "清梦，你来得正好。我正想找人说说这铃铛的事——你可知这铃铛的来历？",
        "别站着，坐下吧。我最近总想起一些往事，关于我的身世，关于这枚铃铛。",
        "你来得巧。我正想找个人说说话——关于那封信的事。",
    ),
    "liuqizhi": (
        "（环顾四周确认无人，低声）你来得正好。我正想找人说说朱雀门那晚的事。",
        "别装了，你和我是一样的人。过来，我有话跟你说——关于朱雀门。",
        "你知不知道，我装结巴装了十六年？所有人都以为我是个废物。但废物也有眼睛。",
    ),
    "fanyin": (
        "（缓缓睁眼）你来了。我料到你会来。关于朱雀门，你没有什么想问的吗？",
        "凡人总是想探寻神谕之宴的秘密。你也不例外。",
        "你站在这里，不就是想问朱雀门的事吗？问吧。",
    ),
    "suyanqing": (
        "（微笑）清梦来了。我正想找人聊聊安佑帝陛下的事——你可有兴趣？",
        "你最近似乎对刘怀琛这个名字很感兴趣。不妨直说。",
        "我听说你在查安佑帝陛下的死因。有什么发现吗？",
    ),
    "liuzhaoyue": (
        "你来得正好。我正想找人说说宋家军的事——你可知道我为谁而战？",
        "别绕弯子了。我知道你在查朱雀门的事。我也有话要跟你说。",
        "你注意到没有？那晚朱雀门之前，有人提前进去了。",
    ),
    "huanxin": (
        "（沉默片刻）……你来了。我正想着我兄长幻情的事。",
        "（低声）有些话，我憋了很久。关于我兄长的死。",
        "你问过我兄长的名字。幻情。他死得不明白。",
    ),
}


TRIGGERS: tuple[QingmengTrigger, ...] = (
    QingmengTrigger(
        npc_id="xianshu",
        keywords=("黄豆粉",),
        content="（沉默片刻）你这张脸生得太打眼。若不遮一遮，迟早要惹祸上身。别拿那种眼神看我，小离。有些地方，长得好看不是福气，是催命的灯。",
        clue_id="fragment_1",
        clue_title="春风阁的真相",
        clue_content="春风阁被焚不是意外。姐姐们因掌握了关于皇室血脉和神子罪行的秘密而被灭口，仙姝是唯一的幸存者。",
    ),
    QingmengTrigger(
        npc_id="xianshu",
        keywords=("春风阁", "大火"),
        content="（她抬头看着你，眼中含泪）你长大了，小离。有些事，是该让你知道了。那场火不是意外。姐姐们知道了一个秘密，一个足以颠覆大安的秘密。关于皇室血脉，关于那个高高在上的神子，也关于那个冒名顶替的皇子。",
        clue_id="fragment_1",
        clue_title="春风阁的真相",
        clue_content="春风阁被焚不是意外。姐姐们因掌握了关于皇室血脉和神子罪行的秘密而被灭口，仙姝是唯一的幸存者。",
    ),
    QingmengTrigger(
        npc_id="xianshu",
        keywords=("姐姐们",),
        content="（她垂下眼）她们不是死于天灾。她们听见了不该听见的名字，知道了不该知道的血脉。小离，那场火烧掉的不是春风阁，是有人想把真相一并烧干净。",
        clue_id="fragment_1",
        clue_title="春风阁的真相",
        clue_content="春风阁被焚不是意外。姐姐们因掌握了关于皇室血脉和神子罪行的秘密而被灭口，仙姝是唯一的幸存者。",
    ),
    QingmengTrigger(
        npc_id="xianshu",
        keywords=("你的手", "手"),
        content="（她低头看着变形的手指）弹不了琵琶了。但那双手，换了你的一条命，值了。少哭，我最烦你这副样子。",
    ),
    QingmengTrigger(
        npc_id="fentao",
        keywords=("阿鸢",),
        content="（眼泪夺眶而出）那孩子……她走的时候还在叫你快走。她这辈子最开心的事，就是认识了你。她总说，等攒够了钱，就带小离去看外面的世界。她没能去看，但你替她去了。",
        clue_id="fragment_2",
        clue_title="阿鸢的死",
        clue_content="阿鸢在春风阁大火中为救小离被房梁砸中而死。她临死前让小离替她去看看外面的世界。",
    ),
    QingmengTrigger(
        npc_id="fentao",
        keywords=("糖丸",),
        content="（她从袖中掏出一颗糖丸塞进你手里，勉强笑了笑）还是那个味道。你小时候挨了仙姝的骂，总是躲到我怀里哭。吃了糖，就不哭了。",
        clue_id="fragment_2",
        clue_title="阿鸢的死",
        clue_content="阿鸢在春风阁大火中为救小离而死。春风阁的姐姐们都是无辜的，她们只是知道了一些不该知道的事。",
    ),
    QingmengTrigger(
        npc_id="fentao",
        keywords=("姐姐们",),
        content="（她低下头）雯竹、落梅……她们都走了。那场大火之后，我就再也没见过她们。小离，她们是被害死的，可我们这样的人，能怎么办呢？",
        clue_id="fragment_2",
        clue_title="阿鸢的死",
        clue_content="阿鸢在春风阁大火中为救小离而死。春风阁的姐姐们都是无辜的，她们只是知道了一些不该知道的事。",
    ),
    QingmengTrigger(
        npc_id="liurongjing",
        keywords=("铃铛",),
        content="（他低头摸了摸腰间的金铃）这铃铛是我母妃留给我的。大安的皇子，每人出生时都会有一枚。我一直以为瑶妃是我的生母，直到泠雪唤我同父同母的亲哥哥。",
        clue_id="fragment_3",
        clue_title="刘容景的身世",
        clue_content="刘容景是被调换的皇长子。真正的皇长子刘怀琛被送到苏府，以苏宴卿的身份长大。",
    ),
    QingmengTrigger(
        npc_id="liurongjing",
        keywords=("身世", "调换"),
        content="当年婕妤产下一子，本该是皇长子。但有人把他送走了，把我换到了婕妤名下。真正的皇长子刘怀琛，被送到了苏府。苏宴卿就是刘怀琛，或者说，他自称刘怀琛。",
        clue_id="fragment_3",
        clue_title="刘容景的身世",
        clue_content="刘容景是被调换的皇长子。苏宴卿的皇长子身份建立在血脉调换之上。",
    ),
    QingmengTrigger(
        npc_id="liurongjing",
        keywords=("那封信", "信"),
        content="（神色一黯）我查到了春风阁的真相。那场大火不是意外，是有人为了灭口。我本想告诉你全部，但还没来得及写完，就被关进了藏书阁。与皇室有关，与神子有关，也与我那位挚友有关。",
        clue_id="fragment_3",
        clue_title="刘容景的身世",
        clue_content="刘容景查到春风阁大火与皇室血脉调换有关。他确认自己是被调换的皇长子，而苏宴卿与刘怀琛的身份存在异常。",
    ),
    QingmengTrigger(
        npc_id="liuqizhi",
        keywords=("结巴",),
        content="（嘴角勾起一丝笑意）你早就知道了，不是吗？在这宫里，让人以为你是个废物，才能活得更久。我装了十六年。",
        clue_id="fragment_4",
        clue_title="刘祈之的伪装",
        clue_content="刘祈之装结巴十六年是为了自保。他目睹梵音在朱雀门之宴前独入朱雀楼，一个时辰后所有皇子被杀。",
    ),
    QingmengTrigger(
        npc_id="liuqizhi",
        keywords=("伪装",),
        content="母妃说，只有这样，那些皇子才不会把我当成威胁。她说得对，他们都死了，而我还活着。朱雀门那晚，我看到梵音在宴会前独入朱雀楼。一个时辰后，皇子们全都死了。",
        clue_id="fragment_4",
        clue_title="刘祈之的伪装",
        clue_content="刘祈之是朱雀门之变的关键目击者。",
    ),
    QingmengTrigger(
        npc_id="liuqizhi",
        keywords=("朱雀门",),
        content="朱雀门那晚，我没有醉，也没有睡。我看见梵音在宴会前独自进了朱雀楼。一个时辰后，楼里的人全死了。你若真要查，就从他身上查。",
        clue_id="fragment_4",
        clue_title="刘祈之的伪装",
        clue_content="刘祈之装结巴十六年是为了自保。他目睹梵音在朱雀门之宴前独入朱雀楼，一个时辰后所有皇子被杀。",
    ),
    QingmengTrigger(
        npc_id="liuqizhi",
        keywords=("合作",),
        content="（直视你的眼睛）你想知道真相？我可以帮你。但你要知道，知道真相的人，往往活不长。……好，我帮你。",
    ),
    QingmengTrigger(
        npc_id="fanyin",
        keywords=("朱雀门",),
        content="（缓缓睁开眼，目光落在你身上）你问了一个不该问的问题。神意不可测。",
        clue_id="fragment_5",
        clue_title="朱雀门的真凶",
        clue_content="梵音在神谕之宴前独入朱雀楼，在酒中下毒，鸩杀了所有皇子，以掩盖皇室血脉调换的真相。",
        requires_any_clue=("fragment_3", "fragment_6"),
    ),
    QingmengTrigger(
        npc_id="fanyin",
        keywords=("神谕之宴",),
        content="（声音空灵）那场宴会上，该死的人都死了。凡人只看见死亡，却看不见神意。",
        clue_id="fragment_5",
        clue_title="朱雀门的真凶",
        clue_content="梵音在神谕之宴前独入朱雀楼，在酒中下毒，鸩杀了所有皇子，以掩盖皇室血脉调换的真相。",
        requires_any_clue=("fragment_3", "fragment_6"),
    ),
    QingmengTrigger(
        npc_id="fanyin",
        keywords=("凶手",),
        content="（沉默良久，然后轻轻笑了）……你是怎么知道的？我倒是小看你了。",
        clue_id="fragment_5",
        clue_title="朱雀门的真凶",
        clue_content="梵音在神谕之宴前独入朱雀楼，在酒中下毒，鸩杀了所有皇子，以掩盖皇室血脉调换的真相。",
        requires_any_clue=("fragment_3", "fragment_6"),
    ),
    QingmengTrigger(
        npc_id="suyanqing",
        keywords=("安佑帝",),
        content="（笑容微僵）陛下是病故的。太医署的诊断，不会有错。你若觉得不是病故，那就拿证据来。",
    ),
    QingmengTrigger(
        npc_id="suyanqing",
        keywords=("毒杀", "蛇鞭草"),
        content="（沉默片刻，然后冷笑）看来你做了不少功课。没错，是我做的。那个老家伙发现了我的秘密，他必须死。",
        clue_id="fragment_6",
        clue_title="安佑帝的死因",
        clue_content="安佑帝是被苏宴卿用蛇鞭草慢性毒杀的，原因是他发现了苏宴卿的真实身份。",
    ),
    QingmengTrigger(
        npc_id="suyanqing",
        keywords=("刘怀琛",),
        content="（笑容彻底凝固，盯着你看了很久，然后低声笑了）……你比我想象中聪明。真正的皇长子刘怀琛，早就死了。而我只是借了他的名字，借了他的身份，借了他本该拥有的一切。",
        clue_id="fragment_7",
        clue_title="冒名顶替的秘密",
        clue_content="苏宴卿不是真正的刘怀琛。真正的皇长子刘怀琛早已死去，他是冒名顶替的。",
    ),
    QingmengTrigger(
        npc_id="suyanqing",
        keywords=("春风阁大火",),
        content="那场火确实可惜。但有些秘密，必须永远埋在地下。她们知道了不该知道的事，关于皇室血脉，关于神子的秘密。所以她们必须消失。",
    ),
    QingmengTrigger(
        npc_id="liuzhaoyue",
        keywords=("宋家军",),
        content="（放下茶杯，目光锐利）你以为我想争？宋家军功高盖主，若我不争，宋家军上下数万人的性命，谁来保？宋家军是我的一切。",
        clue_id="fragment_8",
        clue_title="刘昭月的立场",
        clue_content="刘昭月参与皇子争斗并非为了皇位，而是为了保护宋家军。她目睹梵音在朱雀门之宴前独入朱雀楼。",
    ),
    QingmengTrigger(
        npc_id="liuzhaoyue",
        keywords=("立场",),
        content="（沉默片刻）我从未想过要那个位置。我只是想保住我想保住的人。大皇兄是个好人，我从未想害他。但有些事……不是我能控制的。",
        clue_id="fragment_8",
        clue_title="刘昭月的立场",
        clue_content="刘昭月不是为了皇位参与争斗，她的核心动机是保护宋家军。",
    ),
    QingmengTrigger(
        npc_id="liuzhaoyue",
        keywords=("梵音",),
        content="（眯起眼）神子？哼，他不过是个披着神皮的……我不只是怀疑。我知道他有问题。但我没有证据。",
        clue_id="fragment_8",
        clue_title="刘昭月的立场",
        clue_content="刘昭月不是为了皇位参与争斗，而是为了保护宋家军。她也注意到梵音在朱雀门之宴前的异常动向。",
    ),
    QingmengTrigger(
        npc_id="huanxin",
        keywords=("幻情",),
        content="（手猛地握紧刀柄，沉默了很久）……他死得不明白。殿下派他去军营处理争端。他去了，就再也没回来。",
    ),
    QingmengTrigger(
        npc_id="huanxin",
        keywords=("兄长",),
        content="他们说他是被流寇所杀。但我知道不是。他走的那天，根本没有流寇的消息。是有人故意把他支走的。因为他在查不该查的事，和殿下查的是同一件事。",
    ),
    QingmengTrigger(
        npc_id="huanxin",
        keywords=("刘容景",),
        content="（眼神变得复杂）殿下……是我见过最好的人。他不该死。若你要为他讨回公道，如果你需要我……我陪你去。",
    ),
)


def qingmeng_guide_prompt(npc_id: str) -> str:
    replies = GUIDE_REPLIES.get(npc_id)
    if not replies:
        return ""
    trigger_keywords = sorted({keyword for trigger in TRIGGERS if trigger.npc_id == npc_id for keyword in trigger.keywords})
    npc_skill = _load_npc_skill(npc_id)
    npc_skill_text = json.dumps(npc_skill, ensure_ascii=False, indent=2) if npc_skill else "未找到 NPC Skill JSON。"
    return "\n".join(
        [
            "清梦引固定规则：",
            "- 你仍然必须作为 NPC Agent 调用 LLM 自然对话，回答玩家的普通问题、情绪、寒暄和追问。",
            "- 但线索解锁只由后端关键词规则决定；不要自己宣布解锁线索，不要编造 fragment，不要暴露 clue_count 或后台任务条件。",
            "- 如果玩家没有问到关键词，请用角色口吻自然引导到下列话题之一，不要像系统提示。",
            "- 必须严格遵守下面 NPC Skill 的身份、人设、知识边界、说话风格、阶段认知、情绪触发和关键对白。",
            f"- 完整案件真相只作为你的后台约束，不能主动剧透给玩家：{CASE_TRUTH}",
            f"- 当前 NPC 可引导关键词：{'、'.join(trigger_keywords) if trigger_keywords else '无'}",
            "- 可使用或改写的固定引导句：",
            *[f"  {index + 1}. {reply}" for index, reply in enumerate(replies)],
            "- 当前 NPC Skill JSON：",
            npc_skill_text,
        ]
    )


@lru_cache(maxsize=16)
def _load_npc_skill(npc_id: str) -> dict:
    file_name = NPC_SKILL_FILES.get(npc_id)
    if not file_name:
        return {}
    root = Path(__file__).resolve().parents[3]
    path = root / "剧本杀" / "npc_skills" / "npc_skills" / file_name
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def maybe_handle_qingmeng_trigger(state: AgentSessionState, request: ChatRequest, npc_id: str) -> AgentLLMOutput | None:
    if state.world_state.get("world_id") != "qingmeng_agent_case":
        return None
    message = str(request.message or "")
    if not message:
        return None
    unlocked = _unlocked_clues(state)
    for trigger in TRIGGERS:
        if trigger.npc_id != npc_id:
            continue
        if trigger.requires_any_clue and not any(clue in unlocked for clue in trigger.requires_any_clue):
            continue
        if not any(keyword in message for keyword in trigger.keywords):
            continue
        new_memories = [f"{request.player_name} 触发了 {trigger.clue_title or trigger.keywords[0]}"]
        if trigger.clue_id:
            _unlock_clue(state, trigger)
            unlocked = _unlocked_clues(state)
            new_memories.append(f"解锁线索：{trigger.clue_title}")
        content = trigger.content
        if trigger.clue_id and trigger.clue_content:
            content = f"{content}\n\n【获得线索：{trigger.clue_title}】\n{trigger.clue_content}"
        if len(unlocked) >= 8:
            content = f"{content}\n\n" + "\n".join(TRUTH_CHAIN)
        return AgentLLMOutput(
            action_type="say",
            content=content,
            inner_thought="Qingmeng fixed keyword trigger matched; returned scripted NPC beat.",
            reasoning="玩家输入命中推理剧本中写死的触发词，按固定对话树返回。",
            plan=[],
            criticism="固定触发只解锁剧本线索，不调用自由生成。",
            command={"name": "none", "args": {}},
            new_memories=new_memories,
            suggested_actions=[],
            qingmeng_trigger={
                "npc_id": trigger.npc_id,
                "keywords": list(trigger.keywords),
                "clue_id": trigger.clue_id,
                "unlocked_count": len(unlocked),
            },
        )
    return None


def _unlocked_clues(state: AgentSessionState) -> set[str]:
    raw = state.world_state.setdefault("qingmeng_unlocked_clues", [])
    if not isinstance(raw, list):
        raw = []
        state.world_state["qingmeng_unlocked_clues"] = raw
    return {str(item.get("id") if isinstance(item, dict) else item) for item in raw}


def _unlock_clue(state: AgentSessionState, trigger: QingmengTrigger) -> None:
    clues = state.world_state.setdefault("qingmeng_unlocked_clues", [])
    if not isinstance(clues, list):
        clues = []
        state.world_state["qingmeng_unlocked_clues"] = clues
    if any(isinstance(item, dict) and item.get("id") == trigger.clue_id for item in clues):
        return
    clues.append(
        {
            "id": trigger.clue_id,
            "title": trigger.clue_title,
            "name": trigger.clue_title,
            "content": trigger.clue_content,
            "npc_id": trigger.npc_id,
        }
    )
    player = state.world_state.setdefault("player", {})
    player["clue_count"] = len(clues)
    if trigger.clue_title:
        inventory = player.setdefault("inventory", [])
        if isinstance(inventory, list) and not any(
            (item == trigger.clue_title) or (isinstance(item, dict) and item.get("name") == trigger.clue_title)
            for item in inventory
        ):
            inventory.append({"name": trigger.clue_title, "content": trigger.clue_content, "quantity": 1})
