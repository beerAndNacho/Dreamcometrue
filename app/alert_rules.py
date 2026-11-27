"""Validated, bounded price threshold rules. No arbitrary recipients or URLs."""
import re
from decimal import Decimal, InvalidOperation


def parse_rules(text):
    if not isinstance(text, str) or len(text) > 4000:
        raise ValueError('가격 조건 입력 범위 오류')
    rules = []
    seen = set()
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = [x.strip() for x in line.split(',')]
        if len(fields) != 3:
            raise ValueError('종목코드,above 또는 below,가격 형식')
        code, side, raw = fields
        if not re.fullmatch(r'[0-9]{6}', code) or side not in ('above', 'below') or not re.fullmatch(r'[0-9]{1,9}(?:\.[0-9]{1,2})?', raw):
            raise ValueError('가격 조건 형식 오류')
        try:
            value = Decimal(raw)
        except InvalidOperation:
            raise ValueError('가격 조건 형식 오류') from None
        if not 0 < value <= 100_000_000:
            raise ValueError('가격 범위 오류')
        price = format(value.normalize(), 'f')
        key = (code, side, price)
        if key in seen or len(rules) >= 20:
            raise ValueError('중복 또는 최대 20개 가격 조건 초과')
        seen.add(key)
        rules.append(key)
    return rules


def normalize_rules(text):
    return '\n'.join(','.join(x) for x in parse_rules(text))
