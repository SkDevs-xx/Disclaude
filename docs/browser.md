# ブラウザ操作

AI に X への投稿や Gmail の確認など、**ログイン済みの Web サービスをブラウザ経由で操作**させたい場合に設定する。

Playwright MCP と違い、ユーザーが事前にログインした状態（X、Gmail 等）を維持したまま Clive がブラウザを操作できる。

## 仕組み

```
Chrome（永続プロファイル + CDP ポート）
  ↑ WebSocket（CDP 直接接続 + asyncio.Lock で排他制御）
MCP サーバー（browser/server.py）
  ↑ stdin/stdout（MCP プロトコル）
Clive (Claude / Codex)
```

## 環境別の違い

| 環境 | Chrome | 仮想ディスプレイ | VNC / noVNC | 初回ログイン方法 |
|------|--------|-----------------|-------------|-----------------|
| **Windows（GUI あり）** | インストール済みでOK | 不要 | 不要 | Chrome が画面に表示されるので直接ログイン |
| **Linux デスクトップ（GUI あり）** | 要インストール | 不要（DISPLAY が既にある） | 不要 | Chrome が画面に表示されるので直接ログイン |
| **Linux VPS（GUI なし）** | 要インストール | 要（Xtigervnc） | 要（noVNC で遠隔ログイン） | ブラウザから noVNC 経由でログイン |

## GUI 環境（Windows / Linux デスクトップ）

1. Chrome がインストールされていることを確認
2. `config.json` の `browser_enabled` を `true` に変更
3. bot を起動すると Chrome ウインドウが開く
4. 開いた Chrome で X や Gmail にログインする（初回のみ）
5. ログイン後、Chrome はそのまま裏で動き続ける

> Windows の場合、systemd は使えないので `python3 main.py` で直接実行するか、タスクスケジューラで自動起動を設定する。
> Linux デスクトップの場合、日本語フォントがない環境では `sudo apt install fonts-noto-cjk` が必要。

---

## VPS（GUI なし）のセットアップ

VPS にはモニターがないので、Chrome を動かすには仮想ディスプレイが必要。bot が以下を自動で立ち上げる:

| プロセス | Discord デフォルト | Slack デフォルト | 役割 |
|---|---|---|---|
| **Xtigervnc** | ディスプレイ `:99` / VNC ポート `5900` | ディスプレイ `:100` / VNC ポート `5901` | 仮想ディスプレイ + VNC サーバー |
| **Chrome** | CDP `9222` / プロファイル `clive-chrome-discord` | CDP `9221` / プロファイル `clive-chrome-slack` | AI が操作するブラウザ |
| **noVNC** | ポート `6080` | ポート `6081` | ブラウザから VNC に接続できる Web UI |

> ロックファイル `/tmp/.X{N}-lock` が存在すれば Xtigervnc の起動をスキップ（既存を再利用）。
> Chrome がクラッシュしても自動で再起動（指数バックオフ: 5秒 → 10秒 → ... → 最大5分）。

### Step 1: パッケージのインストール（root で実行）

```bash
sudo apt install tigervnc-standalone-server novnc google-chrome-stable fonts-noto-cjk
```

| パッケージ | 用途 |
|---|---|
| `tigervnc-standalone-server` | 仮想ディスプレイ + VNC サーバー（Chrome を動かすために必須） |
| `novnc` | PC のブラウザから VNC に接続する Web クライアント |
| `google-chrome-stable` | AI が操作するブラウザ本体 |
| `fonts-noto-cjk` | 日本語フォント（ないと Chrome で文字が □ になる） |

### Step 2: VNC パスワードの設定（clive ユーザーで実行）

```bash
sudo su - clive
vncpasswd
```

パスワードを入力 > 確認入力 > "Would you like to enter a view-only password?" には `n`。`exit` で root に戻る。

### Step 3: config.json を編集

使うプラットフォームの `browser_enabled` を `true` に、`novnc_bind_address` をサーバーの IP に変更する:

```json
{
  "engine": "claude",
  "novnc_bind_address": "cliveサーバーのIPアドレス",
  "discord": {
    "enabled": true,
    "browser_enabled": true,
    "browser_cdp_port": 9222,
    "browser_novnc_port": 6080,
    "browser_vnc_port": 5900,
    "browser_vnc_display": ":99"
  },
  "slack": {
    "enabled": true,
    "browser_enabled": true,
    "browser_cdp_port": 9221,
    "browser_novnc_port": 6081,
    "browser_vnc_port": 5901,
    "browser_vnc_display": ":100"
  }
}
```

> **複数プラットフォーム同時起動:** `browser_cdp_port` / `browser_novnc_port` / `browser_vnc_port` / `browser_vnc_display` は必ずプラットフォームごとに別々の値にすること。同じ値を使うと起動時に競合する。

**`novnc_bind_address` の値:**

