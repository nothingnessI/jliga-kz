
(() => {
    "use strict";

    const password = document.getElementById(
        "authPassword"
    );

    if (password) {
        password.minLength = 8;

        const wrapper = document.createElement("div");
        wrapper.className = "input-group";

        password.parentNode.insertBefore(
            wrapper,
            password
        );

        wrapper.appendChild(password);

        const eye = document.createElement("button");

        eye.type = "button";
        eye.className = "btn btn-outline-secondary";
        eye.setAttribute(
            "aria-label",
            "Показать пароль"
        );
        eye.setAttribute(
            "aria-pressed",
            "false"
        );

        eye.innerHTML =
            '<i class="bi bi-eye" aria-hidden="true"></i>';

        wrapper.appendChild(eye);

        const hint = document.createElement("div");
        hint.className = "form-text";
        hint.textContent =
            "Для регистрации — минимум 8 символов";

        wrapper.insertAdjacentElement(
            "afterend",
            hint
        );

        eye.addEventListener("click", () => {
            const visible =
                password.type === "password";

            password.type = visible
                ? "text"
                : "password";

            eye.setAttribute(
                "aria-label",
                visible
                    ? "Скрыть пароль"
                    : "Показать пароль"
            );

            eye.setAttribute(
                "aria-pressed",
                String(visible)
            );

            eye.innerHTML = visible
                ? '<i class="bi bi-eye-slash"></i>'
                : '<i class="bi bi-eye"></i>';
        });
    }

    function notify(message, kind = "danger") {
        if (typeof showNotice === "function") {
            showNotice(message, kind);
            return;
        }

        const alert = document.getElementById(
            "notice"
        );

        if (!alert) return;

        alert.className = `alert alert-${kind}`;
        alert.textContent = message;
    }

    function showPhoto(
        input,
        photo,
        empty,
        description,
        url
    ) {
        input.value = url;

        if (description) {
            description.textContent =
                "Фотография загружена";
        }

        if (photo) {
            photo.onload = () => {
                photo.classList.remove("d-none");

                if (empty) {
                    empty.classList.add("d-none");
                }
            };

            photo.onerror = () => {
                photo.classList.add("d-none");

                if (empty) {
                    empty.classList.remove("d-none");
                }

                notify(
                    "Не удалось открыть загруженную фотографию"
                );
            };

            photo.src = url;
        }
    }

    function applyPhoto(button, url) {
        const card = button.closest(
            ".player-card"
        );

        if (card) {
            const input = card.querySelector(
                ".player-photo"
            );

            if (!input) {
                throw new Error(
                    "Поле фотографии игрока не найдено"
                );
            }

            showPhoto(
                input,
                card.querySelector(
                    ".player-photo-preview"
                ),
                card.querySelector(
                    ".player-photo-placeholder"
                ),
                card.querySelector(
                    ".player-photo-description, .player-photo-caption"
                ),
                url
            );

            if (card.dataset.existingId) {
                card.dataset.existingId = "";

                const note = card.querySelector(
                    ".existing-note"
                );

                if (note) {
                    note.textContent =
                        "Фотография изменена: связь с игроком из базы сброшена";
                }
            }

            return;
        }

        const input = document.getElementById(
            "coachPhoto"
        );

        if (!input) {
            throw new Error(
                "Поле фотографии тренера не найдено"
            );
        }

        showPhoto(
            input,
            document.getElementById(
                "coachPhotoPreview"
            ),
            document.getElementById(
                "coachPhotoPlaceholder"
            ),
            null,
            url
        );

        const remove = document.getElementById(
            "coachPhotoRemove"
        );

        if (remove) {
            remove.classList.remove("d-none");
        }
    }

    function labelGalleryButtons() {
        document.querySelectorAll(
            "#coachPhotoButton, " +
            "#coachPhotoChange, " +
            ".player-photo-button, " +
            ".player-photo-change"
        ).forEach(button => {
            if (button.dataset.galleryReady) {
                return;
            }

            button.dataset.galleryReady = "1";

            button.innerHTML =
                '<i class="bi bi-image me-1"></i> ' +
                "Выбрать из галереи";
        });
    }

    labelGalleryButtons();

    const players = document.getElementById(
        "playersContainer"
    );

    if (players) {
        new MutationObserver(
            labelGalleryButtons
        ).observe(players, {
            childList: true
        });
    }

    document.addEventListener(
        "click",
        event => {
            const button = event.target.closest(
                "#coachPhotoButton, " +
                "#coachPhotoChange, " +
                ".player-photo-button, " +
                ".player-photo-change"
            );

            if (!button || button.disabled) {
                return;
            }

            // Не запускаем старый prompt() со ссылкой.
            event.preventDefault();
            event.stopImmediatePropagation();

            const picker = document.createElement(
                "input"
            );

            picker.type = "file";
            picker.accept =
                "image/jpeg,image/png,image/webp";

            picker.setAttribute(
                "aria-label",
                "Выберите фотографию из галереи"
            );

            picker.style.display = "none";

            document.body.appendChild(picker);

            const cleanup = () => picker.remove();

            picker.addEventListener(
                "cancel",
                cleanup,
                { once: true }
            );

            picker.addEventListener(
                "change",
                async () => {
                    const file = picker.files?.[0];

                    cleanup();

                    if (!file) return;

                    if (![
                        "image/jpeg",
                        "image/png",
                        "image/webp"
                    ].includes(file.type)) {
                        notify(
                            "Выберите JPG, PNG или WebP"
                        );
                        return;
                    }

                    if (
                        file.size >
                        8 * 1024 * 1024
                    ) {
                        notify(
                            "Размер фотографии должен быть не больше 8 МБ"
                        );
                        return;
                    }

                    const token = sessionStorage.getItem(
                        "jliga_coach_token"
                    );

                    if (!token) {
                        notify(
                            "Для загрузки фотографии войдите в кабинет тренера"
                        );
                        return;
                    }

                    const previous = button.innerHTML;

                    button.disabled = true;
                    button.textContent = "Загрузка...";

                    try {
                        const data = new FormData();

                        data.append(
                            "file",
                            file,
                            file.name
                        );

                        const response = await fetch(
                            "/api/v1/coach/photos",
                            {
                                method: "POST",
                                headers: {
                                    Authorization:
                                        `Bearer ${token}`
                                },
                                body: data,
                                credentials: "same-origin"
                            }
                        );

                        const json = await response.json()
                            .catch(() => ({}));

                        if (!response.ok) {
                            const detail = json.detail;

                            throw new Error(
                                typeof detail === "string"
                                    ? detail
                                    : `Ошибка загрузки: HTTP ${response.status}`
                            );
                        }

                        if (!json.photo_url) {
                            throw new Error(
                                "Сервер не вернул URL изображения"
                            );
                        }

                        applyPhoto(
                            button,
                            json.photo_url
                        );

                    } catch (error) {
                        notify(
                            error.message ||
                            "Не удалось загрузить фотографию"
                        );

                    } finally {
                        button.disabled = false;
                        button.innerHTML = previous;
                    }
                },
                { once: true }
            );

            picker.click();
        },
        true
    );
})();
