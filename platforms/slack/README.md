# Slack Bot セットアップガイド

このガイドに沿って進めれば、技術的な知識がなくても Slack ボットを動かせます。

---

## 全体の流れ

1. Slack App を作成する
2. トークンを取得する
3. `.env` に設定する
4. `config.json` を設定する
5. 起動する

---

## Step 1: Slack App を作成する

1. ブラウザで https://api.slack.com/apps を開く
2. 右上の **「Create New App」** をクリック
3. **「From an app manifest」** を選択
4. ボットを入れたいワークスペースを選んで **「Next」**
5. 画面上部の **「JSON」** タブをクリック
6. 以下の JSON を**まるごとコピーして貼り付け**（既存の内容は全部消してOK）

```json
{
  "display_information": {
    "name": "AI Bot"
  },
  "features": {
    "bot_user": {
      "display_name": "AI Bot",
      "always_online": true
    },
    "slash_commands": [
      {
        "command": "/model-ai",
        "description": "Switch the AI model (sonnet/opus/haiku)",
        "should_escape": false
      },
      {
        "command": "/status-ai",
        "description": "Show current bot status",
        "should_escape": false
      },
      {
        "command": "/cancel-ai",
        "description": "Cancel the running task in this channel",
        "should_escape": false
      },
      {
        "command": "/mention-ai",
        "description": "Toggle mention-required setting for this channel",
        "should_escape": false
      },
      {
        "command": "/heartbeat-ai",
        "description": "Show or configure the heartbeat scheduler",
        "should_escape": false
      },
      {
        "command": "/review-ai",
        "description": "Review pending research topics",
        "should_escape": false
      },
      {
        "command": "/schedule-ai",
        "description": "Manage scheduled tasks (add / list)",
        "should_escape": false
      },
      {
        "command": "/summarize-ai",
        "description": "Summarize channel messages with AI",
        "should_escape": false
      },
      {
        "command": "/skills-list",
        "description": "Show available skills and invoke one",
        "should_escape": false
      }
    ]
  },
  "oauth_config": {
    "scopes": {
      "bot": [
        "app_mentions:read",
        "channels:history",
        "channels:read",
        "chat:write",
        "commands",
        "files:read",
        "groups:history",
        "groups:read",
        "im:history",
        "im:read",
        "im:write",
        "mpim:history",
        "reactions:write",
        "users:read"
      ]
    }
  },
  "settings": {
    "event_subscriptions": {
      "bot_events": [
        "app_mention",
        "message.channels",
        "message.groups",
        "message.im"
      ]
    },
    "interactivity": {
      "is_enabled": true
    },
    "org_deploy_enabled": false,
    "socket_mode_enabled": true,
    "token_rotation_enabled": false
  }
}
```

7. **「Next」→「Create」** でアプリが作成される

---

## Step 2: ワークスペースにインストールする

1. 左メニューの **「Install App」** をクリック
2. **「Install to Workspace」** ボタンをクリック
3. 権限確認画面が出るので **「許可する」** をクリック
4. インストール完了後、画面に **Bot User OAuth Token** が表示される
   - `xoxb-` で始まる文字列
   - これが **SLACK_BOT_TOKEN**

---

## Step 3: App-Level Token を発行する

Socket Mode（WebSocket 接続）を使うために、もう1つトークンが必要です。

1. 左メニューの **「Basic Information」** をクリック
2. 下にスクロールして **「App-Level Tokens」** セクションを見つける
3. **「Generate Token and Scopes」** をクリック
4. Token Name に適当な名前を入力（例: `socket-token`）
5. **「Add Scope」** をクリックして **`connections:write`** を追加
6. **「Generate」** をクリック
7. 表示された **App-Level Token** をコピー
   - `xapp-` で始まる文字列
   - これが **SLACK_APP_TOKEN**

---

## Step 4: .env を設定する

プロジェクトのルートにある `.env` ファイルに以下を追記します。
`.env` が存在しない場合は `.env.example` をコピーして作成してください。

