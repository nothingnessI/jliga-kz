
(() => {
    "use strict";

    const root = document.getElementById(
        "jliga-live"
    );

    if (!root) return;

    const tournamentId = Number(
        root.dataset.tournamentId
    );

    if (
        !Number.isInteger(tournamentId) ||
        tournamentId < 1
    ) {
        return;
    }

    const endpoint =
        `/api/v1/broadcast/tournaments/${tournamentId}/live`;

    let previous = null;
    let loading = false;

    const style = document.createElement("style");

    style.textContent = `
        #jliga-live {
            margin: 18px 0;
            color: #fff;
            font: 14px system-ui, Arial, sans-serif;
        }

        #jliga-live .live-title {
            margin: 0 0 12px;
            color: #f0f0f0;
            font-size: 20px;
            font-weight: 800;
        }

        #jliga-live .live-list {
            display: grid;
            grid-template-columns:
                repeat(auto-fit, minmax(250px, 1fr));
            gap: 12px;
        }

        #jliga-live .live-match {
            background: #202842;
            border-radius: 9px;
            padding: 15px;
            color: #fff;
            text-decoration: none;
            display: block;
        }

        #jliga-live .live-teams {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 10px;
            font-weight: 700;
        }

        #jliga-live .live-score {
            font-size: 24px;
            font-weight: 900;
            white-space: nowrap;
            color: #c9f12d;
        }

        #jliga-live .live-label {
            font-size: 11px;
            color: #ffb9bd;
            margin: 0 0 12px;
            font-weight: 800;
        }

        #jliga-live .live-empty {
            color: #9298a7;
        }
    `;

    document.head.append(style);

    function create(tag, cls, text) {
        const element = document.createElement(tag);

        if (cls) {
            element.className = cls;
        }

        if (text !== undefined) {
            element.textContent = String(text);
        }

        return element;
    }

    function render(matches) {
        root.replaceChildren();

        root.append(
            create(
                "div",
                "live-title",
                "● Матчи в прямом эфире"
            )
        );

        if (!matches.length) {
            root.append(
                create(
                    "div",
                    "live-empty",
                    "Сейчас нет матчей в прямом эфире"
                )
            );

            return;
        }

        const list = create(
            "div",
            "live-list"
        );

        for (const match of matches) {
            const card = create(
                match.broadcast_id ? "a" : "div",
                "live-match"
            );

            if (match.broadcast_id) {
                card.href =
                    `/broadcast/overlay/${match.broadcast_id}`;
            }

            card.append(
                create(
                    "div",
                    "live-label",
                    match.status === "live"
                        ? "● LIVE"
                        : "● В ЭФИРЕ"
                )
            );

            const line = create(
                "div",
                "live-teams"
            );

            line.append(
                create(
                    "span",
                    "",
                    `${match.home} — ${match.away}`
                )
            );

            line.append(
                create(
                    "strong",
                    "live-score",
                    `${match.home_score ?? "—"} : ` +
                    `${match.away_score ?? "—"}`
                )
            );

            card.append(line);
            list.append(card);
        }

        root.append(list);
    }

    async function refresh() {
        if (loading || document.hidden) return;

        loading = true;

        try {
            const response = await fetch(
                endpoint,
                {
                    cache: "no-store"
                }
            );

            if (!response.ok) {
                throw new Error(
                    `HTTP ${response.status}`
                );
            }

            const data = await response.json();
            const matches = data.matches || [];

            const ids = new Set(
                matches.map(match => match.id)
            );

            // Если матч исчез из LIVE-списка,
            // перезагружаем турнирную страницу.
            // Так обновится и турнирная таблица.
            const finished =
                previous &&
                [...previous].some(
                    id => !ids.has(id)
                );

            if (finished) {
                location.reload();
                return;
            }

            previous = ids;

            render(matches);

        } catch (error) {
            console.warn(
                "J-LIGA live scoreboard:",
                error
            );

        } finally {
            loading = false;
        }
    }

    refresh();

    setInterval(
        refresh,
        2500
    );

    document.addEventListener(
        "visibilitychange",
        () => {
            if (!document.hidden) {
                refresh();
            }
        }
    );
})();
