# セットアップガイド

> Ubuntu / Debian 系 Linux を想定。全手順を root ユーザーで実行してください（`sudo su -`）。

## 1. 専用ユーザーの作成

ボット専用ユーザーを作る。AI エンジンは `--dangerously-skip-permissions` 相当のオプションで動くため、**万が一暴走しても被害を限定する**ためのセキュリティ対策（詳細は [security.md](security.md) を参照）。

```bash
sudo adduser --disabled-password --gecos "clive bot" clive
```

> パスワードなしでも root から `sudo su - clive` で切り替えられる。

## 2. clive ユーザーで環境構築

clive ユーザーに切り替えてホーム直下にクローン:

```bash
sudo su - clive
git clone https://github.com/SkDevs-xx/Clive.git
cp -r Clive/. /home/clive/
rm -rf /home/clive/Clive/
```

Python 仮想環境を作成して依存パッケージをインストール:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
deactivate
exit
```

> **requirements.txt と requirements.lock:** ユーザーは `requirements.txt`（ゆるいバージョン指定）で十分。開発者はピン留めされた `requirements.lock` を使う。

## 3. AI CLI エンジンのインストール

`config.json` の `"engine"` で設定したエンジンに対応する CLI をインストールする。clive ユーザーで実行:

### Claude Code CLI（`"engine": "claude"`）

```bash
sudo su - clive
curl -fsSL https://claude.ai/install.sh | sh
claude
exit
```

> PATH の警告が出たら: `echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc`
> `claude` を実行するとブラウザが開く。VPS の場合は表示される URL を手元の PC で開く。

### Codex CLI（`"engine": "codex"`）

> 現在開発中。対応予定。

## 4. トークンの設定

`.env` はセキュリティのため **root ユーザーで** 作成し、clive ユーザーがファイルを直接読めないよう権限を設定する。systemd が起動時に展開して環境変数として渡す。

```bash
# root で実行
sudo vi /home/clive/.env
```

```
# Discord Bot
DISCORD_BOT_TOKEN=ここにあなたのDiscordBotTokenを貼る

# Slack Bot（Slack を使う場合）
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
```

保存したらパーミッションを設定する:

```bash
sudo chown root:root /home/clive/.env
sudo chmod 600 /home/clive/.env
```

> これにより clive ユーザー（ボットプロセス）は `.env` を直接読めなくなる。
> 開発時に手元で直接実行する場合は `.env.local` を clive ユーザーで用意する

Slack Bot トークンの取得手順は [platforms/slack/README.md](../platforms/slack/README.md) を参照。

## 5. 設定ファイルの編集

### config.json

`engine` を設定し、使用するプラットフォームの `enabled` を `true` にして `allowed_user_ids` を設定すれば動く:

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

> Discord ユーザー ID: 設定 > 詳細設定 > 開発者モード ON > 自分のアイコン右クリック > 「ユーザー ID をコピー」
> Slack ユーザー ID: プロフィール > 「その他」> 「メンバー ID をコピー」（`U` から始まる文字列）

全設定項目の説明: [config.md](config.md)

### .mcp.json

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

> 相対パスは使えません。必ず絶対パスを指定してください。

## 6. 動作確認

clive ユーザーのまま手動で起動して確認する:
`.env.localを用意する必要があります`
```bash
sudo su - clive
source venv/bin/activate
python3 main.py
```

ボットにメンションして返答が来れば OK。`Ctrl+C` で停止、`exit` で root に戻る。

## 7. systemd でデーモン化（本番運用）

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