| 環境 | 値 | 理由 |
|------|---|------|
| Tailscale 経由でアクセス | Tailscale の IP（例: `100.x.x.x`） | VPN 内からのみアクセス可能 |
| SSH トンネル経由でアクセス | `localhost` | ローカルからのみアクセス可能 |
| どこからでもアクセス（非推奨） | `0.0.0.0` | VNC パスワードだけで誰でも入れてしまう |

### Step 4: デーモンを再起動

```bash
sudo systemctl restart clive
```

以下のようなログが出れば成功（Discord + Slack 同時起動の例）:

```
Xtigervnc started on :99, VNC port 5900 (pid=...)
Chrome started with CDP port 9222 (pid=...)
Chrome CDP ready on port 9222
noVNC started on port 6080 (pid=...)
Xtigervnc started on :100, VNC port 5901 (pid=...)
Chrome started with CDP port 9221 (pid=...)
Chrome CDP ready on port 9221
noVNC started on port 6081 (pid=...)
```

### Step 5: 初回ログイン（1回だけ）

X や Gmail など、AI に使わせたいサービスにログインする。**Cookie はサーバーに保存されるので、これは 1 回だけやれば OK。**

1. PC のブラウザで noVNC にアクセス
   - Discord: `http://<novnc_bind_addressのIP>:6080/vnc.html`
   - Slack: `http://<novnc_bind_addressのIP>:6081/vnc.html`
2. Step 2 で設定した VNC パスワードを入力
3. Chrome の画面が見える。アドレスバーに URL を入力してログインする
4. ログインが終わったら VNC 画面のタブを閉じる（Chrome はサーバー上で動き続ける）

> Cookie はプラットフォームごとに `~/.config/clive-chrome-discord/` / `~/.config/clive-chrome-slack/` に保存される。

SSH トンネル経由でアクセスする場合（`novnc_bind_address` が `localhost` のとき）:

```bash
# Discord
ssh -L 6080:localhost:6080 clive@your-server
# Slack（別ターミナルで実行）
ssh -L 6081:localhost:6081 clive@your-server
```

> `http://localhost:6080/vnc.html`（Discord）/ `http://localhost:6081/vnc.html`（Slack）でアクセスできる。

---

## MCP ツール一覧

Clive が使えるブラウザ操作ツール（23 個）:

### Navigation

| ツール | 説明 |
|---|---|
| `browser_navigate` | URL に遷移 |
| `browser_back` | 「戻る」 |
| `browser_reload` | ページ再読み込み |
| `browser_get_url` | 現在の URL を取得 |

### Interaction

| ツール | 説明 |
|---|---|
| `browser_click` | 座標 (x, y) をクリック |
| `browser_double_click` | ダブルクリック |
| `browser_type` | テキスト入力 |
| `browser_clear_field` | フォーカス中の入力欄をクリア |
| `browser_press_key` | キーを押す（Enter, Tab 等） |
| `browser_scroll` | ページをスクロール（up / down） |

### Inspection

| ツール | 説明 |
|---|---|
| `browser_get_content` | ページのテキスト内容を取得 |
| `browser_find_element` | テキストや CSS セレクタで要素を探して座標を返す |
| `browser_status` | 接続状態・タブ一覧を確認 |

### Tabs

| ツール | 説明 |
|---|---|
| `browser_tabs` | タブ一覧・切り替え |
| `browser_new_tab` | 新しいタブを開く |
| `browser_close_tab` | 現在のタブを閉じる |

### Forms

| ツール | 説明 |
|---|---|
| `browser_select_option` | ドロップダウンの option を選択 |
| `browser_upload_file` | ファイルをアップロード |

### Waiting

| ツール | 説明 |
|---|---|
| `browser_wait` | ページの読み込み完了を待つ |
| `browser_wait_for_element` | CSS セレクタやテキストの要素が表示されるまで待つ |

### Dialogs

| ツール | 説明 |
|---|---|
| `browser_handle_dialog` | ダイアログ（alert, confirm, 「Leave site?」等）を処理 |

---

## トラブルシューティング

### Chrome に接続できない

```bash
curl http://127.0.0.1:9222/json   # Discord
curl http://127.0.0.1:9221/json   # Slack
```

レスポンスがなければ Chrome が起動していないか、CDP ポートが違う。`config.json` の `browser_cdp_port` を確認する。

### ログインが消えた

プラットフォームごとのプロファイルディレクトリが存在するか確認:

```bash
ls ~/.config/clive-chrome-discord/
ls ~/.config/clive-chrome-slack/
```

ディレクトリがなければ Cookie が保存されていない。Chrome の `--user-data-dir` パスが変わっていないか確認。

### MCP サーバーのテスト

```bash
venv/bin/python -m browser
```

MCP ハンドシェイクが始まれば正常。Ctrl+C で終了。