```
SLACK_BOT_TOKEN=xoxb-xxxxxxxxxxxx-xxxxxxxxxxxx-xxxxxxxxxxxxxxxxxxxxxxxx
SLACK_APP_TOKEN=xapp-1-xxxxxxxxxxxx-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

- `xoxb-...` の部分を Step 2 で取得した **Bot Token** に置き換える
- `xapp-...` の部分を Step 3 で取得した **App-Level Token** に置き換える

---

## Step 5: config.json を設定する

プロジェクトのルートにある `config.json` を編集します。
`config.json` が存在しない場合は `config.example.json` をコピーして作成してください。

```json
{
  "slack": {
    "enabled": true,
    "allowed_user_ids": ["U012AB3CD"]
  }
}
```

**`allowed_user_ids` の調べ方:**

1. Slack の自分のプロフィールを開く
2. 「...」メニュー →「メンバーIDをコピー」
3. コピーした `U` から始まる文字列を貼り付ける

---

## Step 6: 依存パッケージをインストールする

```bash
pip install -r requirements.txt
```

---

## Step 7: 起動する

```bash
python main.py
```

ターミナルに以下のようなログが出れば成功です:

```
INFO  Bolt app is running in Socket Mode!
```

---

## 使えるコマンド一覧

| コマンド | 説明 |
|---|---|
| `/model-ai` | AI モデルを切り替える（Sonnet / Opus / Haiku）・Thinking ON/OFF |
| `/status-ai` | 現在の設定と実行状況を確認する・スレッド返信 ON/OFF を切り替える |
| `/cancel-ai` | このチャンネルで実行中のタスクをキャンセルする |
| `/mention-ai` | @メンション不要モードの ON/OFF を切り替える |
| `/heartbeat-ai` | 定期レポートの設定・手動実行・Heartbeat 専用 Thinking ON/OFF |
| `/review-ai` | 調査済みトピックのフィードバックレビュー |
| `/schedule-ai add` | 定期タスクを追加する |
| `/schedule-ai list` | 登録済みの定期タスクを一覧表示する |
| `/summarize-ai` | チャンネルの会話を AI で要約する |
| `/summarize-ai 先週の議論` | 指定した内容でフィルタリングして要約する |
| `/skills-list` | 利用可能なスキル一覧を表示し、ボタンで選択して発動する（新規作成スキルも再起動不要で認識） |

---

## チャットの使い方

- **チャンネル**: `@AI Bot` とメンションしてメッセージを送る
- **ダイレクトメッセージ**: メンション不要でそのまま話しかける
- **メンション不要チャンネル**: `/mention-ai` で設定すると @メンションなしで全メッセージに反応する
- **ファイル添付**: 画像やテキストファイルを添付してメッセージを送れる
- **スレッド返信**: デフォルトでスレッドに返信。`/status-ai` でチャンネル直接投稿に変更可能

---

## トラブルシューティング

### 「SLACK_BOT_TOKEN が設定されていません」と出る

`.env` ファイルが正しい場所にあるか確認してください（`main.py` と同じディレクトリ）。
トークンの前後にスペースや引用符がないか確認してください。

### コマンドを打っても「このアプリは /xxx をサポートしていません」と出る

マニフェスト JSON の `slash_commands` にそのコマンドが含まれているか確認してください。
変更した場合はアプリを再インストールしてください（**「Install App」→「Reinstall to Workspace」**）。

### ボットがメッセージに反応しない

- ボットをチャンネルに招待しているか確認してください（`/invite @AI Bot`）
- `allowed_user_ids` に自分のメンバーIDが入っているか確認してください
- ターミナルのログにエラーが出ていないか確認してください

### メンション不要設定にしたのに反応しない

マニフェストの `bot_events` に `message.channels`（パブリックチャンネル）または `message.groups`（プライベートチャンネル）が含まれているか確認してください。
追加後は **「Install App」→「Reinstall to Workspace」** でアプリを再インストールしてください。

### 「Socket Mode is not enabled」と出る

マニフェストの `socket_mode_enabled: true` が設定されているか確認してください。
**「App Settings」→「Socket Mode」** から手動で有効化することもできます。
