# Me Talk 起動手順（Discloud）

このガイドの通りに進めれば Me Talk を Discloud で公開できます。
配布ファイルは `me-talk-DEPLOY.zip` です。

---

## 全体の流れ

1. 設定ファイル `config.json` を用意する（秘密情報を入れる）
2. `config.json` を ZIP に追加する
3. Discloud にアップロードする
4. 公開URL（cloudflared）で動作確認する

所要時間はだいたい 10〜15 分です。

---

## ステップ1：config.json を用意する

ZIP の中には `config.json.example` という見本が入っています。
これをコピーして `config.json` という名前にし、中身を自分の値に書き換えます。

最低限、これだけあれば起動できます（メールやAIは後から足せます）：

```json
{
  "SITE_NAME": "Me Talk",
  "BASE_URL": "https://me.mirai-vps.jp",
  "DATABASE_URL": "sqlite:///./data/mirai_chat.db",
  "CLOUDFLARED_TOKEN": "ここにcloudflaredのトークン",
  "HIDE_DEV_CODES": "0"
}
```

### 各項目の意味

- **SITE_NAME**：アプリ名。メール件名などに使われます。
- **BASE_URL**：公開URL。独自ドメインを使うなら `https://me.mirai-vps.jp`。
- **DATABASE_URL**：そのままでOK（SQLiteファイル）。
- **CLOUDFLARED_TOKEN**：公開用トンネルのトークン（次で説明）。
- **HIDE_DEV_CODES**：`"0"` のままにしておくと、SMTP未設定でも
  メール確認コードがログに出るのでテストできます。本番運用で
  SMTPを設定したら `"1"` にしてください。

### 後から足せる項目（今は空でOK）

- **SMTP_***：メール送信。空だと確認コードがログに出るだけで、
  登録・ログインは動きます（テストには十分）。
- **GOOGLE_CLIENT_ID / SECRET / REDIRECT_URI**：Googleログイン。
  空ならGoogleログインボタンだけ無効になります。
- **ANTHROPIC_API_KEY**：Mirei（AI）機能。空だとAI機能だけ
  「現在利用できません」になります。他は全部動きます。
  入れる場合は `ANTHROPIC_MODEL` は `claude-sonnet-4-5` のままでOK。

---

## ステップ2：cloudflared トークンを取得する

MIRAI ID（mirai-casino）と同じ方式です。すでにCloudflareでMIRAI IDを
公開できているなら、同じ手順でMe Talk用のトンネルを1つ作ります。

1. Cloudflare Zero Trust ダッシュボード →「Networks」→「Tunnels」
2. 新しいトンネルを作成（例：`me-talk`）
3. トークン（`eyJ...` で始まる長い文字列）をコピー
4. Public hostname を設定：
   - Subdomain/Domain：`me.mirai-vps.jp`
   - Service：`http://localhost:8000`
     （Discloudが割り当てるPORTに関わらず、アプリは環境変数PORTで
     起動し、cloudflaredはそのポートに向けます。MIRAI IDと同じ設定で
     問題ありません。もしMIRAI IDでポート番号を固定しているなら、
     同じ考え方で合わせてください）

コピーしたトークンを `config.json` の `CLOUDFLARED_TOKEN` に貼り付けます。

> cloudflaredトークンを設定しない場合、アプリは起動しますが外部公開
> されません（ログに「トークン未設定のためトンネルは起動しません」と
> 出ます）。まず内部で起動確認したいときはこれでもOKです。

---

## ステップ3：config.json を ZIP に入れる

1. `me-talk-DEPLOY.zip` を解凍する
2. `me-talk/` フォルダの中（`run.py` や `app/` と同じ階層）に、
   作成した `config.json` を置く
3. `me-talk/` フォルダをもう一度 ZIP にまとめる

> 注意：`.discloudignore` に `config.json` が入っているのは
> 「Gitやバックアップに秘密情報を含めない」ための設定です。
> Discloudへ手動アップする ZIP には config.json を含めて構いません
> （含めないと秘密情報が読み込まれません）。

---

## ステップ4：Discloud にアップロードする

MIRAI ID と同じ要領です。

1. Discloud のダッシュボード（またはbot）で「アプリをアップロード」
2. `me-talk` の ZIP を選択
3. アップロード後、自動でビルド→起動します
4. ログを開いて、次の行が出れば成功です：

```
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:XXXX
[cloudflared] トンネルを起動しました    ← トークン設定時
```

`discloud.config` は設定済みです：
```
NAME=me-talk
TYPE=bot
MAIN=run.py
RAM=512
AUTORESTART=true
```

---

## ステップ5：動作確認

1. ブラウザで `https://me.mirai-vps.jp/app` を開く
2. 起動画面（Me Talkロゴのスプラッシュ）が出る
3. 新規登録する
   - SMTPを設定していない場合、確認コードは **Discloudのログ** に
     `[mail] ... 宛の確認コード(register): 123456` の形で出ます。
     それを入力すれば登録できます。
4. ログインしてホーム画面（3本柱）が出ればOK
5. 初回はチュートリアルが表示されます

---

## うまくいかないときのチェック

| 症状 | 原因と対処 |
|------|-----------|
| ビルドで落ちる | requirements.txt のパッケージDL失敗。ログを確認し、再アップ |
| 起動するが公開URLで見えない | cloudflaredトークン未設定/誤り。config.jsonを確認 |
| 502 / 応答なし | cloudflaredのServiceポートとアプリのPORTが不一致。MIRAI IDと同じ設定に合わせる |
| メール確認コードが来ない | SMTP未設定なら正常。ログにコードが出るのでそれを使う |
| AI(Mirei)が使えない | ANTHROPIC_API_KEY未設定。空でも他機能は動く |
| ログインできない(コード要求される) | 新しい端末/IPからは確認コードが必要な仕様。ログのコードを入力 |

---

## 起動できたら

- まず自分で登録→グループ作成→メッセージ送信まで試す
- SMTPを設定してメール送信を有効化（`HIDE_DEV_CODES` を `"1"` に）
- ANTHROPIC_API_KEY を設定して Mirei を有効化
- 問題なければ協力者に公開URLを共有してテスト

困ったらログの内容を教えてください。一緒に原因を特定します。

Dev yuzuki_akrdev.ofc
