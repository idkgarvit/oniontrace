"""Minimal Chrome DevTools Protocol client for driving the console on the bot screen.

Written because the interesting failures in a local web app are not in the HTTP status
code: they are a handler that throws in `graph.js` after the DOM is painted, a panel that
overflows its column, a muted colour that fails contrast, or a form that posts but never
tells the user it worked. Those need a real browser and a real event loop.
"""

from __future__ import annotations

import asyncio
import json
import urllib.request

import websockets


def pages(port: int = 9222) -> list[dict]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json") as fh:
        return [t for t in json.load(fh) if t.get("type") == "page"]


class CDP:
    def __init__(self, ws_url: str, target_url: str | None = None, port: int = 9222):
        # ws_url is the debugger socket. target_url is the page to load. Keeping them in
        # one field is how a driver ends up navigating the tab to its own socket.
        self.ws_url = ws_url
        self.target_url = target_url
        self.port = port
        self.ws = None
        self._id = 0
        self.events: list[dict] = []

    async def __aenter__(self) -> "CDP":
        self.ws = await websockets.connect(  # type: ignore[call-arg]
            self.ws_url, max_size=60 * 1024 * 1024
        )
        for domain in ("Page", "Runtime", "Log", "Console", "Network"):
            try:
                await self.send(f"{domain}.enable")
            except Exception:
                pass
        if self.target_url:
            await self.send("Page.navigate", {"url": self.target_url})
            await asyncio.sleep(1.6)
        return self

    async def __aexit__(self, *exc) -> None:
        if self.ws:
            await self.ws.close()

    async def send(self, method: str, params: dict | None = None, **kw):
        params = {**(params or {}), **kw}
        ws = self.ws
        assert ws is not None, "send() outside the context manager"
        self._id += 1
        mid = self._id
        await ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            raw = json.loads(await ws.recv())
            if raw.get("id") == mid:
                if "error" in raw:
                    raise RuntimeError(f"{method}: {raw['error']}")
                return raw.get("result", {})
            if "method" in raw:
                self.events.append(raw)

    async def drain(self, seconds: float = 0.5) -> None:
        """Collect events that arrive after an action without blocking on a reply."""

        ws = self.ws
        assert ws is not None, "drain() outside the context manager"
        end = asyncio.get_event_loop().time() + seconds
        while True:
            left = end - asyncio.get_event_loop().time()
            if left <= 0:
                return
            try:
                raw = json.loads(await asyncio.wait_for(ws.recv(), timeout=left))
            except asyncio.TimeoutError:
                return
            except websockets.ConnectionClosed:
                return
            if "method" in raw:
                self.events.append(raw)

    async def js(self, expression: str):
        res = await self.send(
            "Runtime.evaluate",
            expression=expression,
            returnByValue=True,
            awaitPromise=True,
            userGesture=True,
        )
        if "exceptionDetails" in res:
            raise RuntimeError(f"JS threw: {res['exceptionDetails'].get('text')}")
        return res.get("result", {}).get("value")

    async def goto(self, url: str, settle: float = 1.5) -> None:
        await self.send("Page.navigate", {"url": url})
        await asyncio.sleep(settle)

    def problems(self) -> list[str]:
        """Console errors, thrown exceptions and failed requests seen so far."""

        out = []
        for ev in self.events:
            m = ev.get("method")
            p = ev.get("params", {})
            if m == "Runtime.exceptionThrown":
                d = p.get("exceptionDetails", {})
                text = d.get("text", "")
                desc = (d.get("exception") or {}).get("description", "")
                out.append(f"JS EXCEPTION: {text} {desc}".strip()[:400])
            elif m == "Runtime.consoleAPICalled" and p.get("type") in ("error", "warning"):
                args = " ".join(
                    str(a.get("value", a.get("description", ""))) for a in p.get("args", [])
                )
                out.append(f"console.{p['type']}: {args}"[:400])
            elif m == "Log.entryAdded":
                e = p.get("entry", {})
                if e.get("level") in ("error", "warning"):
                    out.append(f"log.{e['level']}: {e.get('text','')}"[:400])
            elif m == "Network.loadingFailed":
                out.append(f"request failed: {p.get('errorText')} ({p.get('type')})")
        return out

    async def screenshot(self, path: str) -> int:
        res = await self.send("Page.captureScreenshot", format="png", captureBeyondViewport=False)
        import base64

        blob = base64.b64decode(res["data"])
        with open(path, "wb") as fh:
            fh.write(blob)
        return len(blob)


async def first_page(port: int = 9222, target_url: str | None = None) -> CDP:
    ps = pages(port)
    if not ps:
        raise SystemExit("no page target on the debugging port")
    # Prefer a real http tab over the devtools/blank targets.
    ps.sort(key=lambda t: 0 if t.get("url", "").startswith("http") else 1)
    return CDP(ps[0]["webSocketDebuggerUrl"], target_url, port)
