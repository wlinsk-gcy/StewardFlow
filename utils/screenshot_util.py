import asyncio
import base64
from pathlib import Path

from core.protocol import Event, EventType

async def wait_and_emit_screenshot_event(
    ws_manager,
    *,
    client_id: str,
    agent_id: str,
    turn_id: str,
    img_path: str | Path,
    timeout_s: float = 15.0,
    poll_interval_s: float = 0.05,
    mime: str = "image/png",
    delete_after_send: bool = False,
) -> bool:

    p = Path(img_path)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s

    last_size = -1
    stable_hits = 0

    while loop.time() < deadline:
        try:
            if p.is_file():
                size = p.stat().st_size
                if size > 0 and size == last_size:
                    stable_hits += 1
                    if stable_hits >= 2:
                        img_bytes = p.read_bytes()
                        b64 = base64.b64encode(img_bytes).decode("ascii")
                        data_url = f"data:{mime};base64,{b64}"

                        event = Event(
                            EventType.SCREENSHOT,
                            agent_id,
                            turn_id,
                            {
                                "mime": mime,
                                "path": str(p),
                                "size": len(img_bytes),
                                "content": data_url,
                            },
                        )
                        await ws_manager.send(event.to_dict(), client_id=client_id)

                        if delete_after_send:
                            try:
                                p.unlink()
                            except FileNotFoundError:
                                pass
                        return True
                else:
                    stable_hits = 0
                    last_size = size
        except FileNotFoundError:
            pass

        await asyncio.sleep(poll_interval_s)

    return False



def clean_screenshot():
    p = Path(".screenshots")
    try:
        if p.is_dir():
            for item in p.iterdir():
                try:
                    if item.is_file():
                        item.unlink()
                except FileNotFoundError:
                    pass
    except FileNotFoundError:
        pass  # Possibly deleted by another thread/process.
