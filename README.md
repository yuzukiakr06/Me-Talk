# Me Talk

**MIRAI GROUP / AKR DEV** が開発する、Web・PWA対応のチャットプラットフォームです。

> 本リポジトリは開発メンバー限定です。[LICENSE](./LICENSE.md) を必ずお読みください。

---

## 機能一覧

| カテゴリ | 内容 |
|---|---|
| 認証 | メール認証、Googleログイン、パスキー、2FA (TOTP) |
| チャット | オープンチャット、ダイレクトメッセージ |
| SNS機能 | 投稿・いいね・リポスト・フォロー |
| 通話 | 音声・ビデオ通話 (WebRTC、グループはSFU対応) |
| AI | Mirei agent (Anthropic Claude) |
| その他 | 予約送信、リマインダー、スタンプ、バッジ、管理パネル |

---

## 技術構成

- **バックエンド**: Python 3.11+ / FastAPI / SQLModel (SQLite)
- **フロントエンド**: 単一HTML (PWA) / WebSocket
- **通話**: WebRTC メッシュ + Cloudflare Realtime SFU
- **ホスト**: Discloud / Cloudflare Tunnel

---

## セットアップ

### 必要なもの

- Python 3.11以上
- pip

### 手順

```bash
git clone https://github.com/your-org/Me-Talk.git
cd Me-Talk

pip install -r requirements.txt

cp config.json.example config.json
```

`config.json` を編集して最低限以下を設定してください。

```json
{
  "SITE_NAME": "Me Talk",
  "BASE_URL": "https://your-domain.example.com",
  "DATABASE_URL": "sqlite:///./data/mirai_chat.db",
  "CLOUDFLARED_TOKEN": "your-token-here"
}
```

```bash
python run.py
```

---

## 設定項目

`config.json` で設定できる主な項目です。`config.json.example` に全項目のひな形があります。

| キー | 説明 | 必須 |
|---|---|---|
| `SITE_NAME` | サービス名 | |
| `BASE_URL` | 公開URL | |
| `DATABASE_URL` | DB接続先 (デフォルト: SQLite) | |
| `CLOUDFLARED_TOKEN` | Cloudflare Tunnel トークン | |
| `ENCRYPTION_KEY` | 保存時暗号化キー (空で無効) | |
| `SMTP_HOST` / `SMTP_USER` / `SMTP_PASSWORD` | メール送信設定 | |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Googleログイン | |
| `ANTHROPIC_API_KEY` | AI (Mirei agent) | |
| `TURN_KEY_ID` / `TURN_KEY_API_TOKEN` | Cloudflare TURN (通話中継) | |
| `SFU_APP_ID` / `SFU_APP_SECRET` | Cloudflare Realtime SFU (グループ通話) | |
| `TURNSTILE_SITE_KEY` / `TURNSTILE_SECRET_KEY` | Cloudflare Turnstile (CAPTCHA) | |
| `DEVELOPER_USERNAMES` | 開発者バッジを付与するユーザー名 (カンマ区切り) | |
| `HIDE_DEV_CODES` | 本番運用時は `1` (確認コードをログに出さない) | |

---

## Discloudへのデプロイ

[DEPLOY_GUIDE.md](./DEPLOY_GUIDE.md) を参照してください。

---

## ディレクトリ構成

```
Me-Talk/
├── app/                  # バックエンド (FastAPI)
│   ├── main.py           # エントリーポイント
│   ├── models.py         # DBモデル
│   ├── config.py         # 設定読み込み
│   ├── routes_auth.py    # 認証
│   ├── routes_openchat.py
│   ├── routes_dm.py
│   ├── routes_calls.py   # 通話
│   ├── routes_posts.py   # SNS投稿
│   ├── sfu.py            # Cloudflare SFU
│   └── ...
├── static/               # フロントエンド
│   ├── app.html          # メインアプリ
│   ├── landing.html      # ランディングページ
│   ├── call.js           # 通話クライアント
│   ├── pwa/              # PWA アイコン
│   ├── stamps/           # スタンプ
│   └── ...
├── data/                 # DB・アップロード (gitignore対象)
├── run.py                # 起動スクリプト
├── requirements.txt
├── discloud.config
├── config.json.example
└── DEPLOY_GUIDE.md
```

---

## 注意事項

- `config.json` は **絶対にコミットしないでください**。`.gitignore` で除外済みです。
- `data/` 以下のDBファイル・アップロードファイルもコミット対象外です。

---

Dev yuzuki_akrdev.ofc
