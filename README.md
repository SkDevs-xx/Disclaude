# Clive
<div align="center">

[![Setup](https://img.shields.io/badge/Setup-⚙️-4A90D9?style=flat-square)](docs/setup.md) [![Commands](https://img.shields.io/badge/Commands-⌨️-6A5ACD?style=flat-square)](docs/commands.md) [![Config](https://img.shields.io/badge/Config-🔧-2E8B57?style=flat-square)](docs/config.md) [![Browser](https://img.shields.io/badge/Browser-🌐-E67E22?style=flat-square)](docs/browser.md) [![Skills](https://img.shields.io/badge/Skills-🧠-9B59B6?style=flat-square)](docs/skills.md) [![Security](https://img.shields.io/badge/Security-🔒-E74C3C?style=flat-square)](docs/security.md) [![Slack Setup](https://img.shields.io/badge/Slack_Setup-💬-1ABC9C?style=flat-square)](docs/slack-bot-setup.md) [![Uninstall](https://img.shields.io/badge/Uninstall-🗑️-95A5A6?style=flat-square)](docs/uninstall.md)

</div>
チャットプラットフォームに常駐する自律型 AI エージェント。

AI CLI エンジン（Claude Code CLI / Codex CLI 等）をサブプロセスとして直接実行するアーキテクチャにより、API キーの管理やトークン課金が不要。各 CLI の定額サブスクリプションだけで動作する。ファイルベースの永続記憶（SOUL.md / USER.md / EMOTION.md）で人格・記憶・感情を持ち、単なるタスク実行ツールではなく「住み着く AI」として振る舞う。

> プロジェクト名 "disclaude" は Discord + Claude の合成語に由来。現在は Discord / Slack のマルチプラットフォームに対応。

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

- **Git** / **Python 3.12 以上**
- **AI CLI エンジン** -- Claude Code CLI または Codex CLI
- **Discord Bot Token**（Discord を使う場合）
- **Slack Bot Token / App Token**（Slack を使う場合）

## セットアップ

詳細は **[docs/setup.md](docs/setup.md)** を参照。

1. 専用ユーザー（`clive`）を作成
2. リポジトリをクローン、Python 仮想環境を構築
3. AI CLI エンジンをインストール・認証
4. `.env` にトークンを設定（root で作成、600 権限）
5. `config.json` を編集
6. `.mcp.json` のパスを確認
7. 動作確認 → systemd でデーモン化

## コマンド

Discord はプレフィックスなし（`/model`）、Slack は `-ai` サフィックス付き（`/model-ai`）。

| Discord | Slack | 説明 |
|---------|-------|------|
| `@bot メッセージ` | `@bot メッセージ` | AI と会話（画像・PDF 添付可） |
| `/model` | `/model-ai` | モデル・Thinking 設定 |
| `/cancel` | `/cancel-ai` | 実行中タスクをキャンセル |
| `/reset` | `/reset-ai` | 会話セッションをリセット |
| `/schedule` | `/schedule-ai` | 定期タスク管理 |

→ 全コマンド一覧: **[docs/commands.md](docs/commands.md)**

## ドキュメント

| ドキュメント | 内容 |
|---|---|
| [docs/setup.md](docs/setup.md) | インストール・初期設定手順 |
| [docs/commands.md](docs/commands.md) | 全コマンド一覧 |
| [docs/config.md](docs/config.md) | config.json リファレンス |
| [docs/browser.md](docs/browser.md) | ブラウザ操作のセットアップ（GUI / VPS） |
| [docs/skills.md](docs/skills.md) | スキルシステム |
| [docs/security.md](docs/security.md) | セキュリティ設計 |
| [docs/uninstall.md](docs/uninstall.md) | アンインストール手順 |
| [docs/slack-bot-setup.md](docs/slack-bot-setup.md) | Slack App セットアップ |

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
├── main.py                          # エントリポイント
├── config.json                      # 全設定（グローバル + プラットフォーム別）
├── .env                             # Bot Token（root 管理、600 権限）
├── .mcp.json                        # MCP サーバー登録
├── docs/                            # ドキュメント
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
│   │   ├── bot.py                   # ClaudeBot 本体
│   │   ├── embeds.py                # Discord Embed 生成
│   │   ├── utils.py                 # get_guild_channels, make_discord_collector
│   │   ├── cogs/
│   │   │   ├── utility.py           # /model, /status, /cancel, /reset, /mention
│   │   │   ├── schedule.py          # スケジュール管理
│   │   │   ├── summarize.py         # チャンネル要約
│   │   │   ├── heartbeat.py         # 自律巡回エージェント
│   │   │   └── review.py            # 調査トピックレビュー
│   │   └── workspace/               # Discord 専用ワークスペース
│   │       ├── SOUL.md              # AI の人格・価値観
│   │       ├── USER.md              # ユーザー情報
│   │       ├── CURIOSITY.md         # 未調査キュー
│   │       ├── REVIEW.md            # レビュー待ちトピック
│   │       ├── HEARTBEAT.md         # 巡回チェックリスト
│   │       ├── EMOTION.md           # 感情バロメーター
│   │       ├── sessions.json        # チャンネル別セッション ID
│   │       ├── schedules/           # 登録済みスケジュール定義
│   │       └── memory/              # 調査済み知識アーカイブ
│   └── slack/
│       ├── bot.py                   # SlackBot 本体（Bolt + Socket Mode）
│       ├── utils.py                 # get_workspace_channels, make_slack_collector
│       ├── cogs/
│       │   ├── message.py           # app_mention + DM + メンション不要チャンネル
│       │   ├── commands.py          # /model-ai 〜 /reset-ai 〜 /mention-ai
│       │   ├── schedule.py          # /schedule-ai
│       │   ├── summarize.py         # /summarize-ai
│       │   ├── heartbeat.py         # /heartbeat-ai
│       │   └── review.py            # /review-ai
│       └── workspace/               # Slack 専用ワークスペース
│          （Discord側と同じ仕組み）
│
└── browser/                         # ブラウザ操作 MCP サーバー
    ├── server.py                    # MCP サーバー
    ├── cdp.py                       # Chrome DevTools Protocol クライアント
    ├── tools.py                     # MCP ツール定義
    └── manager.py                   # Xtigervnc + Chrome + noVNC プロセス管理
```

## ライセンス

MIT - SkDevs-xx 2026
