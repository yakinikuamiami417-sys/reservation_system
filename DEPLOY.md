# キリン屋 予約管理システム — クラウドデプロイ手順

## 必要なもの
- GitHubアカウント（無料）: https://github.com
- Render.comアカウント（無料）: https://render.com

---

## ステップ1: GitHubにコードをアップロード

1. https://github.com にアクセスしてログイン
2. 右上の「＋」→「New repository」
3. Repository name: `kiriniya-reservation`
4. Private（非公開）を選択 ← 重要！
5. 「Create repository」をクリック
6. 表示されたコマンドをPCのコマンドプロンプトで実行：
   ```
   cd C:\Users\kirin\dev\LIFE\reservation_system
   git init
   git add .
   git commit -m "初回登録"
   git branch -M main
   git remote add origin https://github.com/あなたのID/kiriniya-reservation.git
   git push -u origin main
   ```

---

## ステップ2: Render.comでデータベースを作成

1. https://render.com にアクセスしてログイン（GitHubアカウントで可）
2. 「New +」→「PostgreSQL」
3. 設定：
   - Name: `kiriniya-db`
   - Region: Singapore（最も近い）
   - Plan: **Free**
4. 「Create Database」をクリック
5. 作成後、「Internal Database URL」をコピーして保存

---

## ステップ3: Render.comでアプリをデプロイ

1. 「New +」→「Web Service」
2. 「Connect a repository」→ GitHubを連携 → `kiriniya-reservation` を選択
3. 設定：
   - Name: `kiriniya-app`
   - Region: Singapore
   - Branch: main
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `gunicorn app:app --workers 2 --bind 0.0.0.0:$PORT`
   - Plan: **Free**
4. 「Environment Variables」に以下を追加：
   - `DATABASE_URL` = ステップ2でコピーしたURL
   - `SECRET_KEY` = 適当な長い文字列（例: `kiriniya-secret-2026-xyz789`）
   - `APP_PASSWORD` = お店のパスワード（例: `kirin2026`）
5. 「Create Web Service」をクリック

---

## ステップ4: 初回DB初期化

デプロイ完了後、ブラウザで以下にアクセス：
```
https://kiriniya-app.onrender.com/init-db
```

---

## アクセスURL

デプロイ後のURL（例）：
```
https://kiriniya-app.onrender.com
```

このURLをタブレット・スマホで開くと使えます。

---

## バックアップについて

- **毎日**: 右上の「↓」ボタン → CSVダウンロード → Google Driveに保存
- **Renderの無料PostgreSQL**: 90日間保持される（期限が来たら有料か移行が必要）
- **有料プラン($7/月)**: 毎日の自動バックアップ付き

---

## ご注意

- 無料プランは15分間アクセスがないとスリープします（最初のアクセスに30〜60秒かかる）
- 月$7の有料プランにするとスリープなしで快適に使えます
