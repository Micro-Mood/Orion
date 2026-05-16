"""写入文件（不存在则创建）"""
from __future__ import annotations

from typing import Any

from .. import ToolDef, param, STR
from ...handlers.base import RequestContext

tool = ToolDef(
    name="write_file",
    description="写入文件内容，文件不存在则自动创建（含父目录）",
    lock="write",
    is_write=True,
    params=[
        param("path", STR, non_empty=True),
        param("content", STR),
        param("encoding", STR, required=False, default="utf-8"),
    ],
)


async def execute(handler: Any, ctx: RequestContext, **kwargs: Any) -> dict[str, Any]:
    return await handler.write_file(ctx, **kwargs)
