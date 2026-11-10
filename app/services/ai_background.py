"""
Генерация AI-фона для карточек чемпионов.
Использует Pollinations.ai (бесплатно, без API-ключей).
Кешируется по хешу промпта — повторный вызов мгновенный.
"""
import hashlib
from pathlib import Path
from urllib.parse import quote

import httpx


CACHE_DIR = Path(__file__).resolve().parent.parent / "static" / "uploads" / "backgrounds"


# Несколько вариантов фона — чтобы можно было выбирать
PROMPTS = [
    # 0 — классика: ночной стадион, конфетти
    "epic football stadium interior at night, golden confetti falling from sky, dramatic floodlights, empty green pitch, cinematic atmosphere, ultra detailed sports photography, no people, no text, no logos, no watermarks",
    # 1 — праздник: кубок, фейерверки
    "champions celebration atmosphere, golden trophy silhouette on pedestal, stadium lights, fireworks in dark sky, cinematic, no text, no logos, no people, professional sports photography",
    # 2 — драматика: прожекторы и дым
    "football stadium at dusk, dramatic lighting, smoke and light rays, empty field, cinematic wide shot, no text, no logos, no people, ultra detailed",
    # 3 — минимализм: тёмно-синий градиент + мяч
    "dark navy blue sports background, single football ball silhouette, subtle geometric patterns, minimalistic, no text, no logos",
]


async def get_ai_background(prompt_index: int = 0) -> str:
    """
    Возвращает относительный URL фона: /static/uploads/backgrounds/<hash>.jpg
    При первом вызове скачивает у Pollinations (~5 сек), далее — из кеша.
    """
    prompt = PROMPTS[prompt_index % len(PROMPTS)]
    cache_key = hashlib.md5(prompt.encode()).hexdigest()[:16]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = CACHE_DIR / f"{cache_key}.jpg"

    # Кеш: если файл существует и весит больше 10 КБ — отдаём
    if cached.exists() and cached.stat().st_size > 10_000:
        return f"/static/uploads/backgrounds/{cache_key}.jpg"

    # Генерируем через Pollinations (бесплатно, без ключей)
    url = (
        f"https://image.pollinations.ai/prompt/{quote(prompt)}"
        f"?width=1920&height=1280&nologo=true&enhance=true&model=flux"
    )

    async with httpx.AsyncClient(timeout=90) as client:
        r = await client.get(url, follow_redirects=True)
        r.raise_for_status()
        if len(r.content) < 5_000:
            raise RuntimeError(f"Pollinations вернул слишком маленькую картинку ({len(r.content)} байт)")
        cached.write_bytes(r.content)

    return f"/static/uploads/backgrounds/{cache_key}.jpg"