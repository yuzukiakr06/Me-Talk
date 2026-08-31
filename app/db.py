"""MIRAI Chat データベース接続。

Dev yuzuki_akrdev.ofc
"""

from pathlib import Path

from sqlmodel import SQLModel, Session, create_engine

from app.config import DATABASE_URL

if DATABASE_URL.startswith("sqlite"):
    db_path = DATABASE_URL.replace("sqlite:///", "")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    engine = create_engine(DATABASE_URL)


def init_db() -> None:
    from app import models  # noqa: F401  全テーブルを metadata に登録してから作成する
    from app import migrations
    SQLModel.metadata.create_all(engine)
    added = migrations.run(engine)
    if added:
        print(f"[db] カラムを追加しました: {', '.join(added)}")


def get_session():
    with Session(engine) as session:
        yield session
