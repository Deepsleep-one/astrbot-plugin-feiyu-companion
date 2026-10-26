"""把本机 ComfyUI 接成陪伴端的出图端口。

三条现实约束，都写进 `status()`，不藏着：
1. **本机生成、云端对话**：ComfyUI 在 `D:\\ComfyUI`（127.0.0.1:8188），bot 在云容器里。
   没有隧道就是连不上——这一轮就把「连不上」如实报成 `integration_pending`。
2. **工作流由人给**：ComfyUI 的 UI 画布格式和 API 格式不是一回事；这里不猜节点，
   只读部署方放好的 API 格式 JSON（占位符 `{{prompt}}`/`{{seed}}`/`{{width}}`/`{{height}}`/`{{steps}}`）。
3. **只往本机发**：base_url 不是回环地址就直接拒绝，避免一句描述被发到别人的服务器上。

这里没有网络代码（`adapters/comfy_http.py` 负责），也没有任何凭证。
"""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

from ..ports.images import (
    DEFAULT_SIZE,
    DEFAULT_STEPS,
    DEFAULT_TIMEOUT,
    IMAGE_PORT_NAME,
    IMAGE_VERSION,
    ImageRequest,
    ImageResult,
    failed,
    is_loopback,
    is_private_address,
    is_allowed_target,
    prepare_prompt,
)

WORKFLOW_NAME = "image_workflow.json"
PLACEHOLDERS = ("prompt", "seed", "width", "height", "steps")
POLL_SECONDS = 0.5
POLL_MAX = 240

_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def image_enabled_from_config(cfg: dict) -> bool:
    value = (cfg or {}).get("image_enable", False)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def image_base_from_config(cfg: dict) -> str:
    return str((cfg or {}).get("image_base_url") or "http://127.0.0.1:8188").strip()


def image_allow_lan_from_config(cfg: dict) -> bool:
    value = (cfg or {}).get("image_allow_lan", False)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _substitute(value: Any, mapping: dict[str, Any]) -> Any:
    if isinstance(value, str):
        whole = _PLACEHOLDER.fullmatch(value.strip())
        if whole is not None and whole.group(1) in mapping:
            # 整串就是一个占位符：保留原类型（seed/width/steps 对 ComfyUI 是数字）
            return mapping[whole.group(1)]

        def repl(match: re.Match[str]) -> str:
            key = match.group(1)
            return str(mapping.get(key, match.group(0)))

        return _PLACEHOLDER.sub(repl, value)
    if isinstance(value, dict):
        return {key: _substitute(item, mapping) for key, item in value.items()}
    if isinstance(value, list):
        return [_substitute(item, mapping) for item in value]
    return value


