"""Me Talk データベースマイグレーション。

SQLModel のモデル定義と実際のテーブルを突き合わせ、
不足しているカラムと索引だけを後から追加する。

create_all() は既存テーブルにカラムを足さないため、
これが無いとモデルにフィールドを増やした時点で
既存のデータベースが no such column で動かなくなる。

Dev yuzuki_akrdev.ofc
"""

from sqlalchemy import inspect, text
from sqlmodel import SQLModel, select

from app import config, crypto, ids
from app.models import DMMessage, OpenChatMessage, OpenChatNote, User


def _default_literal(column) -> str | None:
    """カラムの既定値を SQL リテラルに変換する。変換できない場合は None。"""
    default = getattr(column, "default", None)
    if default is None:
        return None
    if not getattr(default, "is_scalar", False):
        return None
    value = default.arg
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return None


def run(engine) -> list:
    """不足しているカラムと索引を追加し、追加したものの一覧を返す。"""
    from app import models  # noqa: F401  全テーブルを metadata に登録する

    if engine.dialect.name != "sqlite":
        return []

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    plan = []
    for table in SQLModel.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        current = {row["name"] for row in inspector.get_columns(table.name)}
        missing = [c for c in table.columns if c.name not in current]
        if missing or table.indexes:
            plan.append((table, missing))

    applied = []
    with engine.begin() as conn:
        for table, missing in plan:
            for column in missing:
                type_sql = column.type.compile(engine.dialect)
                ddl = 'ALTER TABLE "%s" ADD COLUMN "%s" %s' % (table.name, column.name, type_sql)
                literal = _default_literal(column)
                if literal is not None:
                    ddl += " DEFAULT %s" % literal
                conn.execute(text(ddl))
                applied.append("%s.%s" % (table.name, column.name))
            for index in table.indexes:
                columns = ", ".join('"%s"' % c.name for c in index.columns)
                unique = "UNIQUE " if index.unique else ""
                conn.execute(text(
                    'CREATE %sINDEX IF NOT EXISTS "%s" ON "%s" (%s)'
                    % (unique, index.name, table.name, columns)
                ))
    return applied


def apply_username_renames(session) -> list:
    """設定 USERNAME_RENAMES に従ってユーザー名を付け替える。

    「旧名:新名」の組を順に見て、旧名のユーザーが居て
    新名が誰にも使われていない場合だけ変更する。
    """
    changes = []
    for old, new in config.USERNAME_RENAMES:
        users = session.exec(select(User)).all()
        target = next((u for u in users if (u.username or "").lower() == old.lower()), None)
        if not target:
            continue
        taken = any(
            (u.username or "").lower() == new.lower() and u.id != target.id
            for u in users
        )
        if taken:
            continue
        target.username = new
        session.add(target)
        changes.append("%s -> %s" % (old, new))
    if changes:
        session.commit()
    return changes


def backfill_user_ids(session) -> int:
    """ユーザーIDが未設定のユーザーに採番する。付与した人数を返す。"""
    users = session.exec(select(User)).all()
    used = {u.user_id for u in users if u.user_id}
    added = 0
    for user in users:
        if user.user_id:
            continue
        value = ids.generate_unique(lambda v: v in used)
        used.add(value)
        user.user_id = value
        session.add(user)
        added += 1
    if added:
        session.commit()
    return added


ENCRYPTED_COLUMNS = [
    (OpenChatMessage, "content"),
    (DMMessage, "content"),
    (OpenChatNote, "body"),
]


def encrypt_existing(engine) -> int:
    """まだ平文で入っている本文を暗号化して書き戻す。件数を返す。

    暗号化済みの行には接頭辞が付いているため、
    途中で中断しても再実行すれば残りだけが処理される。
    """
    if not crypto.enabled():
        return 0
    from sqlalchemy import text

    total = 0
    with engine.begin() as conn:
        for model, column in ENCRYPTED_COLUMNS:
            table = model.__tablename__
            rows = conn.execute(
                text(f'SELECT id, "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL AND "{column}" != \'\'')
            ).fetchall()
            pending = [(r[0], r[1]) for r in rows if not crypto.is_encrypted(r[1])]
            for row_id, value in pending:
                conn.execute(
                    text(f'UPDATE "{table}" SET "{column}" = :v WHERE id = :i'),
                    {"v": crypto.encrypt(value), "i": row_id},
                )
            if pending:
                print(f"[crypto] {table}.{column} を暗号化しました: {len(pending)}件")
            total += len(pending)
    return total


def verify_encryption_key(engine) -> None:
    """暗号化済みのデータを実際に復号できるか確かめる。

    鍵を消したり別の鍵に差し替えたりしたまま起動すると、
    書き込みは通るのに読み出しだけが壊れる状態になる。
    それを起動時に検出して止める。
    """
    from sqlalchemy import text

    sample = None
    with engine.connect() as conn:
        for model, column in ENCRYPTED_COLUMNS:
            table = model.__tablename__
            row = conn.execute(
                text(f'SELECT "{column}" FROM "{table}" WHERE "{column}" LIKE \'{crypto.PREFIX}%\' LIMIT 1')
            ).fetchone()
            if row:
                sample = (table, column, row[0])
                break

    if sample is None:
        return
    table, column, value = sample
    if not crypto.enabled():
        raise RuntimeError(
            f"{table}.{column} に暗号化されたデータがありますが ENCRYPTION_KEY が設定されていません。"
            " 鍵を設定しないとメッセージを読み出せません。"
        )
    try:
        crypto.decrypt(value)
    except Exception as exc:
        raise RuntimeError(
            f"ENCRYPTION_KEY で既存のデータを復号できませんでした ({exc})。"
            " 鍵が以前のものと違う可能性があります。元の鍵に戻してください。"
        )
