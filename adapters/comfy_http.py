"""真正的 HTTP 客户端（只在真跑的时候用；测试里换成假的，不碰网络）。

单独放一个文件是为了让 `comfyui.py` 保持「没有网络代码」：那个文件里只有协议与状态判断，
静态检查才盯得住「出图端口不会自己乱连」。
"""

from __future__ import annotations

import json
from typing import Any


class ComfyHTTP:
    """ComfyUI 的三个接口：提交、查历史、取图。"""

    def __init__(self, base_url: str, *, timeout: float = 120.0) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        self.timeout = float(timeout or 120.0)

    async def _session(self):
        import aiohttp  # 延迟导入：测试环境不需要它

        return aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.timeout, connect=5),
            trust_env=False,
        )

    async def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with await self._session() as session:
            async with session.post(f"{self.base_url}{path}", json=payload) as response:
                text = await response.text()
                if response.status != 200:
                    raise RuntimeError(f"ComfyUI {path} 返回 {response.status}")
                return json.loads(text or "{}")

    async def get_json(self, path: str) -> dict[str, Any]:
        async with await self._session() as session:
            async with session.get(f"{self.base_url}{path}") as response:
                text = await response.text()
                if response.status != 200:
                    raise RuntimeError(f"ComfyUI {path} 返回 {response.status}")
                return json.loads(text or "{}")

    async def get_bytes(self, path: str) -> bytes:
        async with await self._session() as session:
            async with session.get(f"{self.base_url}{path}") as response:
                if response.status != 200:
                    raise RuntimeError(f"ComfyUI {path} 返回 {response.status}")
                return await response.read()


__all__ = ["ComfyHTTP"]
