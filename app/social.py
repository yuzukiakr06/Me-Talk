"""Me Talk 人間関係の共通判定。

ブロックと鍵アカウントの判定を1か所に集める。
オープンチャット・DM・グローバルチャットのどこから呼んでも
同じ結果になるようにするための土台。

これを各ルーターがばらばらに実装すると、
「グローバルでは見えないのにDMでは届く」といった穴ができる。

Dev yuzuki_akrdev.ofc
"""

import re

from sqlmodel import Session, select

from app.models import Block, Follow, User

TAG_PATTERN = re.compile(r"#([0-9A-Za-z_\u3040-\u30ff\u3400-\u9fff\uff66-\uff9f]{1,30})")
MAX_TAGS_PER_POST = 8


def blocked_ids(session: Session, user_id: int | None) -> set:
    """自分がブロックした相手と、自分をブロックした相手をまとめて返す。

    どちらの向きでも関係を断つ。片方向だけ隠すと、
    ブロックされた側から一方的に絡めてしまう。
    """
    if not user_id:
        return set()
    out = set()
    for row in session.exec(select(Block).where(Block.user_id == user_id)).all():
        out.add(row.blocked_user_id)
    for row in session.exec(select(Block).where(Block.blocked_user_id == user_id)).all():
        out.add(row.user_id)
    return out


def is_blocked(session: Session, a: int | None, b: int | None) -> bool:
    """2人の間にどちらの向きでもブロックがあるか。"""
    if not a or not b or a == b:
        return False
    row = session.exec(
        select(Block).where(Block.user_id == a, Block.blocked_user_id == b)
    ).first()
    if row:
        return True
    return bool(session.exec(
        select(Block).where(Block.user_id == b, Block.blocked_user_id == a)
    ).first())


def is_following(session: Session, follower_id: int, followee_id: int) -> bool:
    return bool(session.exec(
        select(Follow).where(Follow.follower_id == follower_id, Follow.followee_id == followee_id)
    ).first())


def is_mutual(session: Session, a: int | None, b: int | None) -> bool:
    """相互フォローかどうか。鍵アカウントの解錠条件。"""
    if not a or not b:
        return False
    if a == b:
        return True
    return is_following(session, a, b) and is_following(session, b, a)


def can_view_posts(session: Session, viewer: User | None, target: User) -> bool:
    """相手の投稿を見てよいか。

    鍵アカウントは相互フォローの相手にだけ中身を見せる。
    こちらから一方的にフォローしただけでは見えない。
    """
    if not target or not target.is_active:
        return False
    viewer_id = viewer.id if viewer else None
    if is_blocked(session, viewer_id, target.id):
        return False
    if not target.is_private:
        return True
    if viewer_id is None:
        return False
    return is_mutual(session, viewer_id, target.id)


def can_direct_message(session: Session, sender: User, target: User) -> tuple:
    """DMを送ってよいか。送れない場合は理由も返す。"""
    if not target or not target.is_active:
        return False, "相手が見つかりません。"
    if is_blocked(session, sender.id, target.id):
        return False, "この相手にメッセージを送れません。"
    if target.is_private and not is_mutual(session, sender.id, target.id):
        return False, "このアカウントは非公開です。相互にフォローするとメッセージを送れます。"
    return True, ""


def can_call(session: Session, caller: User, target: User) -> tuple:
    """通話してよいか。

    フレンドであること、鍵アカウントなら相互フォローであることの両方を満たす必要がある。
    """
    from app.routes_friends import friendship_state

    if is_blocked(session, caller.id, target.id):
        return False, "この相手とは通話できません。"
    if friendship_state(session, caller.id, target.id) != "friends":
        return False, "通話はフレンドのみ利用できます。"
    if target.is_private and not is_mutual(session, caller.id, target.id):
        return False, "このアカウントは非公開です。相互にフォローすると通話できます。"
    return True, ""


def extract_tags(text: str) -> list:
    """本文から # で始まるタグを取り出す。小文字にそろえて重複を除く。"""
    found = []
    for raw in TAG_PATTERN.findall(text or ""):
        tag = raw.lower()
        if tag not in found:
            found.append(tag)
        if len(found) >= MAX_TAGS_PER_POST:
            break
    return found
