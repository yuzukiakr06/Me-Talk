"""Me Talk ユーザーID生成。

Discord の snowflake と同じ考え方で、
「経過ミリ秒 + 乱数」から18〜19桁の数字列を組み立てる。
時刻を含むので生成順に並び、同一ミリ秒でも乱数22ビットで衝突しない。

Dev yuzuki_akrdev.ofc
"""

import secrets
import time

EPOCH_MS = 1735689600000

RANDOM_BITS = 22


def generate() -> str:
    """新しいユーザーIDを1つ組み立てる。"""
    elapsed = int(time.time() * 1000) - EPOCH_MS
    if elapsed < 0:
        elapsed = 0
    return str((elapsed << RANDOM_BITS) | secrets.randbits(RANDOM_BITS))


def generate_unique(is_taken) -> str:
    """既存と衝突しないユーザーIDを返す。

    is_taken(value) が True を返す間は作り直す。
    """
    for _ in range(8):
        value = generate()
        if not is_taken(value):
            return value
    while True:
        value = str(secrets.randbits(62))
        if not is_taken(value):
            return value
