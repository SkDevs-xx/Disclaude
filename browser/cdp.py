"""CDP WebSocket client — Chrome DevTools Protocol に直接接続する。"""

import asyncio
import json
import logging
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)


class CDPClient:

    def __init__(self):
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._next_id: int = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: Optional[asyncio.Task] = None
        self.pending_dialog: Optional[dict] = None
        self._send_lock: asyncio.Lock = asyncio.Lock()

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    async def connect(self, port: int = 9222, tab_index: int = 0) -> None:
        """Chrome の CDP に接続する。"""
        self._session = aiohttp.ClientSession()

        # タブ一覧を取得
        async with self._session.get(f"http://127.0.0.1:{port}/json") as resp:
            targets = await resp.json()

        # page タイプのタブをフィルタ
        pages = [t for t in targets if t.get("type") == "page"]
        if not pages:
            raise ConnectionError("No page targets found in Chrome")

        if tab_index >= len(pages):
            tab_index = 0

        ws_url = pages[tab_index]["webSocketDebuggerUrl"]
        self._ws = await self._session.ws_connect(ws_url)
        self._reader_task = asyncio.create_task(self._read_loop())
        await self.send("Page.enable")
        logger.info("Connected to Chrome CDP: %s", ws_url)

    async def disconnect(self) -> None:
        """接続を閉じる。"""
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass

        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("CDP disconnected"))
        self._pending.clear()

        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()

        self._ws = None
        self._session = None

    async def send(self, method: str, params: dict | None = None, timeout: float = 30.0) -> Any:
        """CDP コマンドを送信して結果を返す。"""
        if not self.is_connected:
            raise ConnectionError("Not connected to Chrome CDP")

        async with self._send_lock:
            cmd_id = self._next_id
            self._next_id += 1

            msg = {"id": cmd_id, "method": method}
            if params:
                msg["params"] = params

            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self._pending[cmd_id] = fut

            await self._ws.send_json(msg)

        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(cmd_id, None)
            raise TimeoutError(f"CDP command {method} timed out after {timeout}s")

    async def get_targets(self, port: int = 9222) -> list[dict]:
        """Chrome のタブ一覧を取得する（接続不要）。"""
        session = self._session
        if session is None or session.closed:
            session = aiohttp.ClientSession()
            async with session:
                async with session.get(f"http://127.0.0.1:{port}/json") as resp:
                    targets = await resp.json()
        else:
            async with session.get(f"http://127.0.0.1:{port}/json") as resp:
                targets = await resp.json()
        return [t for t in targets if t.get("type") == "page"]

    async def switch_tab(self, port: int, tab_index: int) -> None:
        """別のタブに切り替える（再接続）。"""
        await self.disconnect()
        await self.connect(port=port, tab_index=tab_index)

    async def _handle_dialog_event(self, params: dict) -> None:
        """ダイアログイベントを処理する。alertのみ自動OK、それ以外は保留してClaudeに委ねる。"""
        dialog_type = params.get("type", "")
        message = params.get("message", "")
        url = params.get("url", "")

        try:
            if dialog_type == "alert":
                # alert は OK しかないので自動処理
                logger.info("Auto-accepting alert: %s", message)
                await self.send("Page.handleJavaScriptDialog", {"accept": True})
            else:
                # それ以外は保留 → 次のMCPツール呼び出し時にClaudeへ通知
                logger.info("Dialog pending for Claude: type=%s url=%s message=%s", dialog_type, url, message)
                self.pending_dialog = {
                    "type": dialog_type,
                    "url": url,
                    "message": message,
                }
        except Exception:
            logger.exception("Failed to handle %s dialog", dialog_type)

    async def _read_loop(self) -> None:
        """WebSocket メッセージを読み続ける。"""
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    msg_id = data.get("id")
                    if msg_id is not None and msg_id in self._pending:
                        fut = self._pending.pop(msg_id)
                        if not fut.done():
                            if "error" in data:
                                fut.set_exception(
                                    RuntimeError(data["error"].get("message", str(data["error"])))
                                )
                            else:
                                fut.set_result(data.get("result", {}))
                    elif data.get("method") == "Page.javascriptDialogOpening":
                        asyncio.create_task(self._handle_dialog_event(data.get("params", {})))
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                    break
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("CDP read loop error")
        finally:
            # WebSocket が切れたら状態をリセットして次回の _ensure_connected で再接続させる
            logger.warning("CDP read loop ended, marking connection as closed")
            self._ws = None
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("CDP connection lost"))
            self._pending.clear()
