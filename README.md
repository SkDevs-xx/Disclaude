# Clive

チャットプラットフォームに常駐する自律型 AI エージェント。

AI CLI エンジン（Claude Code CLI / Codex CLI 等）をサブプロセスとして直接実行するアーキテクチャにより、API キーの管理やトークン課金が不要。
各 CLI の定額サブスクリプションだけで動作する。ファイルベースの永続記憶（SOUL.md / USER.md / EMOTION.md）で人格・記憶・感情を持ち、単なるタスク実行ツールではなく「住み着く AI」として振る舞う。

> プロジェクト名 "disclaude" は Discord + Claude の合成語に由来。
現在は Discord / Slack のマルチプラットフォームに対応し、バックエンドも `config.json` の `engine` キーで切り替えを実装予定。

## アーキテクチャ

```
Discord / Slack
    | メッセージ
Bot (Python)
    | サブプロセス (stdin/stdout)
AI CLI Engine (--dangerously-skip-permissions 相当)
    | MCP (stdin/stdout)
Browser MCP Server --> Chrome (CDP)
    | ファイル読み書き
workspace/ (SOUL.md, USER.md, CURIOSITY.md, EMOTION.md, ...)
```

## こんな使い方ができる

| やりたいこと | 対応する機能 |
|---|---|
| チャットで AI と対話したい | **会話** -- メンションするだけ。チャンネルごとにセッション保持。画像・PDF も添付可 |
| 毎日の定型業務を自動化したい | **スケジュール実行** + **Heartbeat** -- cron 式で定期プロンプト実行、自律巡回で日次 Wrap-up を生成 |
| チャットの振り返りをしたい | **チャンネル要約** -- 大量メッセージを 2 段階フィルタリングで要約 |
| AI に Web サービスを操作させたい | **ブラウザ操作** -- Chrome を遠隔操作。ログイン済みの X / Gmail 等をそのまま使える |
| AI に人格や記憶を持たせたい | **記憶システム** -- ファイルベースの永続記憶（人格・ユーザー情報・感情・知識アーカイブ） |
| 調べものを自律的にやらせたい | **自律調査 & レビュー** -- CURIOSITY.md に書いたトピックを自分で調査し、知識をカテゴリ別に蓄積 |
| 機能を拡張したい | **スキルシステム** -- SKILL.md 形式でスキルを定義、プラットフォームごとに有効/無効を制御 |

## 必要なもの

