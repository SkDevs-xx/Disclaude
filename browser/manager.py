"""Xtigervnc + Chrome + noVNC のプロセスライフサイクルを管理する。"""

import asyncio
import logging
import os
import shutil

import aiohttp

logger = logging.getLogger(__name__)

DEFAULT_PROFILE_DIR = os.path.expanduser("~/.config/clive-chrome")
VNC_PASSWD_FILE = os.path.expanduser("~/.vnc/passwd")


class BrowserManager:

    def __init__(self, cdp_port: int = 9222, vnc_port: int = 5900, novnc_port: int = 6080, novnc_bind: str = "localhost", profile_dir: str | None = None, display: str = ":99"):
        self.cdp_port = cdp_port
        self.vnc_port = vnc_port
        self.novnc_port = novnc_port
        self.novnc_bind = novnc_bind
        self._xvnc_proc: asyncio.subprocess.Process | None = None
        self._chrome_proc: asyncio.subprocess.Process | None = None
        self._novnc_proc: asyncio.subprocess.Process | None = None
        self._watcher: asyncio.Task | None = None
        self._display = display
        self._profile_dir = profile_dir or DEFAULT_PROFILE_DIR
        self._http_session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        """Xtigervnc（仮想ディスプレイ+VNC）、Chrome、noVNC を起動する。"""
        # CDP ポートが既に応答するなら Chrome は起動済み
        if await self._cdp_is_alive():
            logger.info("Chrome CDP already responding on port %d, skipping launch", self.cdp_port)
            return

        # Xtigervnc を起動（ディスプレイのロックファイルがなければ）
        display_num = self._display.lstrip(":")
        lock_file = f"/tmp/.X{display_num}-lock"
        sock_file = f"/tmp/.X11-unix/X{display_num}"

        # Clean up stale locks if process isn't running
        if os.path.exists(lock_file):
            proc_running = False
            try:
                with open(lock_file, "r") as f:
                    pid = int(f.read().strip())
                os.kill(pid, 0)
                proc_running = True
            except Exception:
                pass
            
            if not proc_running:
                logger.info("Cleaning up stale X11 locks for %s", self._display)
                try: os.unlink(lock_file)
                except OSError: pass
                try: os.unlink(sock_file)
                except OSError: pass

        if not os.path.exists(lock_file):
            if not shutil.which("Xtigervnc"):
                logger.warning("Xtigervnc not found — install with: sudo apt install tigervnc-standalone-server")
                return
            if not os.path.exists(VNC_PASSWD_FILE):
                logger.warning("VNC password not set — run: vncpasswd")
                return
            self._xvnc_proc = await asyncio.create_subprocess_exec(
                "Xtigervnc", self._display,
                "-geometry", "1920x1080",
                "-depth", "24",
                "-rfbport", str(self.vnc_port),
                "-SecurityTypes", "VncAuth",
                "-PasswordFile", VNC_PASSWD_FILE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                preexec_fn=os.setsid,
            )
            await asyncio.sleep(1)
            logger.info("Xtigervnc started on %s, VNC port %d (pid=%d)",
                        self._display, self.vnc_port, self._xvnc_proc.pid)
        else:
            logger.info("Xtigervnc already running on %s, reusing", self._display)

        # Chrome を起動
        if not await self._start_chrome():
            return

        # noVNC プロキシを起動（ブラウザから VNC に接続可能にする）
        await self._start_novnc()

        # Chrome プロセス監視を開始
        self._watcher = asyncio.create_task(self._watch_chrome())

    async def _start_chrome(self) -> bool:
        """Chrome を起動して CDP が応答するまで待機する。成功したら True を返す。"""
        chrome_bin = shutil.which("google-chrome") or shutil.which("chromium-browser")
        if not chrome_bin:
            logger.warning("Chrome not found — install with: sudo apt install google-chrome-stable")
            return False

        chrome_env = dict(os.environ)
        chrome_env["DISPLAY"] = self._display
        self._chrome_proc = await asyncio.create_subprocess_exec(
            chrome_bin,
            f"--remote-debugging-port={self.cdp_port}",
            f"--user-data-dir={self._profile_dir}",
            "--no-first-run",
            "--disable-background-timer-throttling",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-extensions",
            "--no-sandbox",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=chrome_env,
            preexec_fn=os.setsid,
        )
        logger.info("Chrome started with CDP port %d (pid=%d)", self.cdp_port, self._chrome_proc.pid)

        for _ in range(20):
            await asyncio.sleep(0.5)
            if await self._cdp_is_alive():
                logger.info("Chrome CDP ready on port %d", self.cdp_port)
                return True

        logger.warning("Chrome started but CDP not responding on port %d", self.cdp_port)
        return True  # プロセスは起動済みなので監視対象にする

    async def _watch_chrome(self) -> None:
        """Chrome プロセスを監視し、落ちたら再起動する（指数バックオフ付き）。"""
        backoff = 5
        while True:
            await asyncio.sleep(backoff)
            if self._chrome_proc and self._chrome_proc.returncode is not None:
                logger.warning("Chrome crashed (code=%s), restarting in %ds...",
                              self._chrome_proc.returncode, backoff)
                try:
                    if await self._start_chrome():
                        backoff = 5  # 成功したらリセット
                    else:
                        backoff = min(backoff * 2, 300)  # 失敗したら倍に（最大5分）
                except Exception as e:
                    logger.error("Error during Chrome restart: %s", e)
                    backoff = min(backoff * 2, 300)

    async def _start_novnc(self) -> None:
        """noVNC プロキシを起動する。"""
        novnc_proxy = "/usr/share/novnc/utils/novnc_proxy"
        if not os.path.exists(novnc_proxy):
            logger.warning("noVNC not found — install with: sudo apt install novnc")
            return

        self._novnc_proc = await asyncio.create_subprocess_exec(
            novnc_proxy,
            "--vnc", f"localhost:{self.vnc_port}",
            "--listen", f"{self.novnc_bind}:{self.novnc_port}",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            preexec_fn=os.setsid,
        )
        logger.info("noVNC started on port %d (pid=%d)", self.novnc_port, self._novnc_proc.pid)

    async def stop(self) -> None:
        """監視タスク → noVNC → Chrome → Xtigervnc の順で停止する。"""
        if self._watcher and not self._watcher.done():
            self._watcher.cancel()
            try:
                await self._watcher
            except asyncio.CancelledError:
                pass
            self._watcher = None

        import signal
        for name, proc in [
            ("noVNC", self._novnc_proc),
            ("Chrome", self._chrome_proc),
            ("Xtigervnc", self._xvnc_proc),
        ]:
            if proc and proc.returncode is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                logger.info("%s stopped (pid=%d)", name, proc.pid)
        self._novnc_proc = None
        self._chrome_proc = None
        self._xvnc_proc = None

        if self._http_session is not None and not self._http_session.closed:
            await self._http_session.close()
            self._http_session = None

    async def _cdp_is_alive(self) -> bool:
        """CDP ポートが応答するか確認する。"""
        try:
            if self._http_session is None or self._http_session.closed:
                self._http_session = aiohttp.ClientSession()
            async with self._http_session.get(
                f"http://127.0.0.1:{self.cdp_port}/json",
                timeout=aiohttp.ClientTimeout(total=2),
            ) as resp:
                return resp.status == 200
        except Exception:
            return False
