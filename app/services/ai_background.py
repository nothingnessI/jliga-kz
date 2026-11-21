
"""
app/services/ai_background.py

Генерация и кеширование AI-фонов для карточек чемпионов J-LIGA.

Особенности:
- Асинхронная загрузка через httpx.
- Проверка полученного изображения.
- Преобразование PNG/WebP в JPEG.
- Атомарное сохранение кеша.
- Повторное использование готовых изображений.
- Логирование ошибок.

Публичный интерфейс:
    await get_ai_background(prompt_index=0)
"""

import asyncio
import hashlib
import io
import logging
import os
import tempfile
from pathlib import Path
from urllib.parse import quote

import httpx
from PIL import Image, UnidentifiedImageError


logger = logging.getLogger(__name__)

CACHE_DIR = (
    Path(__file__).resolve().parent.parent
    / "static"
    / "uploads"
    / "backgrounds"
)

PROMPTS = [
    (
        "epic football stadium interior at night, "
        "golden confetti falling from sky, "
        "dramatic floodlights, empty green pitch, "
        "cinematic atmosphere, ultra detailed "
        "sports photography, no people, no text, "
        "no logos, no watermarks"
    ),
    (
        "champions celebration atmosphere, "
        "golden trophy silhouette on pedestal, "
        "stadium lights, fireworks in dark sky, "
        "cinematic, no text, no logos, no people, "
        "professional sports photography"
    ),
    (
        "football stadium at dusk, dramatic lighting, "
        "smoke and light rays, empty field, "
        "cinematic wide shot, no text, no logos, "
        "no people, ultra detailed"
    ),
    (
        "dark navy blue sports background, "
        "single football ball silhouette, "
        "subtle geometric patterns, minimalistic, "
        "no text, no logos"
    ),
]

IMAGE_WIDTH = 1920
IMAGE_HEIGHT = 1280

MIN_IMAGE_BYTES = 5_000
MAX_IMAGE_BYTES = 15 * 1024 * 1024

REQUEST_TIMEOUT = 90
MAX_RETRIES = 2

# Ограничиваем число пикселей для защиты
# от чрезмерного потребления памяти.
MAX_IMAGE_PIXELS = 25_000_000


def _cache_key(prompt: str) -> str:
    """Создаёт стабильный идентификатор фона."""
    return hashlib.sha256(
        prompt.encode("utf-8")
    ).hexdigest()[:20]


def _public_url(filename: str) -> str:
    """Формирует относительный URL кешированного изображения."""
    return f"/static/uploads/backgrounds/{filename}"


def _validate_and_convert_image(data: bytes) -> bytes:
    """
    Проверяет изображение и возвращает JPEG.

    Поддерживает исходные JPEG, PNG и WebP.
    """

    if not MIN_IMAGE_BYTES <= len(data) <= MAX_IMAGE_BYTES:
        raise ValueError(
            f"Некорректный размер изображения: {len(data)} байт"
        )

    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in ("JPEG", "PNG", "WEBP"):
                raise ValueError(
                    f"Неподдерживаемый формат: {image.format}"
                )

            width, height = image.size

            if (
                width <= 0
                or height <= 0
                or width * height > MAX_IMAGE_PIXELS
            ):
                raise ValueError(
                    f"Недопустимое разрешение: {width}x{height}"
                )

            # Загружаем пиксели полностью, чтобы
            # обнаружить повреждённые изображения.
            image.load()

            if image.mode in ("RGBA", "LA"):
                rgba = image.convert("RGBA")

                background = Image.new(
                    "RGB",
                    rgba.size,
                    (15, 25, 40),
                )
                background.paste(
                    rgba,
                    mask=rgba.getchannel("A"),
                )
                converted = background

            else:
                converted = image.convert("RGB")

            output = io.BytesIO()

            converted.save(
                output,
                format="JPEG",
                quality=90,
                optimize=True,
            )

            result = output.getvalue()

            if not result.startswith(b"\xff\xd8\xff"):
                raise ValueError(
                    "Не удалось преобразовать изображение в JPEG"
                )

            return result

    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError(
            "AI-сервис вернул повреждённое изображение"
        ) from exc


