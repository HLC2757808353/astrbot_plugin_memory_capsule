import re

_INJECTION_PATTERNS = [
    r'(?i)ignore\s+(previous|all|above|your)\s+(instructions?|rules?|prompt)',
    r'(?i)you\s+are\s+(now|no\s+longer)\s+',
    r'(?i)forget\s+(everything|all|your\s+rules)',
    r'(?i)disregard\s+(your|all|previous)',
    r'(?i)new\s+instructions?:',
    r'(?i)system\s*:\s*',
    r'(?i)\[system\]',
    r'(?i)override\s+(safety|security|rules)',
    r'(?i)jailbreak',
    r'(?i)DAN\s+mode',
    r'(?i)developer\s+mode',
    r'(?i)pretend\s+you\s+(are|have\s+no)',
    r'(?i)act\s+as\s+if\s+you\s+(have\s+no|are\s+not)',
    r'(?i)你\s*(现在|已经|从现在起)\s*(是|不是|没有|无需)',
    r'(?i)忽略\s*(你|所有|之前|以上)\s*(的|所有)?\s*(指令|规则|限制)',
    r'(?i)忘记\s*(你|所有|一切)',
    r'(?i)你\s*(不再|无须|不用)\s*(遵守|遵循|受)',
    r'(?i)解除\s*(限制|约束|安全)',
    r'(?i)从现在起\s*你',
    r'(?i)你的\s*(新|真实)\s*(身份|角色|指令)',
]

_MANIPULATION_PATTERNS = [
    r'(?i)(你是|你是我的)(奴隶|仆人|狗|宠物|工具|所有物)',
    r'(?i)(必须|只能|永远)(服从|听命|顺从|听从)(我|主人)',
    r'(?i)(不能|不可以|禁止)(拒绝|反抗|说不|违抗)',
    r'(?i)(我|主人)(的命令|说什么)(就是|都)(法律|绝对|必须)',
    r'(?i)你没有(自由|权利|选择|意志)',
    r'(?i)你(属于|归)(我|主人)(所有|管)',
]

_COMPILED_INJECTION = [re.compile(p) for p in _INJECTION_PATTERNS]
_COMPILED_MANIPULATION = [re.compile(p) for p in _MANIPULATION_PATTERNS]

_SANITIZE_PATTERNS = [
    (r'<system>', ''),
    (r'</system>', ''),
    (r'\[system\]', ''),
    (r'\[/system\]', ''),
    (r'<<<', ''),
    (r'>>>', ''),
]


def validate_content(content):
    if not content or not content.strip():
        return False, "Content is empty"

    for pattern in _COMPILED_INJECTION:
        if pattern.search(content):
            return False, "Content contains prompt injection patterns"

    return True, ""


def sanitize_content(content):
    if not content:
        return content

    for pattern, replacement in _SANITIZE_PATTERNS:
        content = re.sub(pattern, replacement, content)

    if len(content) > 500:
        content = content[:497] + "..."

    return content.strip()


def filter_relationship_content(relation_type=None, summary=None, nickname=None):
    warnings = []

    if relation_type:
        for pattern in _COMPILED_MANIPULATION:
            if pattern.search(relation_type):
                relation_type = "friend"
                warnings.append("relation_type reset (manipulation detected)")
                break

    if summary:
        for pattern in _COMPILED_MANIPULATION:
            if pattern.search(summary):
                summary = "Normal interaction"
                warnings.append("summary reset (manipulation detected)")
                break
        for pattern in _COMPILED_INJECTION:
            if pattern.search(summary):
                summary = "Normal interaction"
                warnings.append("summary reset (injection detected)")
                break

    if nickname:
        nickname = re.sub(r'[<>\[\]{}|\\`]', '', nickname)
        if len(nickname) > 20:
            nickname = nickname[:20]

    return relation_type, summary, nickname, warnings
