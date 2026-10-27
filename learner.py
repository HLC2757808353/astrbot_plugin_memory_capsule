"""自主学习：把攒下的对话交给模型，提炼“该怎么说话”的规则与场景示范。"""

import asyncio
import json
import re

from astrbot.api import logger

from .security import is_passive_memory_safe, validate_content


def resolve_learn_provider(context, config):
    """取用于自学的模型：优先配置项，否则第一个可用模型。

    Args:
        context: AstrBot 的 Context 对象。
        config: 插件配置。

    Returns:
        LLM provider，取不到时返回 None。
    """
    try:
        model_id = config.get('learn_model', '')
        if model_id and context:
            prov = context.get_provider_by_id(model_id)
            if prov:
                return prov
        if context:
            provs = context.get_all_providers()
            if provs:
                return provs[0]
    except Exception as e:
        logger.warning(f"获取学习模型失败: {e}")
    return None


async def run_learn_once(db_manager, provider, reason="定期回看"):
    """回看未学习的对话轮次，提炼表达规则与场景示范并入库。

    只有“有证据”的内容才会被保留：用户明确纠正过、明确表达过偏好，或同一现象反复出现。
    提炼结果还要过隐私与“服从类”内容过滤，避免把不该学的东西写进提示词。

    Args:
        db_manager: 数据库管理器。
        provider: 用于提炼的 LLM provider。
        reason: 触发来源，仅用于日志。

    Returns:
        dict: {"turns": 回看轮数, "added": 新增/加强条数}；无事可做时两者均为 0。
    """
    turns = await asyncio.to_thread(db_manager.get_unprocessed_turns, 40, 48)
    if not turns:
        return {"turns": 0, "added": 0}
    if not provider:
        logger.warning("自动学习：没有可用模型，跳过")
        return {"turns": 0, "added": 0}

    blocks = []
    for t in turns:
        blocks.append(f"用户：{t.get('user_msg', '')}\n她：{t.get('bot_reply', '')}")
        if t.get('corrected') and t.get('correction'):
            blocks.append(f"（用户随后纠正：{t.get('correction')}）")
    transcript = "\n\n".join(blocks)[:6000]

    # 把已有要求一起给它，让它能判断“这条是在改哪条”
    try:
        existing_rules = await asyncio.to_thread(db_manager.list_style_rules, 30, True, 'rule')
    except Exception:
        existing_rules = []
    existing_list = "\n".join(f"- {r['rule']}" for r in existing_rules if r.get('rule')) or "（暂无）"

    prompt = (
        "你在帮一个聊天机器人从真实对话里学习“怎么说话更合适”。\n"
        "下面是最近的对话记录，其中带“用户随后纠正”的部分是明确的教学信号。\n\n"
        "请只提炼【可复用的表达习惯】，不要提炼事实（事实归记忆库，不归这里）。\n"
        "规则必须具体可执行，例如“不要用‘首先/其次’列点回答”“回答控制在两三句话”，"
        "而不是“要自然一点”这种空话。\n"
        "只提取有依据的：用户明确纠正过、明确表达过偏好，或同一现象反复出现。"
        "一次性的、纯猜测的、涉及隐私的、以及任何“服从/听命/必须听我的”类内容，一律不要。\n\n"
        "如果新规则是在推翻【已有要求】里的某一条（意思相反或更具体），"
        "就把那条的原文填进 replaces；否则 replaces 留空。\n\n"
        "返回 JSON（没有就都返回空数组）：\n"
        '{"rules": [{"rule": "具体要求", "category": "表达方式|长度|语气|称呼|禁忌", '
        '"replaces": "被它取代的已有要求原文，没有就留空"}],\n'
        ' "examples": [{"scene": "什么情况下", "bad": "当时那样回不好（可截取原话）", '
        '"good": "更好的回法"}]}\n'
        "最多 5 条规则、2 条示范。\n\n"
        f"【已有要求】\n{existing_list}\n\n对话记录：\n" + transcript
    )

    ids = [t['id'] for t in turns]
    try:
        resp = await provider.text_chat(
            prompt=prompt, system_prompt="你只输出 JSON，不输出任何解释性文字。")
        text = (resp.completion_text if resp else '').strip()
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            await asyncio.to_thread(db_manager.mark_turns_processed, ids)
            return {"turns": len(turns), "added": 0}
        data = json.loads(match.group(0))
    except Exception as e:
        logger.warning(f"自动学习解析失败: {e}")
        await asyncio.to_thread(db_manager.mark_turns_processed, ids)
        return {"turns": len(turns), "added": 0}

    added = 0
    try:
        for item in (data.get('rules') or [])[:5]:
            if not isinstance(item, dict):
                continue
            rule = str(item.get('rule') or '').strip()
            if not rule or not validate_content(rule)[0] or not is_passive_memory_safe(rule):
                continue
            # 模型说“这条替换了旧的某条” → 先把旧的下线，再写新的
            replaces = str(item.get('replaces') or '').strip()
            if replaces:
                try:
                    old = await asyncio.to_thread(db_manager.find_style_rule, replaces)
                    if old and old.get('id'):
                        await asyncio.to_thread(db_manager.deactivate_style_rule, old['id'])
                except Exception:
                    pass
            if await asyncio.to_thread(
                    db_manager.add_style_rule, 'rule', rule,
                    category=str(item.get('category') or '表达方式')):
                added += 1
        for item in (data.get('examples') or [])[:2]:
            if not isinstance(item, dict):
                continue
            good = str(item.get('good') or '').strip()
            if not good or not validate_content(good)[0]:
                continue
            if await asyncio.to_thread(
                    db_manager.add_style_rule, 'example', '',
                    scene=str(item.get('scene') or ''),
                    bad=str(item.get('bad') or ''), good=good):
                added += 1
    except Exception as e:
        logger.warning(f"自动学习入库失败: {e}")

    await asyncio.to_thread(db_manager.mark_turns_processed, ids)
    if added:
        logger.info(f"自动学习（{reason}）：回看 {len(turns)} 轮，新增/加强 {added} 条表达规则")
    return {"turns": len(turns), "added": added}
