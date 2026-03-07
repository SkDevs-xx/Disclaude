"""MCP ツール定義 — Claude に公開するブラウザ操作ツール。"""

import asyncio
import json
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .cdp import CDPClient


def _load_port() -> int:
    """CLIVE_PLATFORM 環境変数を参照してプラットフォーム別の CDP ポートを読み取る。"""
    platform = os.environ.get("CLIVE_PLATFORM", "discord")
    config_path = Path(__file__).resolve().parent.parent / "config.json"
    try:
        with open(config_path) as f:
            cfg = json.load(f)
        return int(cfg.get(platform, {}).get("browser_cdp_port", 9222))
    except Exception:
        return 9222


# MCP サーバーはプラットフォームごとに独立したプロセスとして起動されるため、
# CDPClient のシングルトンはプロセス間で共有されない。
# 各プロセスは CLIVE_PLATFORM 環境変数で自身のプラットフォームを識別し、
# 対応する CDP ポートに接続する。
cdp = CDPClient()
_connected_port: int | None = None


def register_tools(mcp: FastMCP) -> None:
    """MCP サーバーにブラウザツールを登録する。"""

    async def _ensure_connected(allow_dialog: bool = False) -> str | None:
        """接続を確認する。保留ダイアログがあればその情報を返す（allow_dialog=True時は無視）。"""
        global _connected_port
        port = _load_port()
        if not cdp.is_connected or _connected_port != port:
            if cdp.is_connected:
                await cdp.disconnect()
            await cdp.connect(port=port)
            _connected_port = port
        if not allow_dialog:
            return _check_pending_dialog()
        return None

    def _check_pending_dialog() -> str | None:
        """保留中のダイアログがあれば通知用JSONを返す。なければNone。"""
        if cdp.pending_dialog is None:
            return None
        d = cdp.pending_dialog
        return json.dumps({
            "blocked_by_dialog": True,
            "dialog_type": d["type"],
            "url": d["url"],
            "message": d["message"],
            "instruction": "ダイアログが表示されています。内容を確認し browser_handle_dialog で accept=true/false を選択してください",
        }, ensure_ascii=False, indent=2)

    # ─── Navigation ───

    @mcp.tool(name="browser_navigate", description="指定した URL にブラウザを遷移させる")
    async def browser_navigate(url: str) -> str:
        if block := await _ensure_connected():
            return block
        result = await cdp.send("Page.navigate", {"url": url})
        return json.dumps({"navigated": url, "frameId": result.get("frameId", "")}, ensure_ascii=False)

    @mcp.tool(name="browser_back", description="ブラウザの「戻る」ボタンを押す")
    async def browser_back() -> str:
        if block := await _ensure_connected():
            return block
        await cdp.send("Runtime.evaluate", {
            "expression": "window.history.back()",
        })
        await asyncio.sleep(0.5)
        return json.dumps({"action": "back"})

    @mcp.tool(name="browser_reload", description="現在のページを再読み込みする")
    async def browser_reload() -> str:
        if block := await _ensure_connected():
            return block
        await cdp.send("Page.reload")
        return json.dumps({"action": "reload"})

    @mcp.tool(name="browser_get_url", description="現在のページの URL を取得する")
    async def browser_get_url() -> str:
        if block := await _ensure_connected():
            return block
        result = await cdp.send("Runtime.evaluate", {
            "expression": "window.location.href",
            "returnByValue": True,
        })
        url = result.get("result", {}).get("value", "")
        return json.dumps({"url": url}, ensure_ascii=False)

    # ─── Interaction ───

    @mcp.tool(name="browser_click", description="指定座標 (x, y) をクリックする")
    async def browser_click(x: int, y: int) -> str:
        if block := await _ensure_connected():
            return block
        await cdp.send("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1,
        })
        await cdp.send("Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1,
        })
        return json.dumps({"clicked": {"x": x, "y": y}})

    @mcp.tool(name="browser_double_click", description="指定座標 (x, y) をダブルクリックする")
    async def browser_double_click(x: int, y: int) -> str:
        if block := await _ensure_connected():
            return block
        for click_count in [1, 2]:
            await cdp.send("Input.dispatchMouseEvent", {
                "type": "mousePressed", "x": x, "y": y,
                "button": "left", "clickCount": click_count,
            })
            await cdp.send("Input.dispatchMouseEvent", {
                "type": "mouseReleased", "x": x, "y": y,
                "button": "left", "clickCount": click_count,
            })
        return json.dumps({"double_clicked": {"x": x, "y": y}})

    @mcp.tool(name="browser_type", description="テキストを入力する。事前にクリックで入力欄にフォーカスしてから使う。高速（一括入力）")
    async def browser_type(text: str) -> str:
        if block := await _ensure_connected():
            return block
        await cdp.send("Input.insertText", {"text": text})
        return json.dumps({"typed": text}, ensure_ascii=False)

    @mcp.tool(name="browser_type_slow", description="テキストを1文字ずつ入力する。Input.insertText で動作しないサイト向け")
    async def browser_type_slow(text: str) -> str:
        if block := await _ensure_connected():
            return block
        for char in text:
            await cdp.send("Input.dispatchKeyEvent", {"type": "keyDown", "text": char, "key": char})
            await cdp.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": char})
        return json.dumps({"typed": text}, ensure_ascii=False)

    @mcp.tool(name="browser_clear_field", description="現在フォーカスされている入力欄の内容をクリアする")
    async def browser_clear_field() -> str:
        if block := await _ensure_connected():
            return block
        await cdp.send("Input.dispatchKeyEvent", {
            "type": "keyDown", "key": "a", "modifiers": 2,
        })
        await cdp.send("Input.dispatchKeyEvent", {
            "type": "keyUp", "key": "a", "modifiers": 2,
        })
        await cdp.send("Input.dispatchKeyEvent", {
            "type": "keyDown", "key": "Backspace",
        })
        await cdp.send("Input.dispatchKeyEvent", {
            "type": "keyUp", "key": "Backspace",
        })
        return json.dumps({"action": "clear_field"})

    @mcp.tool(name="browser_press_key", description="キーボードのキーを押す（Enter, Tab, Escape 等）")
    async def browser_press_key(key: str) -> str:
        if block := await _ensure_connected():
            return block
        await cdp.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": key})
        await cdp.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": key})
        return json.dumps({"pressed": key})

    @mcp.tool(name="browser_scroll", description="ページをスクロールする。direction は 'up' または 'down'。amount はピクセル数（デフォルト 500）")
    async def browser_scroll(direction: str = "down", amount: int = 500) -> str:
        if block := await _ensure_connected():
            return block
        delta_y = amount if direction == "down" else -amount
        await cdp.send("Input.dispatchMouseEvent", {
            "type": "mouseWheel", "x": 480, "y": 540,
            "deltaX": 0, "deltaY": delta_y,
        })
        return json.dumps({"scrolled": direction, "amount": amount})

    # ─── Combined Actions（高速化） ───

    @mcp.tool(name="browser_click_element", description="CSSセレクタまたはテキストで要素を見つけてクリックする。find_element + click を1ステップで実行")
    async def browser_click_element(selector: str = "", text: str = "") -> str:
        if block := await _ensure_connected():
            return block
        if not selector and not text:
            return json.dumps({"error": "selector or text is required"}, ensure_ascii=False)
        if selector:
            js = f"""
                (() => {{
                    const el = document.querySelector({json.dumps(selector)});
                    if (!el) return {{ error: 'element not found', selector: {json.dumps(selector)} }};
                    el.scrollIntoView({{ block: 'center', behavior: 'instant' }});
                    el.click();
                    const r = el.getBoundingClientRect();
                    return {{
                        clicked: true,
                        tag: el.tagName.toLowerCase(),
                        text: (el.innerText || '').slice(0, 100),
                        x: Math.round(r.x + r.width / 2),
                        y: Math.round(r.y + r.height / 2),
                    }};
                }})()
            """
        else:
            js = f"""
                (() => {{
                    const search = {json.dumps(text)}.toLowerCase();
                    const walker = document.createTreeWalker(
                        document.body, NodeFilter.SHOW_TEXT, null
                    );
                    while (walker.nextNode()) {{
                        const node = walker.currentNode;
                        if (node.textContent.toLowerCase().includes(search)) {{
                            const el = node.parentElement;
                            const r = el.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0) {{
                                el.scrollIntoView({{ block: 'center', behavior: 'instant' }});
                                el.click();
                                return {{
                                    clicked: true,
                                    tag: el.tagName.toLowerCase(),
                                    text: (el.innerText || '').slice(0, 100),
                                    x: Math.round(r.x + r.width / 2),
                                    y: Math.round(r.y + r.height / 2),
                                }};
                            }}
                        }}
                    }}
                    return {{ error: 'element not found', search: {json.dumps(text)} }};
                }})()
            """
        result = await cdp.send("Runtime.evaluate", {
            "expression": js,
            "returnByValue": True,
        })
        return json.dumps(result.get("result", {}).get("value", {}), ensure_ascii=False, indent=2)

    @mcp.tool(name="browser_fill", description="CSSセレクタで入力欄を見つけてテキストを入力する。find + click + clear + type を1ステップで実行")
    async def browser_fill(selector: str, value: str) -> str:
        if block := await _ensure_connected():
            return block
        js = f"""
            (() => {{
                const el = document.querySelector({json.dumps(selector)});
                if (!el) return {{ error: 'element not found', selector: {json.dumps(selector)} }};
                el.scrollIntoView({{ block: 'center', behavior: 'instant' }});
                el.focus();
                const nativeSetter =
                    Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set ||
                    Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')?.set;
                if (nativeSetter) nativeSetter.call(el, {json.dumps(value)});
                else el.value = {json.dumps(value)};
                el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                return {{ filled: true, selector: {json.dumps(selector)}, value: {json.dumps(value)} }};
            }})()
        """
        result = await cdp.send("Runtime.evaluate", {
            "expression": js,
            "returnByValue": True,
        })
        return json.dumps(result.get("result", {}).get("value", {}), ensure_ascii=False, indent=2)

    # ─── Inspection ───

    @mcp.tool(name="browser_status", description="ブラウザ接続の状態を確認する")
    async def browser_status() -> str:
        try:
            port = _load_port()
            targets = await cdp.get_targets(port=port)
            connected = cdp.is_connected
            status = {
                "connected": connected,
                "cdp_port": port,
                "tabs": [{"title": t.get("title", ""), "url": t.get("url", "")} for t in targets],
            }
            if cdp.pending_dialog:
                status["pending_dialog"] = cdp.pending_dialog
            return json.dumps(status, ensure_ascii=False, indent=2)
        except Exception as e:
            return json.dumps({"connected": False, "error": str(e)}, ensure_ascii=False)

    @mcp.tool(name="browser_get_content", description="現在のページのテキスト内容を取得する")
    async def browser_get_content() -> str:
        if block := await _ensure_connected():
            return block
        result = await cdp.send("Runtime.evaluate", {
            "expression": "document.body.innerText",
            "returnByValue": True,
        })
        text = result.get("result", {}).get("value", "")
        if len(text) > 50000:
            text = text[:50000] + "\n... (truncated)"
        return text

    @mcp.tool(name="browser_find_element", description="テキストまたは CSS セレクタで要素を探し、座標とテキストを返す。見つからなければ空リスト")
    async def browser_find_element(text: str = "", selector: str = "") -> str:
        if block := await _ensure_connected():
            return block
        if selector:
            js = f"""
                (() => {{
                    const els = document.querySelectorAll({json.dumps(selector)});
                    return Array.from(els).slice(0, 10).map(el => {{
                        const r = el.getBoundingClientRect();
                        return {{
                            tag: el.tagName.toLowerCase(),
                            text: el.innerText?.slice(0, 100) || '',
                            x: Math.round(r.x + r.width / 2),
                            y: Math.round(r.y + r.height / 2),
                            width: Math.round(r.width),
                            height: Math.round(r.height),
                        }};
                    }});
                }})()
            """
        elif text:
            js = f"""
                (() => {{
                    const search = {json.dumps(text)}.toLowerCase();
                    const walker = document.createTreeWalker(
                        document.body, NodeFilter.SHOW_TEXT, null
                    );
                    const results = [];
                    while (walker.nextNode() && results.length < 10) {{
                        const node = walker.currentNode;
                        if (node.textContent.toLowerCase().includes(search)) {{
                            const el = node.parentElement;
                            const r = el.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0) {{
                                results.push({{
                                    tag: el.tagName.toLowerCase(),
                                    text: el.innerText?.slice(0, 100) || '',
                                    x: Math.round(r.x + r.width / 2),
                                    y: Math.round(r.y + r.height / 2),
                                    width: Math.round(r.width),
                                    height: Math.round(r.height),
                                }});
                            }}
                        }}
                    }}
                    return results;
                }})()
            """
        else:
            return json.dumps({"error": "text or selector is required"}, ensure_ascii=False)

        result = await cdp.send("Runtime.evaluate", {
            "expression": js,
            "returnByValue": True,
        })
        elements = result.get("result", {}).get("value", [])
        return json.dumps({"elements": elements}, ensure_ascii=False, indent=2)

    @mcp.tool(name="browser_snapshot", description="ページ上のインタラクティブ要素（リンク・ボタン・入力欄等）をすべて取得する。ページ構造の把握に使う")
    async def browser_snapshot() -> str:
        if block := await _ensure_connected():
            return block
        js = """
            (() => {
                const items = [];
                const seen = new Set();
                document.querySelectorAll(
                    'a[href], button, input, select, textarea, [role="button"], [role="link"], [role="tab"], [onclick], summary'
                ).forEach(el => {
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) return;
                    const key = `${Math.round(r.x)},${Math.round(r.y)}`;
                    if (seen.has(key)) return;
                    seen.add(key);
                    const item = {
                        tag: el.tagName.toLowerCase(),
                        x: Math.round(r.x + r.width / 2),
                        y: Math.round(r.y + r.height / 2),
                    };
                    const label = (
                        el.ariaLabel || el.title || el.placeholder ||
                        el.innerText || el.value || el.alt || ''
                    ).trim().slice(0, 80);
                    if (label) item.label = label;
                    if (el.type) item.type = el.type;
                    if (el.tagName === 'A' && el.href) item.href = el.href.slice(0, 120);
                    if (el.id) item.id = el.id;
                    if (el.name) item.name = el.name;
                    items.push(item);
                });
                return {
                    url: window.location.href,
                    title: document.title,
                    elements: items,
                    total: items.length,
                };
            })()
        """
        result = await cdp.send("Runtime.evaluate", {
            "expression": js,
            "returnByValue": True,
        })
        data = result.get("result", {}).get("value", {})
        return json.dumps(data, ensure_ascii=False, indent=2)

    # ─── Tabs ───

    @mcp.tool(name="browser_tabs", description="タブ一覧を取得する。tab_index を指定するとそのタブに切り替える")
    async def browser_tabs(tab_index: int = -1) -> str:
        port = _load_port()
        targets = await cdp.get_targets(port=port)
        tabs = [{"index": i, "title": t.get("title", ""), "url": t.get("url", "")} for i, t in enumerate(targets)]

        if tab_index >= 0:
            await cdp.switch_tab(port=port, tab_index=tab_index)
            return json.dumps({"switched_to": tab_index, "tabs": tabs}, ensure_ascii=False, indent=2)

        return json.dumps({"tabs": tabs}, ensure_ascii=False, indent=2)

    @mcp.tool(name="browser_new_tab", description="新しいタブを開く。url を指定すればそのページを開く")
    async def browser_new_tab(url: str = "about:blank") -> str:
        if block := await _ensure_connected():
            return block
        result = await cdp.send("Target.createTarget", {"url": url})
        target_id = result.get("targetId", "")
        return json.dumps({"new_tab": target_id, "url": url}, ensure_ascii=False)

    @mcp.tool(name="browser_close_tab", description="現在のタブを閉じる")
    async def browser_close_tab() -> str:
        if block := await _ensure_connected():
            return block
        targets = await cdp.get_targets(port=_load_port())
        if not targets:
            return json.dumps({"error": "no tabs found"})
        target_id = targets[0].get("id", "")
        await cdp.send("Target.closeTarget", {"targetId": target_id})
        return json.dumps({"closed": target_id})

    # ─── Forms ───

    @mcp.tool(name="browser_select_option", description="<select> 要素の option を選択する。CSS セレクタと value を指定")
    async def browser_select_option(selector: str, value: str) -> str:
        if block := await _ensure_connected():
            return block
        js = f"""
            (() => {{
                const el = document.querySelector({json.dumps(selector)});
                if (!el) return {{ error: 'element not found' }};
                el.value = {json.dumps(value)};
                el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                return {{ selected: el.value }};
            }})()
        """
        result = await cdp.send("Runtime.evaluate", {
            "expression": js,
            "returnByValue": True,
        })
        return json.dumps(result.get("result", {}).get("value", {}), ensure_ascii=False)

    @mcp.tool(name="browser_upload_file", description="ファイルをアップロードする。selector で <input type='file'> を指定し、file_path でローカルファイルパスを指定")
    async def browser_upload_file(selector: str, file_path: str) -> str:
        if block := await _ensure_connected():
            return block
        doc = await cdp.send("DOM.getDocument")
        root_id = doc["root"]["nodeId"]
        node = await cdp.send("DOM.querySelector", {
            "nodeId": root_id,
            "selector": selector,
        })
        node_id = node.get("nodeId", 0)
        if node_id == 0:
            return json.dumps({"error": f"element not found: {selector}"}, ensure_ascii=False)

        await cdp.send("DOM.setFileInputFiles", {
            "nodeId": node_id,
            "files": [file_path],
        })
        return json.dumps({"uploaded": file_path, "selector": selector}, ensure_ascii=False)

    # ─── Waiting ───

    @mcp.tool(name="browser_wait", description="ページの読み込み完了を待つ。timeout_sec で最大待機秒数を指定（デフォルト 10）")
    async def browser_wait(timeout_sec: int = 10) -> str:
        if block := await _ensure_connected():
            return block
        try:
            result = await cdp.send("Runtime.evaluate", {
                "expression": """
                    new Promise(resolve => {
                        if (document.readyState === 'complete') resolve('complete');
                        else window.addEventListener('load', () => resolve('complete'));
                    })
                """,
                "awaitPromise": True,
                "returnByValue": True,
            }, timeout=float(timeout_sec))
            state = result.get("result", {}).get("value", "unknown")
        except TimeoutError:
            state = "timeout"
        return json.dumps({"readyState": state})

    @mcp.tool(name="browser_wait_for_element", description="指定した CSS セレクタまたはテキストの要素が表示されるまで待つ。SPA やローディング待ちに使う")
    async def browser_wait_for_element(selector: str = "", text: str = "", timeout_sec: int = 10) -> str:
        if block := await _ensure_connected():
            return block
        if selector:
            js = f"""
                new Promise((resolve, reject) => {{
                    const sel = {json.dumps(selector)};
                    const el = document.querySelector(sel);
                    if (el) return resolve({{ found: true, tag: el.tagName.toLowerCase() }});
                    const observer = new MutationObserver(() => {{
                        const el = document.querySelector(sel);
                        if (el) {{
                            observer.disconnect();
                            resolve({{ found: true, tag: el.tagName.toLowerCase() }});
                        }}
                    }});
                    observer.observe(document.body, {{ childList: true, subtree: true }});
                    setTimeout(() => {{ observer.disconnect(); resolve({{ found: false }}); }}, {timeout_sec * 1000});
                }})
            """
        elif text:
            js = f"""
                new Promise((resolve, reject) => {{
                    const search = {json.dumps(text)}.toLowerCase();
                    if (document.body.innerText.toLowerCase().includes(search))
                        return resolve({{ found: true }});
                    const observer = new MutationObserver(() => {{
                        if (document.body.innerText.toLowerCase().includes(search)) {{
                            observer.disconnect();
                            resolve({{ found: true }});
                        }}
                    }});
                    observer.observe(document.body, {{ childList: true, subtree: true, characterData: true }});
                    setTimeout(() => {{ observer.disconnect(); resolve({{ found: false }}); }}, {timeout_sec * 1000});
                }})
            """
        else:
            return json.dumps({"error": "selector or text is required"}, ensure_ascii=False)

        result = await cdp.send("Runtime.evaluate", {
            "expression": js,
            "awaitPromise": True,
            "returnByValue": True,
        }, timeout=float(timeout_sec + 2))
        return json.dumps(result.get("result", {}).get("value", {}), ensure_ascii=False)

    # ─── Dialogs ───

    @mcp.tool(name="browser_handle_dialog", description="ダイアログ（alert, confirm, 'Leave site?' 等）を処理する。accept=true で OK/Leave、false で Cancel")
    async def browser_handle_dialog(accept: bool = True) -> str:
        await _ensure_connected(allow_dialog=True)
        await cdp.send("Page.handleJavaScriptDialog", {"accept": accept})
        cdp.pending_dialog = None
        return json.dumps({"dialog": "accepted" if accept else "dismissed"})
