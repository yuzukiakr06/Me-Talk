"""MIRAI Chat メール送信。

MIRAI ID と同じ SMTP 設定形式で確認コードメールを送る。
SMTP未設定の場合はコンソール出力のみ行う(開発用フォールバック)。

Dev yuzuki_akrdev.ofc
"""

import smtplib
from email.message import EmailMessage

from app import config


CODE_TTL_MINUTES = 15

_LABELS = {
    "login": ("ログイン", "ログインを完了するには、下のコードを入力してください。"),
    "new_ip": ("新しい端末からのログイン", "いつもと違う端末からのログインです。ご本人であれば、下のコードを入力してください。"),
    "register": ("会員登録", "Me Talk へようこそ。登録を完了するには、下のコードを入力してください。"),
    "password_change": ("パスワードの変更", "パスワードを変更するには、下のコードを入力してください。心当たりがない場合は、このメールを無視してください。"),
    "password_set": ("パスワードの設定", "パスワードを設定するには、下のコードを入力してください。心当たりがない場合は、このメールを無視してください。"),
    "username_change": ("ユーザー名の変更", "ユーザー名を変更するには、下のコードを入力してください。"),
}


def _labels(purpose: str) -> tuple:
    return _LABELS.get(purpose, ("認証", "下のコードを入力してください。"))


def _subject(purpose: str) -> str:
    if purpose == "login":
        return f"{config.SITE_NAME} ログインの確認コード"
    if purpose == "new_ip":
        return f"{config.SITE_NAME} 新しい端末からのログイン確認"
    if purpose == "password_change":
        return f"{config.SITE_NAME} パスワード変更の確認コード"
    if purpose == "password_set":
        return f"{config.SITE_NAME} パスワード設定の確認コード"
    if purpose == "username_change":
        return f"{config.SITE_NAME} ユーザー名変更の確認コード"
    return f"{config.SITE_NAME} へようこそ - 登録の確認コード"


def _body_text(code: str, purpose: str) -> str:
    label, lead = _labels(purpose)
    base = config.BASE_URL.rstrip("/")
    return (
        f"{config.SITE_NAME}\n"
        "つながる、話せる、Me Talk。\n"
        "--------------------------------------------\n\n"
        f"{lead}\n\n"
        f"    確認コード: {code}\n\n"
        f"このコードの有効期限は {CODE_TTL_MINUTES} 分です。\n"
        "コードは他の人に教えないでください。\n\n"
        f"心当たりがない場合は、このメールを削除していただいて問題ありません。\n"
        f"({label}の手続きは完了しません)\n\n"
        "--------------------------------------------\n"
        f"Me Talk を開く: {base}/app\n"
        f"利用規約: {base}/legal/terms\n"
        f"プライバシーポリシー: {base}/legal/privacy\n\n"
        "MIRAI GROUP / AKR DEV corporation\n"
        "このメールは送信専用です。返信はできません。\n"
    )