def _is_valid_cache(path: Path) -> bool:
    """Проверяет существующий JPEG в кеше."""

    if not path.is_file():
        return False

    try:
        size = path.stat().st_size

        if not MIN_IMAGE_BYTES <= size <= MAX_IMAGE_BYTES:
            return False

        with Image.open(path) as image:
            if image.format != "JPEG":
                return False

            width, height = image.size

            if (
                width <= 0
                or height <= 0
                or width * height > MAX_IMAGE_PIXELS
            ):
                return False

            image.verify()

        return True

    except (OSError, ValueError, UnidentifiedImageError):
        return False


def _save_atomically(path: Path, data: bytes) -> None:
    """
    Сохраняет файл через временный файл.

    При ошибке основной кеш не повреждается.
    """

    fd, temporary_path = tempfile.mkstemp(
        prefix="jliga_bg_",
        suffix=".tmp",
        dir=str(path.parent),
    )

    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())

        os.replace(
            temporary_path,
            path,
        )

    finally:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)


async def _download_image(prompt: str) -> bytes:
    """Загружает изображение с повторными попытками."""

    url = (
        "https://image.pollinations.ai/prompt/"
        f"{quote(prompt, safe='')}"
        f"?width={IMAGE_WIDTH}"
        f"&height={IMAGE_HEIGHT}"
        "&nologo=true"
        "&enhance=true"
        "&model=flux"
    )

    timeout = httpx.Timeout(
        REQUEST_TIMEOUT,
        connect=15.0,
    )

    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
    ) as client:

        for attempt in range(MAX_RETRIES + 1):
            try:
                response = await client.get(url)
                response.raise_for_status()

                content_type = (
                    response.headers
                    .get("content-type", "")
                    .split(";")[0]
                    .strip()
                    .lower()
                )

                if content_type not in (
                    "image/jpeg",
                    "image/jpg",
                    "image/png",
                    "image/webp",
                    "application/octet-stream",
                ):
                    raise ValueError(
                        "AI-сервис вернул неожиданный "
                        f"тип содержимого: {content_type}"
                    )

                # Преобразуем в JPEG независимо
                # от формата исходного ответа.
                return await asyncio.to_thread(
                    _validate_and_convert_image,
                    response.content,
                )

            except (
                httpx.HTTPError,
                ValueError,
            ) as exc:
                logger.warning(
                    "Ошибка загрузки AI-фона, "
                    "попытка %s/%s: %s",
                    attempt + 1,
                    MAX_RETRIES + 1,
                    exc,
                )

                if attempt >= MAX_RETRIES:
                    raise RuntimeError(
                        "Не удалось получить корректный "
                        "AI-фон после нескольких попыток"
                    ) from exc

                await asyncio.sleep(
                    min(2 ** attempt, 5)
                )

    raise RuntimeError(
        "Не удалось загрузить AI-фон"
    )


async def get_ai_background(
    prompt_index: int = 0,
) -> str:
    """
    Возвращает URL фонового изображения.

    Если фон уже сохранён, использует кеш.
    Иначе загружает изображение и сохраняет JPEG.

    Пример результата:
        /static/uploads/backgrounds/abc123.jpg
    """

    if not isinstance(prompt_index, int):
        raise TypeError(
            "prompt_index должен быть целым числом"
        )

    prompt = PROMPTS[
        prompt_index % len(PROMPTS)
    ]

    key = _cache_key(prompt)
    filename = f"{key}.jpg"

    CACHE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    cached_path = CACHE_DIR / filename

    if await asyncio.to_thread(
        _is_valid_cache,
        cached_path,
    ):
        logger.debug(
            "AI-фон получен из кеша: %s",
            filename,
        )
        return _public_url(filename)

    logger.info(
        "Генерация AI-фона: вариант %s",
        prompt_index,
    )

    image_data = await _download_image(prompt)

    await asyncio.to_thread(
        _save_atomically,
        cached_path,
        image_data,
    )

    logger.info(
        "AI-фон сохранён: %s (%s байт)",
        filename,
        len(image_data),
    )

    return _public_url(filename)
