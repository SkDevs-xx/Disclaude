"""
添付ファイル処理
"""

import logging
import uuid
from pathlib import Path

import aiofiles
import aiohttp

import core.config as _cfg

MAX_ATTACHMENT_SIZE = 10 * 1024 * 1024  # 10 MB

try:
    from pdfminer.high_level import extract_text as pdf_extract_text
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False

def _logger() -> logging.Logger:
    return _cfg._logger()


_http_session: aiohttp.ClientSession | None = None


def _get_http_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession()
    return _http_session


async def close_http_session() -> None:
    """グローバル HTTP セッションを閉じる。Bot 停止時に呼ぶ。"""
    global _http_session
    if _http_session is not None and not _http_session.closed:
        await _http_session.close()
        _http_session = None


TEXT_EXTENSIONS = {".txt", ".csv", ".md", ".py", ".js", ".ts", ".json", ".yaml", ".yml",
                   ".toml", ".ini", ".cfg", ".sh", ".html", ".css", ".xml", ".log"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
PDF_EXTENSION = ".pdf"


async def process_attachment(attachment) -> tuple[str | None, Path | None]:
    """
    Returns (text_to_inject, image_path)
    text_to_inject: プロンプトに追記するテキスト
    image_path: --image に渡す画像ファイルパス
    """
    ext = Path(attachment.filename).suffix.lower()

    # サイズ制限（OOM 防止）
    if hasattr(attachment, "size") and attachment.size and attachment.size > MAX_ATTACHMENT_SIZE:
        mb = attachment.size / (1024 * 1024)
        return f"\n\n（添付ファイル: {attachment.filename} — サイズ超過: {mb:.1f} MB、上限 10 MB）\n", None

    _cfg.ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
    # ファイル名衝突防止のため UUID プレフィックスを付与
    safe_name = f"{uuid.uuid4().hex[:8]}_{attachment.filename}"
    save_path = _cfg.ATTACHMENTS_DIR / safe_name

    session = _get_http_session()
    async with session.get(attachment.url) as resp:
        if resp.status != 200:
            _logger().warning("Discord file download failed: status %s", resp.status)
            return f"\n\n（添付ファイル: {attachment.filename} — ダウンロード失敗: {resp.status}）\n", None
            
        cl = resp.headers.get("Content-Length")
        if cl and int(cl) > MAX_ATTACHMENT_SIZE:
            mb = int(cl) / (1024 * 1024)
            return f"\n\n（添付ファイル: {attachment.filename} — サイズ超過: {mb:.1f} MB、上限 10 MB）\n", None
            
        downloaded = 0
        async with aiofiles.open(save_path, "wb") as f:
            async for chunk in resp.content.iter_chunked(1024 * 1024):
                downloaded += len(chunk)
                if downloaded > MAX_ATTACHMENT_SIZE:
                    save_path.unlink(missing_ok=True)
                    return f"\n\n（添付ファイル: {attachment.filename} — ダウンロード中のサイズ超過）\n", None
                await f.write(chunk)

    try:
        if ext in TEXT_EXTENSIONS:
            import asyncio
            content = await asyncio.to_thread(save_path.read_text, encoding="utf-8", errors="replace")
            save_path.unlink(missing_ok=True)
            return f"\n\n--- 添付ファイル: {attachment.filename} ---\n{content[:4000]}\n---\n", None

        elif ext in IMAGE_EXTENSIONS:
            # attachments/ に保存されたファイルを削除し、workspace/tmp/ に保存
            _cfg.TMP_DIR.mkdir(parents=True, exist_ok=True)
            tmp_path = _cfg.TMP_DIR / safe_name
            # ファイルをリネーム（移動）する。Windows対応のため shutil.move を検討するか、同一ファイルシステム前提なら rename で良い
            save_path.rename(tmp_path)
            abs_path = tmp_path.resolve()
            text = (
                f"\n\n--- 添付画像: {attachment.filename} ---\n"
                f"画像ファイルのパス: {abs_path}\n"
                f"このファイルを Read ツールで読み取り、画像の内容を分析・説明してください。\n"
                f"---\n"
            )
            return text, tmp_path

        elif ext == PDF_EXTENSION and PDF_AVAILABLE:
            import asyncio
            text = await asyncio.to_thread(pdf_extract_text, str(save_path))
            save_path.unlink(missing_ok=True)
            return f"\n\n--- PDF: {attachment.filename} ---\n{text[:4000]}\n---\n", None

        else:
            save_path.unlink(missing_ok=True)
            return f"\n\n（添付ファイル: {attachment.filename}、種別: {ext or '不明'}）\n", None
    except Exception as e:
        _logger().exception("Attachment processing error: %s", e)
        save_path.unlink(missing_ok=True)
        return f"\n\n（添付ファイル処理エラー: {attachment.filename}）\n", None