def _body_html(code: str, purpose: str) -> str:
    label, lead = _labels(purpose)
    site = config.SITE_NAME
    base = config.BASE_URL.rstrip("/")
    return f"""<!DOCTYPE html>
<html lang="ja">
<body style="margin:0;padding:0;background:#0b0f14;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#0b0f14;padding:28px 12px;">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;background:#141b24;border-radius:16px;overflow:hidden;font-family:-apple-system,'Hiragino Kaku Gothic ProN',Meiryo,sans-serif;">

<tr><td align="center" style="padding:30px 32px 22px;background:#101820;">
<img src="{base}/static/pwa/icon-192.png" width="56" height="56" alt="Me Talk" style="display:block;border:0;">
<div style="font-size:25px;font-weight:bold;color:#34d399;margin-top:10px;letter-spacing:0.5px;">{site}</div>
<div style="font-size:12px;color:#6b9080;margin-top:5px;">つながる、話せる、Me Talk。</div>
</td></tr>

<tr><td style="height:3px;background:#34d399;font-size:0;line-height:0;">&nbsp;</td></tr>

<tr><td style="padding:30px 32px 8px;">
<div style="font-size:15px;color:#e6e9ef;line-height:1.8;">{lead}</div>
</td></tr>

<tr><td style="padding:18px 32px 6px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#0b1119;border:1px solid #24303d;border-radius:12px;">
<tr><td align="center" style="padding:22px 16px;">
<div style="font-size:11px;color:#8b93a1;letter-spacing:1px;margin-bottom:10px;">確認コード</div>
<div style="font-size:34px;font-weight:bold;color:#34d399;letter-spacing:10px;font-family:'Courier New',monospace;">{code}</div>
</td></tr>
</table>
</td></tr>

<tr><td style="padding:14px 32px 4px;">
<div style="font-size:13px;color:#8b93a1;line-height:1.8;">
有効期限は <span style="color:#cdd3dc;">{CODE_TTL_MINUTES}分間</span> です。<br>
コードは他の人に教えないでください。
</div>
</td></tr>

<tr><td style="padding:16px 32px 22px;">
<div style="font-size:12px;color:#6b7280;line-height:1.8;border-top:1px solid #1f2733;padding-top:16px;">
心当たりがない場合は、このメールを削除していただいて問題ありません。{label}の手続きは完了しません。
</div>
</td></tr>

<tr><td style="padding:20px 26px 24px;background:#101820;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0">
<tr>
<td valign="middle" align="left" style="padding-right:10px;">
<img src="{base}/static/brand/mirai-mark.png" height="26" alt="MIRAI GROUP" style="border:0;vertical-align:middle;">
<img src="{base}/static/brand/akr-dev.png" height="22" alt="AKR DEV corporation" style="border:0;vertical-align:middle;margin-left:10px;">
<div style="font-size:10px;color:#4b5563;margin-top:8px;">MIRAI GROUP / AKR DEV corporation</div>
</td>
<td valign="middle" align="right">
<a href="{base}/app" style="display:inline-block;background:#34d399;color:#04140d;font-size:12px;font-weight:bold;text-decoration:none;padding:9px 16px;border-radius:8px;margin-bottom:7px;">Me Talk を開く</a>
<br>
<a href="{base}/legal/terms" style="display:inline-block;border:1px solid #2f3a48;color:#8b93a1;font-size:11px;text-decoration:none;padding:6px 13px;border-radius:7px;margin-bottom:6px;">利用規約</a>
<br>
<a href="{base}/legal/privacy" style="display:inline-block;border:1px solid #2f3a48;color:#8b93a1;font-size:11px;text-decoration:none;padding:6px 13px;border-radius:7px;">プライバシーポリシー</a>
</td>
</tr>
</table>
<div style="font-size:11px;color:#4b5563;margin-top:16px;border-top:1px solid #1a222c;padding-top:12px;">
<a href="{base}" style="color:#34d399;text-decoration:none;">{base}</a>
&nbsp;/&nbsp;このメールは送信専用です。返信はできません。
</div>
</td></tr>

</table>
</td></tr>
</table>
</body>
</html>"""


def send_mail(to: str, subject: str, text: str, html: str | None = None) -> bool:
    """任意のメールを1通送る。SMTP未設定なら送らずに False を返す。"""
    if not config.SMTP_HOST or not config.SMTP_USER or not config.SMTP_PASSWORD:
        print(f"[mail] SMTP未設定のため送信をスキップ: {to} / {subject}")
        return False
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.SMTP_FROM
    message["To"] = to
    message.set_content(text)
    if html:
        message.add_alternative(html, subtype="html")
    return _deliver(message, to)


def send_verification_code(email: str, code: str, purpose: str = "register") -> bool:
    if not config.SMTP_HOST or not config.SMTP_USER or not config.SMTP_PASSWORD:
        print(f"[mail] {email} 宛の確認コード({purpose}): {code}")
        return False
    message = EmailMessage()
    message["Subject"] = _subject(purpose)
    message["From"] = config.SMTP_FROM
    message["To"] = email
    message.set_content(_body_text(code, purpose))
    message.add_alternative(_body_html(code, purpose), subtype="html")
    ok = _deliver(message, email)
    if not ok:
        print(f"[mail] {email} 宛の確認コード({purpose}): {code}")
    return ok


def _deliver(message: EmailMessage, to: str) -> bool:
    try:
        secure = config.SMTP_SECURE.lower()
        if secure in {"ssl", "smtps"}:
            smtp = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=20)
        else:
            smtp = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20)
        with smtp:
            if secure not in {"none", "plain", "off", "ssl", "smtps"}:
                smtp.starttls()
            smtp.login(config.SMTP_USER, config.SMTP_PASSWORD)
            smtp.send_message(message)
        return True
    except Exception as exc:
        print(f"[mail] 送信失敗 {to}: {exc}")
        return False
