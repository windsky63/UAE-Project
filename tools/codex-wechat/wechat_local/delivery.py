import re


def mention_parts(text, mention):
    """Replace the recipient label with one native mention, preserving its position.

    Legacy messages without a recipient header retain their original prefix.
    """
    match = re.search(r'^回复对象：@[^\n]*', text, re.MULTILINE)
    if match:
        return text[:match.start()] + '回复对象：', text[match.end():]
    return '', text


def mentioned_text(text, mention):
    before, after = mention_parts(text, mention)
    return before + '@' + mention + '\u2005' + after


class DeliveryNotSent(RuntimeError):
    """No UI send action was submitted."""
