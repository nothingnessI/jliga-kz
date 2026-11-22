
/*
 * J-LIGA Broadcast Graphics Controls
 *
 * Расширяет существующий studio.html.
 * Не дублирует управление счётом и событиями.
 */

(() => {
    "use strict";

    const match = location.pathname.match(
        /^\/broadcast\/studio\/(\d+)\/?$/
    );

    if (!match) return;

    const BASE =
        `/api/v1/broadcast/sessions/${Number(match[1])}`;

    const find = name =>
        document.querySelector(
            `[data-graphic="${name}"]`
        );

    const status = (message, error = false) => {
        const element = document.getElementById(
            "status"
        );

        if (!element) return;

        element.textContent = message;
        element.classList.toggle(
            "error",
            error
        );
    };


    async function api(url, options = {}) {
        const response = await fetch(
            url,
            {
                ...options,
                credentials: "same-origin",
                cache: "no-store",
                headers: {
                    Accept: "application/json",
                    ...(options.headers || {})
                }
            }
        );

        const data = await response.json()
            .catch(() => ({}));

        if (!response.ok) {
            throw new Error(
                typeof data.detail === "string"
                    ? data.detail
                    : data.detail?.message ||
                      `HTTP ${response.status}`
            );
        }

        return data;
    }


    let sending = false;


    async function show(
        graphic_type,
        payload = {}
    ) {
        if (sending) return;

        sending = true;

        try {
            const current = await api(BASE);

            const updated = await api(
                `${BASE}/commands`,
                {
                    method: "POST",
                    headers: {
                        "Content-Type": "application/json"
                    },
                    body: JSON.stringify({
                        action: "SHOW_GRAPHIC",
                        expected_revision:
                            current.revision,
                        payload: {
                            graphic_type,
                            payload
                        }
                    })
                }
            );

            status(
                `В эфире: ${graphic_type} · ревизия ${updated.revision}`
            );

        } catch (error) {
            status(
                `Графика: ${error.message}`,
                true
            );

        } finally {
            sending = false;
        }
    }


    async function presentation() {
        return api(
            `${BASE}/presentation`
        );
    }


    function hook(name, fn) {
        const button = find(name);

        if (button) {
            button.onclick = fn;
        }
    }


    // ===========================================
    // MATCH INFORMATION
    // ===========================================

    hook(
        "match",
        () => show("MATCH_INFO")
    );


    // ===========================================
    // TEAM ROSTERS
    // ===========================================

    hook(
        "home_lineup",
        () => show(
            "LINEUP",
            {
                side: "home",
                page: 0
            }
        )
    );

    hook(
        "away_lineup",
        () => show(
            "LINEUP",
            {
                side: "away",
                page: 0
            }
        )
    );


    // ===========================================
    // COMPARISON
    // ===========================================

    hook(
        "comparison",
        () => show("COMPARISON")
    );


    // ===========================================
    // VAR
    // ===========================================

    hook(
        "var",
        () => show(
            "VAR",
            {
                text: "ПРОВЕРКА VAR"
            }
        )
    );


    // ===========================================
    // STANDINGS
    // ===========================================

    hook(
        "standings",
        async () => {
            try {
                const data = await presentation();

                if (!data.standings.length) {
                    return status(
                        "Турнирная таблица ещё не рассчитана в БД",
                        true
                    );
                }

                await show(
                    "STANDINGS"
                );

            } catch (error) {
                status(
                    error.message,
                    true
                );
            }
        }
    );


    // ===========================================
    // MATCH EVENTS
    // ===========================================

    hook(
        "events",
        async () => {
            try {
                const data = await presentation();

                if (!data.events.length) {
                    return status(
                        "Событий матча ещё нет",
                        true
                    );
                }

                const labels = {
                    goal: "ГОЛ",
                    own_goal: "АВТОГОЛ",
                    penalty_goal: "ПЕНАЛЬТИ",
                    yellow_card: "ЖК",
                    red_card: "КК",
                    substitution_in: "ВЫХОД",
                    substitution_out: "УХОД"
                };

                const lines = data.events
                    .slice(0, 12)
                    .map(event => {
                        const type =
                            labels[event.event_type] ||
                            event.event_type;

                        return (
                            `${event.minute}′ ${type}` +
                            ` · ${event.player_name}`
                        );
                    });

                await show(
                    "MATCH_INFO",
                    {
                        heading: "СОБЫТИЯ МАТЧА",
                        lines
                    }
                );

            } catch (error) {
                status(
                    error.message,
                    true
                );
            }
        }
    );


    // ===========================================
    // PENALTY SHOOTOUT
    // ===========================================

    hook(
        "penalties",
        async () => {
            try {
                const data = await presentation();

                const match = data.match;

                if (
                    match.home_penalty == null ||
                    match.away_penalty == null
                ) {
                    return status(
                        "Счёт серии пенальти не заполнен в матче",
                        true
                    );
                }

                await show(
                    "PENALTIES"
                );

            } catch (error) {
                status(
                    error.message,
                    true
                );
            }
        }
    );


    // ===========================================
    // WEATHER
    // ===========================================

    const weather = find("weather");

    if (weather) {
        weather.disabled = true;

        weather.title =
            "В проекте не подключены достоверные данные о погоде";
    }


    // ===========================================
    // EXTRA GRAPHICS BUTTONS
    // ===========================================

    const grid = find("away_lineup")
        ?.closest(".grid2");

    if (grid) {
        const add = (title, handler) => {
            const button =
                document.createElement("button");

            button.className = "btn";
            button.type = "button";
            button.textContent = title;

            button.onclick = handler;

            grid.append(button);
        };

        add(
            "Тренер HOME",
            () => show(
                "COACH",
                {
                    side: "home"
                }
            )
        );

        add(
            "Тренер AWAY",
            () => show(
                "COACH",
                {
                    side: "away"
                }
            )
        );

        add(
            "Заявка HOME · стр. 2",
            () => show(
                "LINEUP",
                {
                    side: "home",
                    page: 1
                }
            )
        );

        add(
            "Заявка AWAY · стр. 2",
            () => show(
                "LINEUP",
                {
                    side: "away",
                    page: 1
                }
            )
        );
    }

})();
