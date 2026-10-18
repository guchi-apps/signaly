"""デプロイ時にテーブルの作成・列の追加を行うマイグレーションランナー（#183）。

アプリ本体（`main.py`）は DDL を一切実行しない。本番の DB ユーザーは用途で分かれて
おり、常時稼働するアプリ用ユーザーは SELECT / INSERT / UPDATE / DELETE しか持たない
（guchi-apps/vps の `docs/web-stack.md`）。DDL 権限を持つのはマイグレーション専用
ユーザーだけで、その資格情報はこのスクリプトの実行中だけ `DB_ADMIN_USER` /
`DB_ADMIN_PASSWORD` として渡される。

実行:

    cd <TARGET_DIR>
    DB_ADMIN_USER=... DB_ADMIN_PASSWORD=... .venv/bin/python backend/migrate_db.py

DB の接続先は `.env.local`（ローカル開発）または `<TARGET_DIR>/.env`（VPS。systemd の
EnvironmentFile と同じファイル）から読む。
このスクリプトは ssh 経由で直接叩かれるため systemd の環境を受け取れないが、
`python-dotenv` を足さずに済むよう最小限のパーサを自前で持つ。
"""

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# DDL が権限で弾かれたときの MySQL エラー。1044 は DB 単位、1142 はテーブル単位の拒否。
_DDL_DENIED_MARKERS = ("1044", "1142", "command denied")


def load_env_file(path: Path) -> None:
    """`KEY=VALUE` 形式のファイルを環境変数へ読み込む。

    既にある環境変数を上書きしない。呼び出し側が `DB_ADMIN_USER` などを environment で
    渡してくるため、そちらを勝たせる。
    """
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = _unquote(value.strip())


def _unquote(value: str) -> str:
    """VPS の app-deploy-gate が書く `KEY="VALUE"` の形を元の値へ戻す（#352）。

    gate は値をダブルクォートで囲み、`\\` と `"` をバックスラッシュでエスケープし、改行は
    `\\n` にして書く。systemd の `EnvironmentFile` は同じ形を読めるが、このパーサが囲みを
    外さないと `DB_NAME` が `"app_signaly"`（引用符込み）になり接続先を取り違える。
    クォートの無い旧形式の行（ローカルの `.env.local` を含む）はそのまま返す。
    """
    if len(value) < 2 or value[0] != '"' or value[-1] != '"':
        return value
    body, out, i = value[1:-1], [], 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            out.append({"n": "\n", "\\": "\\", '"': '"'}.get(nxt, "\\" + nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _grant_hint(db_name: str, using_admin: bool) -> str:
    who = "DB_ADMIN_USER" if using_admin else "アプリ用の DB ユーザー"
    lines = [
        "",
        f"DDL が拒否されました（データベース: {db_name} / 実行ユーザー: {who}）。",
    ]
    if not using_admin:
        lines += [
            "DB_ADMIN_USER / DB_ADMIN_PASSWORD が渡されていません。",
            "アプリ用ユーザーは設計上 CRUD 権限しか持たないため、DDL は実行できません。",
            "デプロイのマイグレーションステップからマイグレーション専用ユーザーを渡してください。",
        ]
    else:
        lines.append("マイグレーション専用ユーザーに、この DB への DDL 権限がありません。")
    lines += [
        "",
        "VPS で MySQL の管理ユーザーとして一度だけ実行してください:",
        "",
        f"  GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, DROP, REFERENCES",
        f"    ON `{db_name}`.* TO '<マイグレーション専用ユーザー>'@'localhost';",
        "  FLUSH PRIVILEGES;",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    # ローカル開発は .env.local、VPS は .env。先に読んだ側が勝つ（load_env_file は
    # 既にある値を上書きしない）ので、両方ある環境では .env.local を優先する。
    load_env_file(_ROOT / ".env.local")
    load_env_file(_ROOT / ".env")

    if not os.environ.get("DB_NAME"):
        print("DB_NAME が設定されていません（.env も環境変数も空）。", file=sys.stderr)
        return 1

    sys.path.insert(0, str(_ROOT / "backend"))
    import database  # noqa: E402  .env を読んだ後でないと import 時点で落ちる

    using_admin = bool(os.environ.get("DB_ADMIN_USER"))
    print(
        "マイグレーションを実行します"
        f"（DB: {os.environ['DB_NAME']} / "
        f"{'マイグレーション専用ユーザー' if using_admin else 'アプリ用ユーザー'}）"
    )
    try:
        database.init_db()
    except Exception as exc:  # noqa: BLE001  失敗の原因を切り分けて出したい
        message = str(exc)
        if any(marker in message for marker in _DDL_DENIED_MARKERS):
            print(message, file=sys.stderr)
            print(_grant_hint(os.environ["DB_NAME"], using_admin), file=sys.stderr)
            return 1
        raise
    print("マイグレーションが完了しました。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
