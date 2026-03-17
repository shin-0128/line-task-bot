# LINE Task Bot

LINEグループのメッセージ・画像を監視し、Claude AIでタスクを自動検出してGoogleスプレッドシートに記録するBotです。

## 機能

- LINEグループへの送信メッセージをリアルタイムで受信
- テキスト・画像の両方からタスクをAI検出（Claude Opus）
- 検出したタスクをGoogleスプレッドシートに自動追記
- タスクをローカルJSONファイルにもバックアップ

## セットアップ

### 1. 依存パッケージのインストール

```bash
pip install -r requirements.txt
```

### 2. 環境変数の設定

`.env.example` をコピーして `.env` を作成し、各値を設定します。

```bash
cp .env.example .env
```

| 変数名 | 説明 |
|---|---|
| `LINE_CHANNEL_SECRET` | LINE Messaging API のチャンネルシークレット |
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE Messaging API のアクセストークン |
| `ANTHROPIC_API_KEY` | Anthropic API キー |
| `SPREADSHEET_ID` | 書き込み先のGoogleスプレッドシートID |
| `GOOGLE_CREDENTIALS_JSON` | サービスアカウントJSONキーファイルのパス |

### 3. Google Sheets APIの設定

1. [Google Cloud Console](https://console.cloud.google.com/) でプロジェクトを作成
2. Google Sheets API を有効化
3. サービスアカウントを作成してJSONキーをダウンロード
4. ダウンロードしたJSONのパスを `GOOGLE_CREDENTIALS_JSON` に設定
5. 対象スプレッドシートをサービスアカウントのメールアドレスに**編集者**として共有

### 4. スプレッドシートの準備

シート1行目に以下のヘッダーを手動で追加してください。

| A | B | C | D | E | F |
|---|---|---|---|---|---|
| 日時 | グループID | 送信者ID | タスク内容 | 担当者 | 期限 |

### 5. LINE Webhookの設定

1. [LINE Developers Console](https://developers.line.biz/) でチャンネルを作成
2. Webhook URLに `https://<your-domain>/webhook` を設定
3. Webhookの利用をオンにする
4. Botをグループに追加する

### 6. サーバー起動

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

## ローカル開発（ngrokを使う場合）

```bash
ngrok http 8000
```

表示されたURLをLINE DevelopersコンソールのWebhook URLに設定します。

## ディレクトリ構成

```
line-task-bot/
├── main.py                  # アプリケーション本体
├── requirements.txt         # 依存パッケージ
├── .env.example             # 環境変数テンプレート
├── .env                     # 環境変数（git管理外）
├── credentials.json         # GCPサービスアカウントキー（git管理外）
├── logs/                    # 受信ログ（git管理外）
└── tasks/                   # 検出タスク（git管理外）
```

## タスク検出の仕組み

以下のような表現を含むメッセージからタスクを検出します。

- 「〜やっておいて」「〜しておいて」
- 「〜お願い」「〜お願いします」「〜頼む」
- 「〜までに」（締め切りを示す表現）
- 「〜してください」「〜対応して」
