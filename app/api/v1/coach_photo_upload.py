
"""Загрузка фотографий тренеров и игроков J-LIGA."""

import asyncio
import io
import os
import uuid
import warnings

from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    UploadFile,
    status,
)
from PIL import Image, ImageOps, UnidentifiedImageError

from app.api.v1.coach import get_current_coach
from app.models.user import User


router = APIRouter(
    prefix="/api/v1/coach",
    tags=["Coach Photos"],
)

STATIC_ROOT = (
    Path(__file__).resolve().parents[2]
    / "static"
)

UPLOAD_DIR = (
    STATIC_ROOT
    / "uploads"
    / "coach-applications"
)

MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
MAX_SIDE = 1800

ALLOWED_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
}


def process_and_save_photo(content: bytes) -> str:
    filename = f"{uuid.uuid4().hex}.jpg"

    destination = UPLOAD_DIR / filename
    temporary = UPLOAD_DIR / f".{filename}.tmp"

    UPLOAD_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    try:
        with warnings.catch_warnings():
            warnings.simplefilter(
                "error",
                Image.DecompressionBombWarning,
            )

            with Image.open(
                io.BytesIO(content)
            ) as picture:

                if (
                    picture.width * picture.height
                    > MAX_IMAGE_PIXELS
                ):
                    raise ValueError(
                        "Слишком большое разрешение"
                    )

                if getattr(
                    picture,
                    "n_frames",
                    1,
                ) != 1:
                    raise ValueError(
                        "Анимированные изображения не поддерживаются"
                    )

                picture.load()

                oriented = ImageOps.exif_transpose(
                    picture
                )

                oriented.thumbnail(
                    (MAX_SIDE, MAX_SIDE),
                    Image.Resampling.LANCZOS,
                )

                if (
                    oriented.mode in ("RGBA", "LA")
                    or "transparency" in oriented.info
                ):
                    rgba = oriented.convert("RGBA")

                    rgb = Image.new(
                        "RGB",
                        rgba.size,
                        (255, 255, 255),
                    )

                    rgb.paste(
                        rgba,
                        mask=rgba.getchannel("A"),
                    )

                else:
                    rgb = oriented.convert("RGB")

                try:
                    rgb.save(
                        temporary,
                        "JPEG",
                        quality=86,
                        optimize=True,
                    )
                finally:
                    rgb.close()

        os.replace(
            temporary,
            destination,
        )

    except Exception:
        temporary.unlink(
            missing_ok=True
        )
        raise

    return (
        "/static/uploads/"
        f"coach-applications/{filename}"
    )


@router.post(
    "/photos",
    status_code=status.HTTP_201_CREATED,
)
async def upload_coach_photo(
    file: UploadFile = File(...),
    coach: User = Depends(get_current_coach),
) -> dict[str, str]:

    if file.content_type not in ALLOWED_TYPES:
        raise HTTPException(
            status_code=422,
            detail="Допустимы только JPEG, PNG или WebP",
        )

    try:
        content = await file.read(
            MAX_UPLOAD_BYTES + 1
        )

    finally:
        await file.close()

    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail="Фото должно быть не больше 8 МБ",
        )

    if not content:
        raise HTTPException(
            status_code=422,
            detail="Выбран пустой файл",
        )

    try:
        url = await asyncio.to_thread(
            process_and_save_photo,
            content,
        )

    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as error:

        raise HTTPException(
            status_code=422,
            detail="Некорректное изображение",
        ) from error

    return {
        "photo_url": url,
    }