- **Git** -- `git --version` で確認。なければ `sudo apt install git`
- **Python 3.12 以上** -- `python3 --version` で確認
- **AI CLI エンジン** -- Claude Code CLI または Codex CLI。セットアップ手順内でインストールする
- **Discord Bot Token**（Discord を使う場合） -- [Discord Developer Portal](https://discord.com/developers/applications) で Bot を作成して取得
- **Slack Bot Token / App Token**（Slack を使う場合） -- [api.slack.com/apps](https://api.slack.com/apps) で App を作成して取得

## クイックスタート

> 以下の手順は Ubuntu / Debian 系 Linux を想定しています。
> 全て root ユーザーで実行してください（`sudo su -` で root になる）。

### 1. 専用ユーザーの作成

ボット専用のユーザーを作る。AI エンジンは `--dangerously-skip-permissions` 相当のオプションで動くため、**万が一暴走しても被害を限定する**ためのセキュリティ対策（詳細は[セキュリティ](#セキュリティ)を参照）。

```bash
sudo adduser --disabled-password --gecos "clive bot" clive
```

> パスワードなしでも root から `sudo su - clive` で切り替えられる。

### 2. clive ユーザーで環境構築

clive ユーザーに切り替えてホーム直下にクローン:

```bash
sudo su - clive
git clone https://github.com/SkDevs-xx/disclaude.git
cp -r disclaude/. /home/clive/
rm -rf /home/clive/disclaude/
```

Python 仮想環境を作成して依存パッケージをインストール:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
deactivate
exit
```

> **requirements.txt と requirements.lock:** ユーザーは `requirements.txt`（ゆるいバージョン指定）で十分。
開発者はピン留めされた `requirements.lock` を使う。

### 3. AI CLI エンジンのインストール

`config.json` の `"engine"` で設定したエンジンに対応する CLI をインストールする。自分の PC にインストール済みでも、サーバーの clive ユーザーには別途必要。clive ユーザーで実行:

#### Claude Code CLI の場合（`"engine": "claude"`）

```bash
curl -fsSL https://claude.ai/install.sh | sh
claude
```

> PATH の警告が出たら: `echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc`
> `claude` を実行するとブラウザが開く。VPS の場合は表示される URL を手元の PC で開く。

#### Codex CLI の場合（`"engine": "codex"`）

> 現在開発中。対応予定。

### 4. トークンの設定

`.env` を編集して取得したトークンを貼り付ける:

```bash
vi .env
```

```
# Discord Bot
DISCORD_BOT_TOKEN=ここにあなたのDiscordBotTokenを貼る

# Slack Bot（Slack を使う場合）
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
```

### 5. 設定ファイルの編集

#### config.json

`config.json` を編集する。`engine` を設定し、使用するプラットフォームの `enabled` を `true` にして `allowed_user_ids` を設定すれば動く:

```json
{
  "engine": "claude",
  "novnc_bind_address": "localhost",
  "discord": {
    "enabled": true,
    "model": "sonnet",
    "thinking": false,
    "skip_permissions": true,
    "browser_enabled": false,
    "browser_cdp_port": 9222,
    "browser_novnc_port": 6080,
    "browser_vnc_port": 5900,
    "browser_vnc_display": ":99",
    "allowed_user_ids": ["あなたのDiscordユーザーID"],
    "heartbeat_enabled": false,
    "heartbeat_channel_id": "",
    "heartbeat_interval_minutes": 60,
    "no_mention_channels": []
  },
  "slack": {
    "enabled": false,
    "model": "sonnet",
    "thinking": false,
    "skip_permissions": true,
    "browser_enabled": false,
    "browser_cdp_port": 9221,
    "browser_novnc_port": 6081,
    "browser_vnc_port": 5901,
    "browser_vnc_display": ":100",
    "allowed_user_ids": ["あなたのSlackユーザーID"],
    "heartbeat_enabled": false,
    "heartbeat_channel_id": "",
    "heartbeat_interval_minutes": 60,
    "no_mention_channels": [],
    "reply_in_thread": true
  }
}
```


> Discord ユーザー ID: Discord の設定 > 詳細設定 > 開発者モードを ON > 自分のアイコンを右クリック > 「ユーザー ID をコピー」

> Slack   ユーザー ID: Slack でプロフィールを開く > 「その他」> 「メンバー ID をコピー」（`U` から始まる文字列）

#### .mcp.json の編集

`/home/clive` の部分を **自分の環境の絶対パス** に置き換える（home に配置していればスキップ）:

```json
{
  "mcpServers": {
    "clive-browser": {
      "command": "/home/clive/venv/bin/python",
      "args": ["-m", "browser"],
      "cwd": "/home/clive"
    }
  }
}
```

> 相対パスは使えません。必ず `/home/` から始まる絶対パスを指定してください。

### 6. 動作確認

ここまでで基本機能は使える。clive ユーザーのまま手動で起動して確認する:

```bash
source venv/bin/activate
python3 main.py
```

ボットにメンションして返答が来れば OK。`Ctrl+C` で停止し、`exit` で root に戻る。

### 7. systemd でデーモン化（本番運用）

root ユーザーで実行する:

```bash
sudo cp /home/clive/clive.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable clive
sudo systemctl start clive
```

> `enable` = OS 起動時に自動スタート、`start` = 今すぐ起動。

設定変更後の再起動:

```bash
sudo systemctl restart clive
```

ログの確認:

```bash
# リアルタイム
journalctl -u clive -f

# 直近 1 時間
journalctl -u clive --since "1 hour ago"
```

## コマンド一覧

Discord と Slack で同等のコマンドが使えます。Discord はプレフィックスなし（`/model`）、Slack は `-ai` サフィックス付き（`/model-ai`）です。

### 基本操作

| Discord | Slack | 説明 |
|---------|-------|------|
| `@bot メッセージ` | `@bot メッセージ` | AI と会話（画像・PDF の添付も可） |
| `/model` | `/model-ai` | モデル（Sonnet / Opus / Haiku）と Thinking ON/OFF を設定 |
| `/status` | `/status-ai` | 現在のモデル・Thinking・実行中タスクの確認（Slack はスレッド返信 ON/OFF も設定可） |
| `/cancel` | `/cancel-ai` | 現在のチャンネルで実行中のプロセスを中止 |
| `/reset` | `/reset-ai` | 会話セッションをリセット（次のメッセージから新しい会話が始まる） |
| `/mention` | `/mention-ai` | このチャンネルでのメンション要否を設定（OFF にするとメンションなしで反応） |

### スケジュール

| Discord | Slack | 説明 |
|---------|-------|------|
| `/schedule add` | `/schedule-ai add` | 定期実行タスクの追加（UI フォーム） |
| `/schedule list` | `/schedule-ai list` | 登録済みスケジュールの一覧・編集・一時停止・削除 |

### 分析

| Discord | Slack | 説明 |
|---------|-------|------|
| `/summarize [prompt]` | `/summarize-ai [prompt]` | チャンネルの会話を AI に質問・要約（カスタムプロンプト対応） |

### Heartbeat

| Discord | Slack | 説明 |
|---------|-------|------|
| `/heartbeat` | `/heartbeat-ai` | Heartbeat のステータス表示・設定変更・手動実行（Slack は Heartbeat 専用 Thinking モードも設定可） |

### レビュー

| Discord | Slack | 説明 |
|---------|-------|------|
| `/review` | `/review-ai` | 調査済みトピックのレビュー・フィードバック |

### スキル

| Discord | Slack | 説明 |
|---------|-------|------|
| `/skills-list` | `/skills-list` | 利用可能なスキル一覧を表示し、ボタンで選択して発動する（新規作成スキルも再起動不要で認識） |

## 詳細設定

### config.json リファレンス

**グローバル設定:**

| キー | 説明 | デフォルト |
|------|------|-----------|
| `engine` | 使用する CLI エンジン（`claude` / `codex`）。**必須** | なし（未設定時はエラーで起動停止） |
| `novnc_bind_address` | noVNC のバインド先 IP（後述） | `localhost` |

**Discord 設定:**

| キー | 説明 | デフォルト |
|------|------|-----------|
| `discord.enabled` | Discord Bot を起動するか | `false` |
| `discord.model` | モデル名（`sonnet` / `opus` / `haiku`） | `sonnet` |
| `discord.thinking` | 思考モードの有効化 | `false` |
| `discord.skip_permissions` | CLI の権限確認をスキップ | `true` |
| `discord.browser_enabled` | ブラウザ操作機能の有効化 | `false` |
| `discord.browser_cdp_port` | Chrome DevTools Protocol のポート | `9222` |
| `discord.browser_novnc_port` | noVNC Web UI のポート | `6080` |
| `discord.browser_vnc_port` | Xtigervnc の VNC ポート | `5900` |
| `discord.browser_vnc_display` | Xtigervnc の仮想ディスプレイ番号 | `":99"` |
| `discord.allowed_user_ids` | ボットに話しかけられるユーザーの Discord ID（配列） | なし（必須） |
| `discord.heartbeat_enabled` | Heartbeat 自律巡回の有効化 | `false` |
| `discord.heartbeat_channel_id` | Heartbeat 通知を送るチャンネル ID | なし |
| `discord.heartbeat_interval_minutes` | Heartbeat の実行間隔（分） | `60` |
| `discord.heartbeat_thinking` | Heartbeat 専用の Thinking モード | `false` |
| `discord.no_mention_channels` | メンション不要で反応するチャンネル ID（配列） | `[]` |

**Slack 設定:**

| キー | 説明 | デフォルト |
|------|------|-----------|
| `slack.enabled` | Slack Bot を起動するか | `false` |
| `slack.allowed_user_ids` | ボットに話しかけられるユーザーの Slack ユーザー ID（配列） | なし（必須） |
| `slack.no_mention_channels` | メンション不要で反応するチャンネル ID（配列） | `[]` |
| `slack.browser_enabled` | ブラウザ操作機能の有効化 | `false` |
| `slack.browser_cdp_port` | Chrome DevTools Protocol のポート | `9221` |
| `slack.browser_novnc_port` | noVNC Web UI のポート | `6081` |
| `slack.browser_vnc_port` | Xtigervnc の VNC ポート | `5901` |
| `slack.browser_vnc_display` | Xtigervnc の仮想ディスプレイ番号 | `":100"` |
| `slack.reply_in_thread` | メッセージをスレッドで返信するか（`false` でチャンネルに直接投稿） | `true` |
| `slack.heartbeat_thinking` | Heartbeat 専用の Thinking モード | `false` |

### ブラウザ操作

AI に X への投稿や Gmail の確認など、**ログイン済みの Web サービスをブラウザ経由で操作** させたい場合に設定する。

#### 環境別の違い

| 環境 | Chrome | 仮想ディスプレイ | VNC / noVNC | 初回ログイン方法 |
|------|--------|-----------------|-------------|-----------------|
| **Windows（GUI あり）** | インストール済みでOK | 不要 | 不要 | Chrome が画面に表示されるので直接ログイン |
| **Linux デスクトップ（GUI あり）** | 要インストール | 不要（DISPLAY が既にある） | 不要 | Chrome が画面に表示されるので直接ログイン |
| **Linux VPS（GUI なし）** | 要インストール | 要（Xtigervnc） | 要（noVNC で遠隔ログイン） | ブラウザから noVNC 経由でログイン |

> VPS 環境では `browser_vnc_display` に指定した番号の仮想ディスプレイを Xtigervnc で起動する。
ディスプレイのロックファイル（`/tmp/.X{N}-lock`）が既に存在する場合は既存のディスプレイを再利用する。

#### GUI 環境（Windows / Linux デスクトップ）の場合

1. Chrome がインストールされていることを確認
2. `config.json` の `browser_enabled` を `true` に変更
3. bot を起動すると Chrome ウインドウが開く
4. 開いた Chrome で X や Gmail にログインする（初回のみ）
5. ログイン後、Chrome はそのまま裏で動き続ける

> Windows の場合、systemd は使えないので `python3 main.py` で直接実行するか、タスクスケジューラで自動起動を設定する。

> Linux デスクトップの場合、日本語フォントがない環境では `sudo apt install fonts-noto-cjk` が必要。

以下の VPS 向け手順は読み飛ばして OK。

---

#### VPS（GUI なし）の場合

VPS にはモニターがないので、Chrome を動かすには「仮想ディスプレイ」が必要。bot が以下を自動で立ち上げる:

| プロセス | 何をするか |
|---|---|
| **Xtigervnc** | モニターの代わりになる仮想ディスプレイ + VNC サーバー |
| **Chrome** | AI が操作するブラウザ |
| **noVNC** | ブラウザから VNC に接続できる Web 画面（初回ログイン用） |

#### Step 1: 必要パッケージのインストール（root で実行）

```bash
sudo apt install tigervnc-standalone-server novnc google-chrome-stable fonts-noto-cjk
```

| パッケージ | なぜ必要か |
|---|---|
| `tigervnc-standalone-server` | 仮想ディスプレイ + VNC サーバー（Chrome を動かすために必須） |
| `novnc` | PC のブラウザから VNC に接続する Web クライアント |
| `google-chrome-stable` | AI が操作するブラウザ本体 |
| `fonts-noto-cjk` | 日本語フォント（ないと Chrome で文字が全部 □ になる） |

#### Step 2: VNC パスワードの設定（clive ユーザーで実行）

```bash
sudo su - clive
vncpasswd
```

パスワードを入力 > 確認入力 > "Would you like to enter a view-only password?" には `n` と答える。`exit` で root に戻る。

#### Step 3: config.json を編集

使うプラットフォームの `browser_enabled` を `true` に、`novnc_bind_address` をサーバーの IP に変更する:

```json
{
  "engine": "claude",
  "novnc_bind_address": "cliveサーバーのIPアドレス",
  "discord": {
    "enabled": true,
    "model": "sonnet",
    "thinking": false,
    "skip_permissions": true,
    "browser_enabled": true,
    "browser_cdp_port": 9222,
    "browser_novnc_port": 6080,
    "browser_vnc_port": 5900,
    "browser_vnc_display": ":99",
    "allowed_user_ids": ["YOUR_DISCORD_USER_ID"],
    "heartbeat_enabled": false,
    "heartbeat_channel_id": "YOUR_HEARTBEAT_CHANNEL_ID",
    "heartbeat_interval_minutes": 60,
    "heartbeat_thinking": false,
    "no_mention_channels": []
  },
  "slack": {
    "enabled": true,
    "model": "sonnet",
    "thinking": false,
    "skip_permissions": true,
    "browser_enabled": true,
    "browser_cdp_port": 9221,
    "browser_novnc_port": 6081,
    "browser_vnc_port": 5901,
    "browser_vnc_display": ":100",
    "allowed_user_ids": ["YOUR_SLACK_USER_ID"],
    "heartbeat_enabled": false,
    "heartbeat_channel_id": "YOUR_HEARTBEAT_CHANNEL_ID",
    "heartbeat_interval_minutes": 60,
    "heartbeat_thinking": false,
    "reply_in_thread": false,
    "no_mention_channels": []
  }
}
```

> **複数プラットフォームを同時起動する場合:** `browser_cdp_port` / `browser_novnc_port` / `browser_vnc_port` / `browser_vnc_display` 
すべてプラットフォームごとに別々の値を設定すること。同じ値を使うと起動時に競合してブラウザが立ち上がらない。

**`novnc_bind_address` に何を入れるか:**

| 環境 | 値 | 理由 |
|------|---|------|
| Tailscale 経由でアクセス | Tailscale の IP（例: `100.x.x.x`） | VPN 内からのみアクセス可能 |
| SSH トンネル経由でアクセス | `localhost` | ローカルからのみアクセス可能 |
| どこからでもアクセス（非推奨） | `0.0.0.0` | VNC パスワードだけで誰でも入れてしまう |

#### Step 4: デーモンを再起動

```bash
sudo systemctl restart clive
```

以下のようなログが出れば成功（Discord + Slack 同時起動の例）:

```
Xtigervnc started on :99, VNC port 5900 (pid=...)   <- Discord 用仮想ディスプレイ
Chrome started with CDP port 9222 (pid=...)
Chrome CDP ready on port 9222
noVNC started on port 6080 (pid=...)
Xtigervnc started on :100, VNC port 5901 (pid=...)  <- Slack 用仮想ディスプレイ
Chrome started with CDP port 9221 (pid=...)
Chrome CDP ready on port 9221
noVNC started on port 6081 (pid=...)
```

#### Step 5: 初回ログイン（1回だけ）

X や Gmail など、AI に使わせたいサービスにブラウザから手動でログインする。**ログイン情報（Cookie）はサーバーに保存されるので、これは 1 回だけやれば OK。**

1. PC のブラウザで noVNC にアクセス（プラットフォームごとにポートが異なる）
   - Discord: `http://<novnc_bind_addressのIP>:6080/vnc.html`
   - Slack: `http://<novnc_bind_addressのIP>:6081/vnc.html`
   - 例（Tailscale）: `http://100.x.x.x:6080/vnc.html`
2. Step 2 で設定した VNC パスワードを入力
3. Chrome の画面が見える。アドレスバーに URL を入力して X や Gmail にログインする
4. ログインが終わったら VNC 画面のタブを閉じる（Chrome はサーバー上で動き続ける）

> Cookie はプラットフォームごとに `~/.config/clive-chrome-discord/` / `~/.config/clive-chrome-slack/` に保存される。Chrome をアップデートしてもログインは維持される。

> Chrome がクラッシュしても自動で再起動する（指数バックオフ: 5秒 > 10秒 > ... > 最大5分）。

SSH トンネル経由でアクセスする場合（`novnc_bind_address` が `localhost` のとき）:

```bash
# Discord
ssh -L 6080:localhost:6080 clive@your-server
# Slack（別ターミナルで実行）
ssh -L 6081:localhost:6081 clive@your-server
```

> `http://localhost:6080/vnc.html`（Discord）/ `http://localhost:6081/vnc.html`（Slack）でアクセスできる。

詳細は [browser/README.md](browser/README.md) を参照。

### スキルシステム

`skills/` ディレクトリに SKILL.md を配置することで、AI エンジンへの指示を拡張できる。

#### SKILL.md の書き方

```yaml
---
name: review
description: 調査済みトピックのレビューと報告
platforms: [discord, slack]
user-invocable: true
---

# Instructions
ここに AI エンジンへの指示を書く。
```

#### フィールド一覧

| フィールド | 必須 | 説明 |
|---|---|---|
| `name` | Yes | スキルの識別名（ディレクトリ名と一致させる） |
| `description` | Yes | スキルの説明（マッチング時の判断に使用） |
| `platforms` | No | 有効なプラットフォーム（省略すると全プラットフォームで有効） |
| `user-invocable` | No | `true` でユーザーが直接呼び出せるスキルとして登録 |

#### 動作の仕組み

- `skills/` 配下のディレクトリを起動時にスキャンし、各 SKILL.md を読み込む
- `platforms` フィールドで現在のプラットフォームに該当するスキルだけが有効化される
- スキルの Instructions セクションはエンジン呼び出し時にプロンプトの先頭に自動注入される
- `user-invocable: true` のスキルは `/skills-list` コマンドのボタンから直接呼び出せる
- `/skills-list` 実行時にスキルを再スキャンするため、新規作成したスキルは**再起動不要で即座に認識される**

#### 例: 毎朝 X に投稿するスキル

```yaml
---
name: morning-post
description: 毎朝の X 投稿を作成して投稿する
platforms: [discord]
user-invocable: false
---

# Instructions
HEARTBEAT.md のチェックリストに従い、毎朝の X 投稿を作成する。
ブラウザで X を開き、投稿内容を入力して送信する。
投稿内容は USER.md の関心事に基づいて生成する。
```

## セキュリティ

AI エンジンは `--dangerously-skip-permissions` 相当のオプションで動作し、任意のコマンドを実行できる。以下の多層防御で被害を限定する。

### 専用ユーザーによる分離

`clive` 専用ユーザーで実行することで、他のユーザーのファイルやプロセスへのアクセスを OS レベルで制限する。

### systemd サンドボックス

サービスファイル（`clive.service`）に以下のサンドボックス設定が含まれている:

| 設定 | 効果 |
|------|------|
| `ProtectSystem=strict` | `/usr`, `/etc` 等のシステムファイルへの書き込みを禁止 |
| `ReadWritePaths=/home/clive` | clive の home だけ書き込み許可 |
| `PrivateTmp=true` | `/tmp` を他プロセスと分離（覗き見できない） |
| `NoNewPrivileges=true` | 権限昇格（sudo 等）を禁止 |
| `ProtectHome=false` | `ProtectSystem=strict` + `ReadWritePaths` で十分なため無効化 |

これにより、AI エンジンが自由にコマンドを実行しても:

- `/home/clive` の外に書き込めない
- 他ユーザーのファイルが見えない
- root 権限を取得できない

### ブラウザ操作のリスク

ブラウザ操作を有効にすると、AI は Chrome の Cookie にアクセスできる。
つまりログイン済みサービスの操作権限を持つことになる。`allowed_user_ids` で操作を許可するユーザーを限定し、不要なサービスにはログインしないこと。

### noVNC のアクセス制限

`novnc_bind_address` を `0.0.0.0` にすると、VNC パスワードだけでインターネット上の誰でもブラウザ画面にアクセスできてしまう。Tailscale や SSH トンネル経由でのアクセスを推奨する。

## マルチプラットフォーム

プラットフォームごとに独立したワークスペース（人格・記憶・設定）を持つ設計。`config.json` の `enabled` フラグで起動するプラットフォームを制御する:

```json
{
  "engine": "claude",
  "discord": { "enabled": true, ... },
  "slack":   { "enabled": false, ... }
}
```

新しいプラットフォーム用ワークスペースを初期化
`例：discordの記憶をslackに移植する場合`
```bash
source venv/bin/activate
python3 main.py --init-workspace slack --from discord
deactivate
```

コピー後に `platforms/slack/workspace/SOUL.md` を編集してプラットフォームに合った人格に調整する。

### Slack App の設定

マニフェスト JSON をコピペするだけでセットアップできる。詳細は [platforms/slack/README.md](platforms/slack/README.md) を参照。

## 仕組み

AI CLI エンジン（Claude Code CLI / Codex CLI 等）をサブプロセスとして実行し、Discord メッセージやスケジュールに応じてプロンプトを渡す。API キーは不要で、各 CLI の認証・サブスクリプションで動作する。

- チャンネルごとに非同期ロックで排他制御
- セッション ID でマルチターン会話を維持
- 2000 文字制限は Markdown 構造を保ったまま自動分割
- タイムアウト: fast モード 180 秒 / planning モード 300 秒
- スキルエンジンが SKILL.md を読み込み、プラットフォームに応じた指示を AI エンジンに注入

## プロジェクト構成

```
clive/
├── main.py                          # エントリポイント（config.json の enabled で起動）
├── config.json                      # 全設定（グローバル + プラットフォーム別）
├── .env                             # Bot Token テンプレート
├── .mcp.json                        # MCP サーバー登録
│
├── core/                            # プラットフォーム非依存
│   ├── engine.py                    # エンジン抽象化（Claude / Codex CLI ディスパッチャ）
│   ├── config.py                    # 設定管理（init_workspace で動的パス切替）
│   ├── message.py                   # split_message 等テキストユーティリティ
│   ├── memory.py                    # HEARTBEAT.md / REVIEW.md パース・書き込み
│   ├── wrapup.py                    # 日次 Wrap-up ロジック
│   ├── scheduler.py                 # cron ユーティリティ
│   ├── attachments.py               # 添付ファイル処理
│   └── skills/                      # スキルエンジン
│       ├── models.py                # Skill dataclass
│       ├── loader.py                # SKILL.md パーサー（YAML frontmatter）
│       └── registry.py              # スキル登録・検索・マッチング
│
├── skills/                          # スキル定義（SKILL.md を置く場所）
│   ├── review/SKILL.md
│   └── heartbeat/SKILL.md
│
├── platforms/                       # プラットフォーム固有
│   ├── base.py                      # PlatformContext dataclass
│   ├── discord/
│   │   ├── __init__.py              # DISCORD_FORMAT_HINT 定数
│   │   ├── bot.py                   # ClaudeBot 本体
│   │   ├── embeds.py                # Discord Embed 生成
│   │   ├── utils.py                 # get_guild_channels, make_discord_collector
│   │   ├── cogs/
│   │   │   ├── utility.py           # /model, /status, /cancel, /reset, /mention
│   │   │   ├── schedule.py          # スケジュール管理
│   │   │   ├── summarize.py         # チャンネル要約
│   │   │   ├── heartbeat.py         # 自律巡回エージェント
│   │   │   └── review.py            # 調査トピックレビュー
│   │   └── workspace/               # Discord 専用ワークスペース（構成は Slack と同じ）
│   └── slack/
│       ├── __init__.py              # SLACK_FORMAT_HINT 定数
│       ├── bot.py                   # SlackBot 本体（Bolt + Socket Mode）
│       ├── utils.py                 # get_workspace_channels, make_slack_collector
│       ├── cogs/
│       │   ├── message.py           # app_mention + DM + メンション不要チャンネル
│       │   ├── commands.py          # /model-ai, /status-ai, /cancel-ai, /reset-ai, /mention-ai
│       │   ├── schedule.py          # /schedule-ai
│       │   ├── summarize.py         # /summarize-ai
│       │   ├── heartbeat.py         # /heartbeat-ai（Heartbeat 専用 Thinking 設定付き）
│       │   └── review.py            # /review-ai
│       └── workspace/               # Slack 専用ワークスペース
│           ├── SOUL.md              # AI の人格・価値観
│           ├── USER.md              # ユーザー情報
│           ├── CURIOSITY.md         # 未調査キュー
│           ├── REVIEW.md            # レビュー待ちトピック
│           ├── HEARTBEAT.md         # 巡回チェックリスト
│           ├── EMOTION.md           # 感情バロメーター
│           ├── sessions.json        # チャンネル別セッション ID
│           ├── channel_names.json   # チャンネル名キャッシュ
│           ├── schedules/           # 登録済みスケジュール定義
│           ├── temp/                # 一時ファイル
│           └── memory/
│               ├── curiosity/       # 調査済み知識アーカイブ
│               │   ├── self/        #   自己探求
│               │   ├── tech/        #   技術
│               │   └── business/    #   ビジネス
│               └── wrapup/          # 日次 Wrap-up ログ
│
└── browser/                         # ブラウザ操作
    ├── server.py                    # ブラウザ MCP サーバー
    ├── cdp.py                       # Chrome DevTools Protocol クライアント
    ├── tools.py                     # MCP ツール定義
    └── manager.py                   # Xtigervnc + Chrome + noVNC プロセス管理
```

## アンインストール

Clive を完全に削除する手順。root ユーザーで実行する。

### 1. サービスの停止・削除

```bash
sudo systemctl stop clive
sudo systemctl disable clive
sudo rm /etc/systemd/system/clive.service
sudo systemctl daemon-reload
```

### 2. clive ユーザーとホームディレクトリの削除

ホームディレクトリごと削除（config.json, .env, workspace/ 等すべて消える）:

```bash
sudo userdel -r clive
```

> ユーザーだけ消してデータを残したい場合は `-r` を外す。`/home/clive/` は手動で削除できる。

### 3. Chrome プロファイルの削除

ブラウザ操作を使っていた場合、Cookie やログイン情報が残っている:

```bash
sudo rm -rf /home/clive/.config/clive-chrome-discord
sudo rm -rf /home/clive/.config/clive-chrome-slack
```

> Step 2 で `-r` を付けて削除済みなら不要。

### 4. VPS 向けパッケージの削除（任意）

ブラウザ操作用にインストールしたパッケージが不要なら:

```bash
sudo apt remove --purge tigervnc-standalone-server novnc google-chrome-stable
sudo apt autoremove
```

> `fonts-noto-cjk` は他のアプリでも使われるため残しておいてよい。
完全に消したい場合は `sudo apt remove fonts-noto-cjk` で削除できる。

## ライセンス

MIT - SkDevs-xx 2026
