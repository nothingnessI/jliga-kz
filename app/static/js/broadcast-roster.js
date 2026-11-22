
(() => {
  "use strict";

  const route = location.pathname.match(
    /^\/broadcast\/studio\/(\d+)\/?$/
  );
  if (!route) return;

  const sessionId = Number(route[1]);
  const BASE = `/api/v1/broadcast/sessions/${sessionId}`;
  const $ = id => document.getElementById(id);

  let roster = null;
  let timeline = [];
  let busy = false;

  const labels = {
    goal: "⚽ Гол",
    penalty_goal: "⚽ Пенальти (гол)",
    own_goal: "⚽ Автогол",
    yellow_card: "🟨 Жёлтая",
    red_card: "🟥 Красная",
    assist: "Ассист",
  };

  const actions = [
    ["goal", "⚽ Гол"],
    ["penalty_goal", "Ⓟ Пен. гол"],
    ["yellow_card", "🟨 ЖК"],
    ["red_card", "🟥 КК"],
    ["own_goal", "Автогол"],
  ];

  const css = document.createElement("style");
  css.textContent = `
    #eventStudio {
      margin:20px 0;
      background:#303030;
      border:1px solid #464646;
      border-radius:9px;
      padding:14px;
    }
    #eventStudio h2 {
      font-size:15px;
      color:#f4d5e4;
      margin:0 0 12px;
    }
    #eventStudio .evtools {
      display:flex;
      align-items:center;
      gap:10px;
      flex-wrap:wrap;
      margin-bottom:12px;
    }
    #eventStudio input[type=number] {
      max-width:95px;
    }
    #eventStudio .evgrid {
      display:grid;
      grid-template-columns:1fr 1fr;
      gap:16px;
    }
    #eventStudio .evteam {
      background:#252525;
      border:1px solid #484848;
      border-radius:7px;
      overflow:hidden;
    }
    #eventStudio .evteam h3 {
      font-size:14px;
      background:#1f1f1f;
      margin:0;
      padding:12px;
      color:#c9f12d;
    }
    #eventStudio .evplayer {
      padding:10px;
      border-top:1px solid #424242;
    }
    #eventStudio .evname {
      font-weight:750;
      font-size:13px;
      margin-bottom:7px;
    }
    #eventStudio .evactions {
      display:flex;
      gap:5px;
      flex-wrap:wrap;
    }
    #eventStudio .evbtn {
      background:#494a4c;
      color:white;
      border:1px solid #666;
      border-radius:5px;
      padding:7px 8px;
      cursor:pointer;
      font-size:12px;
    }
    #eventStudio .evbtn:hover {
      filter:brightness(1.16);
    }
    #eventStudio .evbtn:disabled {
      opacity:.4;
      cursor:wait;
    }
    #eventStudio .evbtn.danger {
      color:#ffadb1;
    }
    #eventStudio .evfeed {
      margin-top:17px;
    }
    #eventStudio .eventrow {
      display:flex;
      align-items:center;
      gap:10px;
      border-top:1px solid #484848;
      padding:9px 3px;
    }
    #eventStudio .eventrow span:nth-child(2) {
      flex:1;
      min-width:0;
    }
    #eventStudio .evmuted {
      color:#aeb4c1;
      font-size:11px;
    }
    #eventStudio .evstatus {
      margin-bottom:12px;
      color:#c9f12d;
      white-space:pre-wrap;
    }
    #eventStudio .evstatus.error {
      color:#ff9f9f;
    }
    @media(max-width:740px) {
      #eventStudio .evgrid {
        grid-template-columns:1fr;
      }
    }
  `;
  document.head.append(css);

  const root = document.createElement("section");
  root.id = "eventStudio";
  root.innerHTML = `
    <h2>⚽ СОСТАВЫ И СОБЫТИЯ МАТЧА · БАЗА ДАННЫХ</h2>
    <div class="evtools">
      <label>
        Минута
        <input id="eventMinute" type="number"
          min="0" max="120" value="1" class="field">
      </label>
      <label>
        <input type="checkbox" id="eventAutoMinute" checked>
        По часам матча
      </label>
      <button class="btn" type="button" id="eventReload">
        ↻ Обновить заявки
      </button>
      <a class="muted" id="eventAdminLink"
        target="_blank" rel="noopener">
        Открыть события в админке
      </a>
    </div>
    <div class="evmuted">
      Кнопка игрока записывает событие в match_events.
      Голы пересчитывают счёт матча и эфирного табло.
    </div>
    <div id="eventStatus" class="evstatus" role="status"></div>
    <div class="evgrid">
      <div id="eventHome" class="evteam"></div>
      <div id="eventAway" class="evteam"></div>
    </div>
    <div class="evfeed">
      <h2>СОБЫТИЯ МАТЧА</h2>
      <div id="eventTimeline"></div>
    </div>
  `;

  document.querySelector(".columns")?.after(root);

  function message(text, error = false) {
    const el = $("eventStatus");
    el.textContent = text;
    el.classList.toggle("error", error);
  }

  async function request(url, options = {}) {
    const response = await fetch(url, {
      ...options,
      credentials: "same-origin",
      cache: "no-store",
      headers: {
        Accept: "application/json",
        ...(options.headers || {}),
      },
    });

    const data = await response.json().catch(() => ({}));

    if (!response.ok) {
      const detail = data.detail;
      const text = typeof detail === "string"
        ? detail
        : detail?.message || `HTTP ${response.status}`;

      throw new Error(text);
    }

    return data;
  }

  async function getState() {
    return request(BASE);
  }

  function setBusy(next) {
    busy = next;

    root.querySelectorAll("button").forEach(btn => {
      btn.disabled = next;
    });
  }

  function buildPlayer(team, player) {
    const row = document.createElement("div");
    row.className = "evplayer";

    const title = document.createElement("div");
    title.className = "evname";
    title.textContent =
      `${player.number ?? "—"} · ${player.name}` +
      (player.captain ? " (К)" : "");

    const buttons = document.createElement("div");
    buttons.className = "evactions";

    actions.forEach(([type, caption]) => {
      const button = document.createElement("button");

      button.type = "button";
      button.className = "evbtn";
      button.textContent = caption;

      button.onclick = () => addEvent(
        team.id,
        player.id,
        player.name,
        type
      );

      buttons.append(button);
    });

    row.append(title, buttons);
    return row;
  }

  function renderRoster() {
    if (!roster) return;

    for (const side of ["home", "away"]) {
      const team = roster[side];

      const target = $(
        side === "home" ? "eventHome" : "eventAway"
      );

      target.replaceChildren();

      const heading = document.createElement("h3");
      heading.textContent =
        `${team.name} (${team.players.length})`;

      target.append(heading);

      if (!team.players.length) {
        const empty = document.createElement("div");

        empty.className = "evplayer evmuted";
        empty.textContent =
          "Нет активных игроков в заявке турнира";

        target.append(empty);
      }

      team.players.forEach(player => {
        target.append(buildPlayer(team, player));
      });
    }
  }

  function renderEvents() {
    const target = $("eventTimeline");
    target.replaceChildren();

    if (!timeline.length) {
      target.textContent = "Событий пока нет";
      return;
    }

    timeline.forEach(event => {
      const row = document.createElement("div");
      row.className = "eventrow";

      const minute = document.createElement("b");
      minute.textContent = `${event.minute}′`;

      const description = document.createElement("span");
      description.textContent =
        `${labels[event.event_type] || event.event_type}` +
        ` · ${event.player_name} · ${event.team_name}`;

      const undo = document.createElement("button");
      undo.className = "evbtn danger";
      undo.type = "button";
      undo.textContent = "Отменить";
      undo.onclick = () => deleteEvent(event);

      row.append(minute, description, undo);
      target.append(row);
    });
  }

  async function loadRoster() {
    roster = await request(`${BASE}/roster`);

    $("eventAdminLink").href =
      `/admin/matches/${roster.match_id}/events`;

    renderRoster();
  }

  async function loadEvents() {
    if (busy) return;

    timeline = await request(`${BASE}/events`);
    renderEvents();
  }

  async function eventMinute() {
    if (!$("eventAutoMinute").checked) {
      const minute = Number($("eventMinute").value);

      if (
        !Number.isInteger(minute) ||
        minute < 0 ||
        minute > 120
      ) {
        throw new Error(
          "Минута должна быть от 0 до 120"
        );
      }

      return minute;
    }

    const current = await getState();

    const minute = Math.min(
      120,
      Math.floor(
        Number(current.current_elapsed_ms || 0) / 60000
      ) + 1
    );

    $("eventMinute").value = minute;
    return minute;
  }

  function updateStudio(data) {
    if (
      data?.state &&
      typeof window.render === "function"
    ) {
      window.render(data.state);
    }

    if (typeof window.refresh === "function") {
      window.refresh(true);
    }
  }

  async function addEvent(
    teamId,
    playerId,
    playerName,
    eventType
  ) {
    if (busy) return;

    setBusy(true);

    try {
      const minute = await eventMinute();
      const current = await getState();

      const result = await request(
        `${BASE}/events`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            team_id: teamId,
            player_id: playerId,
            event_type: eventType,
            minute,
            expected_revision: current.revision,
          }),
        }
      );

      timeline = result.events;
      renderEvents();
      updateStudio(result);

      message(
        `Записано в БД: ${labels[eventType]}` +
        ` · ${playerName} · ${minute}′`
      );

    } catch (error) {
      message(
        `Не сохранено: ${error.message}`,
        true
      );

    } finally {
      setBusy(false);
    }
  }

  async function deleteEvent(event) {
    if (busy) return;

    if (!confirm(
      `Удалить событие ${event.minute}′ — ` +
      `${event.player_name}?`
    )) {
      return;
    }

    setBusy(true);

    try {
      const current = await getState();

      const result = await request(
        `${BASE}/events/${event.id}`,
        {
          method: "DELETE",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            expected_revision: current.revision,
          }),
        }
      );

      timeline = result.events;
      renderEvents();
      updateStudio(result);

      message(
        "Событие удалено из БД. Счёт пересчитан."
      );

    } catch (error) {
      message(
        `Ошибка удаления: ${error.message}`,
        true
      );

    } finally {
      setBusy(false);
    }
  }

  async function showLineup(side) {
    if (!roster) return;

    const team = roster[side];

    try {
      const current = await getState();

      const lines = team.players
        .slice(0, 14)
        .map(player =>
          `${player.number ?? "—"}  ${player.name}`
        );

      await request(`${BASE}/commands`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          action: "SHOW_GRAPHIC",
          expected_revision: current.revision,
          payload: {
            graphic_type: "LINEUP",
            payload: {
              side,
              team_name: team.name,
              lines,
            },
          },
        }),
      });

      message(
        `Заявка «${team.name}» отправлена в эфир.`
      );

    } catch (error) {
      message(
        `Графика не показана: ${error.message}`,
        true
      );
    }
  }

  function wireExistingButtons() {
    for (const side of ["home", "away"]) {
      const key = `${side}_lineup`;

      const button = document.querySelector(
        `[data-graphic="${key}"]`
      );

      if (button) {
        button.textContent =
          `Заявка ${side === "home" ? "HOME" : "AWAY"}`;

        button.onclick = () => showLineup(side);
      }
    }

    const eventsButton = document.querySelector(
      '[data-graphic="events"]'
    );

    if (eventsButton) {
      eventsButton.onclick = () =>
        $("eventTimeline").scrollIntoView({
          behavior: "smooth",
        });
    }

    const live = $("liveEvents");

    if (live) {
      live.onclick = () =>
        $("eventStudio").scrollIntoView({
          behavior: "smooth",
        });
    }

    const penalties = document.querySelector(
      '[data-graphic="penalties"]'
    );

    if (penalties) {
      penalties.onclick = () => {
        message(
          "Для регистрации забитого пенальти " +
          "нажмите «Ⓟ Пен. гол» возле игрока."
        );

        $("eventStudio").scrollIntoView({
          behavior: "smooth",
        });
      };
    }
  }

  $("eventReload").onclick = async () => {
    try {
      await Promise.all([
        loadRoster(),
        loadEvents(),
      ]);

      message("Заявки и события обновлены");

    } catch (error) {
      message(error.message, true);
    }
  };

  wireExistingButtons();

  Promise.all([
    loadRoster(),
    loadEvents(),
  ]).catch(error => {
    message(
      `Ошибка подключения к базе: ${error.message}`,
      true
    );
  });

  setInterval(() => {
    loadEvents().catch(() => {});
  }, 3000);

  setInterval(() => {
    if (!busy) {
      loadRoster().catch(() => {});
    }
  }, 30000);
})();
