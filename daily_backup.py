# daily_backup.py — 毎日の自動バックアップ
#
# 予約全件・顧客集計・POS売上明細をまとめたExcelを作成し、指定のメールアドレスに
# 添付ファイルとして送信する。Render の Cron Job から「python daily_backup.py」として
# 毎日1回実行される想定（手順は DEPLOY.md 参照）。
#
# 必要な環境変数（Web Serviceとは別に、Cron Job側にも設定する）:
#   DATABASE_URL              - 本番DBの接続文字列（Web Serviceと同じ値）
#   BACKUP_EMAIL_FROM         - 送信元Gmailアドレス
#   BACKUP_EMAIL_APP_PASSWORD - 上記Gmailの「アプリパスワード」（通常のログインパスワードではない）
#   BACKUP_EMAIL_TO           - 送信先メールアドレス（未設定ならBACKUP_EMAIL_FROM宛に送る）
import os
import smtplib
import sys
from email.message import EmailMessage

from backup_export import build_backup_workbook_bytes, today_jst


def send_email(attachment: bytes, filename: str) -> None:
    sender = os.environ['BACKUP_EMAIL_FROM']
    password = os.environ['BACKUP_EMAIL_APP_PASSWORD']
    to = os.environ.get('BACKUP_EMAIL_TO', sender)

    msg = EmailMessage()
    msg['Subject'] = f'【麒麟屋 予約システム】自動バックアップ {today_jst().isoformat()}'
    msg['From'] = sender
    msg['To'] = to
    msg.set_content(
        f'{today_jst().isoformat()} 時点の予約データバックアップです。\n'
        '自動送信メールのため返信不要です。'
    )
    msg.add_attachment(
        attachment,
        maintype='application',
        subtype='vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        filename=filename,
    )

    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
        s.login(sender, password)
        s.send_message(msg)


def main() -> None:
    required = ['DATABASE_URL', 'BACKUP_EMAIL_FROM', 'BACKUP_EMAIL_APP_PASSWORD']
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f'必要な環境変数が未設定です: {", ".join(missing)}')
        sys.exit(1)

    data = build_backup_workbook_bytes()
    fname = f'kiriniya_backup_{today_jst().isoformat()}.xlsx'
    send_email(data, fname)
    print(f'バックアップメール送信完了: {fname} -> {os.environ.get("BACKUP_EMAIL_TO", os.environ["BACKUP_EMAIL_FROM"])}')


if __name__ == '__main__':
    main()
