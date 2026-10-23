"""Immutable image attachments carried with a prompt, including queued replies."""
import asyncio
import base64
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

MAX_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class Image:
    name: str
    mime: str
    data: bytes

    @classmethod
    def from_bytes(cls, name, data):
        if len(data) > MAX_BYTES:
            raise ValueError("image exceeds 20 MiB")
        mime = ("image/png" if data.startswith(b"\x89PNG\r\n\x1a\n") else
                "image/jpeg" if data.startswith(b"\xff\xd8\xff") else
                "image/gif" if data.startswith((b"GIF87a", b"GIF89a")) else
                "image/webp" if data[:4] == b"RIFF" and data[8:12] == b"WEBP" else None)
        if not mime:
            raise ValueError("expected a PNG, JPEG, GIF or WebP image")
        return cls(name, mime, data)

    @classmethod
    def from_path(cls, path):
        p = Path(path).expanduser()
        with p.open("rb") as f:
            return cls.from_bytes(p.name, f.read(MAX_BYTES + 1))

    def encoded(self):
        return base64.b64encode(self.data).decode("ascii")

    def acp(self):
        return {"type": "image", "mimeType": self.mime, "data": self.encoded()}

    def claude(self):
        return {"type": "image", "source": {"type": "base64", "media_type": self.mime, "data": self.encoded()}}

    def codex(self):
        return {"type": "image", "url": f"data:{self.mime};base64,{self.encoded()}"}


class Prompt(str):
    def __new__(cls, text, images=()):
        obj = super().__new__(cls, text)
        obj.images = tuple(images)
        return obj

    @classmethod
    def join_prompts(cls, prompts):
        return cls("\n\n".join(prompts), [i for p in prompts for i in getattr(p, "images", ())])


async def _read(*args):
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                               stderr=asyncio.subprocess.DEVNULL)

    async def until_eof() -> bytes:
        # A single read() returns as soon as *some* data is buffered, which can be a partial
        # clipboard (wl-paste and xclip write in chunks). Read until EOF, but never buffer more
        # than one byte past the limit, so a runaway clipboard is refused without eating memory.
        chunks: list[bytes] = []
        size = 0
        while size <= MAX_BYTES:
            chunk = await proc.stdout.read(min(1 << 16, MAX_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        return b"".join(chunks)

    try:
        data = await asyncio.wait_for(until_eof(), 5)
        if len(data) > MAX_BYTES:
            raise ValueError("clipboard exceeds 20 MiB")
        await asyncio.wait_for(proc.wait(), 5)
        if proc.returncode:
            raise ValueError("could not read the clipboard")
        return data
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def clipboard():
    """Return an Image or text; never run clipboard contents as a command."""
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-paste"):
        types = (await _read("wl-paste", "--list-types")).decode().splitlines()
        mime = next((t for t in ("image/png", "image/jpeg", "image/webp", "image/gif") if t in types), None)
        if mime:
            return Image.from_bytes("clipboard." + mime.split("/")[1], await _read("wl-paste", "--type", mime))
        return (await _read("wl-paste", "--no-newline")).decode()
    if os.environ.get("DISPLAY") and shutil.which("xclip"):
        types = (await _read("xclip", "-selection", "clipboard", "-o", "-t", "TARGETS")).decode().splitlines()
        mime = next((t for t in ("image/png", "image/jpeg", "image/webp", "image/gif") if t in types), None)
        if mime:
            return Image.from_bytes("clipboard." + mime.split("/")[1], await _read("xclip", "-selection", "clipboard", "-o", "-t", mime))
        return (await _read("xclip", "-selection", "clipboard", "-o")).decode()
    raise ValueError("clipboard images need wl-paste (Wayland) or xclip (X11); use /image <path> instead")