class ComfyUIAdapter:
    """实现 `ports.images.ImagePort`。"""

    def __init__(
        self,
        *,
        base_url: str,
        enabled: bool = True,
        allow_lan: bool = False,
        workflow_path: Path | str | None = None,
        http: Any = None,
        state_dir: Path | str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        logger: Any = None,
    ) -> None:
        self.base_url = str(base_url or "").strip()
        self.enabled = bool(enabled)
        self.allow_lan = bool(allow_lan)
        self.workflow_path = Path(workflow_path) if workflow_path else None
        self.http = http
        self.state_dir = Path(state_dir) if state_dir else None
        self.timeout = float(timeout or DEFAULT_TIMEOUT)
        self.logger = logger
        self.calls = 0
        self.last_error = ""

    # ---- 状态 ---------------------------------------------------------
    def workflow(self) -> dict[str, Any] | None:
        path = self.workflow_path
        if path is None or not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - 坏文件当没有
            self.last_error = type(exc).__name__
            return None
        return raw if isinstance(raw, dict) and raw else None

    def status(self) -> tuple[str, str]:
        if not self.enabled:
            return "integration_pending", "出图总开关关着（image_enable=false）：只发文字"
        if not is_allowed_target(self.base_url, allow_lan=self.allow_lan):
            if is_private_address(self.base_url):
                return (
                    "integration_pending",
                    f"image_base_url={self.base_url or '（空）'} 是私网地址，但 image_allow_lan 没开："
                    "要经过本机隧道出图，得显式打开它",
                )
            return (
                "integration_pending",
                f"image_base_url={self.base_url or '（空）'} 既不是本机回环也不是私网：不往公网服务器发描述",
            )
        if self.workflow() is None:
            return (
                "integration_pending",
                f"还没配 API 格式的工作流（{WORKFLOW_NAME}）："
                "从 ComfyUI 里「导出 (API)」放进来就行",
            )
        if self.http is None:
            return "integration_pending", "没有 HTTP 通道（这一版没接真是正常的）"
        where = "本机" if is_loopback(self.base_url) else "经隧道到本机"
        return "ready", f"出图可用：{self.base_url}（{where}生成，工作流 {self.workflow_path.name if self.workflow_path else WORKFLOW_NAME}）"

    # ---- 出图 ---------------------------------------------------------
    def _api_prompt(self, request: ImageRequest, prompt: str) -> dict[str, Any]:
        workflow = self.workflow()
        if workflow is None:
            raise ValueError("没有工作流")
        width, _, height = str(request.size or DEFAULT_SIZE).partition("x")
        mapping = {
            "prompt": prompt,
            "seed": int(request.seed or 0),
            "width": int(width or 1024),
            "height": int(height or 1024),
            "steps": int(request.steps or DEFAULT_STEPS),
        }
        return _substitute(workflow, mapping)

    @staticmethod
    def _image_ref(history: dict[str, Any], prompt_id: str) -> dict[str, Any] | None:
        entry = (history or {}).get(prompt_id) or {}
        outputs = entry.get("outputs") or {}
        for node in outputs.values():
            for image in (node or {}).get("images") or []:
                if isinstance(image, dict) and image.get("filename"):
                    return image
        return None

    async def generate(self, request: ImageRequest) -> ImageResult:
        state, detail = self.status()
        if state != "ready":
            return failed(detail)
        body, why = prepare_prompt(getattr(request, "prompt", ""), max_chars=getattr(request, "max_chars", 300))
        if not body:
            return failed(why)
        started = time.monotonic()
        self.calls += 1
        client_id = uuid.uuid4().hex
        try:
            graph = self._api_prompt(request, body)
        except Exception as exc:  # noqa: BLE001 - 工作流坏了也说清楚
            self.last_error = type(exc).__name__
            return failed(f"工作流不能用：{type(exc).__name__}")
        try:
            submitted = await self.http.post_json("/prompt", {"prompt": graph, "client_id": client_id})
            prompt_id = str((submitted or {}).get("prompt_id") or "")
            if not prompt_id:
                return failed("ComfyUI 没返回 prompt_id")
            history: dict[str, Any] = {}
            for _ in range(POLL_MAX):
                history = await self.http.get_json(f"/history/{prompt_id}")
                if self._image_ref(history, prompt_id) is not None:
                    break
                await self._sleep(POLL_SECONDS)
                if time.monotonic() - started > self.timeout:
                    return failed(f"等了 {self.timeout:.0f} 秒还没出图")
            ref = self._image_ref(history, prompt_id)
            if ref is None:
                return failed("ComfyUI 说跑完了，但没有图")
            query = (
                f"/view?filename={ref.get('filename','')}"
                f"&subfolder={ref.get('subfolder','')}&type={ref.get('type','output')}"
            )
            data = await self.http.get_bytes(query)
            if not data:
                return failed("取回来的图是空的")
            path = self._save(data, prompt_id)
            return ImageResult(
                ok=True,
                path=str(path),
                elapsed_ms=int((time.monotonic() - started) * 1000),
                prompt_id=prompt_id,
            )
        except Exception as exc:  # noqa: BLE001 - 连不上/超时/坏响应都只退回文字
            self.last_error = type(exc).__name__
            if self.logger is not None:
                self.logger.info("[feiyu_companion] 出图没成（%s）", type(exc).__name__)
            return failed(f"ComfyUI 报错：{type(exc).__name__}")

    async def _sleep(self, seconds: float) -> None:
        import asyncio

        await asyncio.sleep(seconds)

    def _save(self, data: bytes, prompt_id: str) -> Path:
        base = self.state_dir or Path.cwd()
        target = base / "images"
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"feiyu_{prompt_id[:12]}.png"
        path.write_bytes(data)
        return path

    def info(self) -> dict[str, Any]:
        state, detail = self.status()
        return {
            "port": IMAGE_PORT_NAME,
            "version": IMAGE_VERSION,
            "state": state,
            "detail": detail,
            "enabled": self.enabled,
            "base_url": self.base_url,
            "calls": self.calls,
            "last_error": self.last_error,
        }


__all__ = [
    "WORKFLOW_NAME",
    "PLACEHOLDERS",
    "ComfyUIAdapter",
    "image_enabled_from_config",
    "image_base_from_config",
    "image_allow_lan_from_config",
]
