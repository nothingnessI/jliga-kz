
/* J-LIGA /admin/users — no external JS libraries needed. */
(() => {
  "use strict";

  const API = "/admin/api/users";
  const $ = id => document.getElementById(id);

  const roleNames = {
    admin: "Администратор",
    editor: "Редактор",
    captain: "Капитан",
    user: "Пользователь"
  };

  let page = 1;
  let pages = 1;
  let currentAdmin = null;
  let selected = null;
  let rows = [];
  let teamsLoaded = false;
  let busy = false;
  let searchTimer = null;
  let requestId = 0;

  const http = async (url, options = {}) => {
    const response = await fetch(url, {
      ...options,
      cache: "no-store",
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        ...(options.method
          ? {"X-JLiga-Admin-Action": "1"}
          : {}),
        ...(options.body
          ? {"Content-Type": "application/json"}
          : {}),
        ...options.headers
      }
    });

    const data = await response.json().catch(() => ({}));

    if (!response.ok) {
      const detail = data.detail;

      const msg = typeof detail === "string"
        ? detail
        : Array.isArray(detail)
          ? detail.map(e => e.msg || "Ошибка").join("; ")
          : detail?.message || `HTTP ${response.status}`;

      throw new Error(
        response.status === 401
          ? "Войди в админку заново"
          : msg
      );
    }

    return data;
  };

  const send = (url, method, object) => http(url, {
    method,
    ...(object !== undefined
      ? {body: JSON.stringify(object)}
      : {})
  });

  function notice(text, error = false) {
    $("usersAlert").textContent = text;
    $("usersAlert").classList.toggle("error", error);
  }

  function node(tag, className = "", content = null) {
    const n = document.createElement(tag);

    if (className) {
      n.className = className;
    }

    if (content !== null) {
      n.textContent = String(content);
    }

    return n;
  }

  function date(value) {
    if (!value) return "—";

    const parsed = new Date(value);

    return Number.isNaN(parsed.getTime())
      ? String(value)
      : new Intl.DateTimeFormat("ru-RU", {
          day: "2-digit",
          month: "2-digit",
          year: "numeric"
        }).format(parsed);
  }

  function badge(text, cls) {
    return node("span", `users-pill ${cls}`, text);
  }

  function button(text, cls, handler) {
    const b = node("button", `btn btn-sm ${cls}`, text);

    b.type = "button";
    b.disabled = busy;
    b.addEventListener("click", handler);

    return b;
  }

  function makeActions(u, mobile = false) {
    const box = node(
      "div",
      mobile ? "users-card-actions" : "users-actions"
    );

    box.append(
      button(
        "Изменить",
        "btn-outline-primary",
        () => edit(u)
      ),
      button(
        "Новый пароль",
        "btn-outline-secondary",
        () => resetPassword(u)
      )
    );

    if (u.id !== currentAdmin) {
      box.append(
        button(
          u.is_active
            ? "Блокировать"
            : "Разблокировать",
          u.is_active
            ? "btn-outline-danger"
            : "btn-outline-success",
          () => toggle(u)
        )
      );
    }

    return box;
  }

  function render() {
    const tbody = $("usersTable");
    const mobile = $("usersMobile");

    tbody.replaceChildren();
    mobile.replaceChildren();

    if (!rows.length) {
      const tr = node("tr");
      const td = node(
        "td",
        "users-empty",
        "Пользователей по выбранным условиям нет"
      );

      td.colSpan = 6;
      tr.append(td);
      tbody.append(tr);

      mobile.append(
        node(
          "div",
          "users-empty",
          "Пользователей не найдено"
        )
      );
    }

    for (const u of rows) {
      const tr = node("tr");
      const person = node("td");

      person.append(
        node(
          "div",
          "users-name",
          `${u.last_name} ${u.first_name}` +
          (u.id === currentAdmin ? " (вы)" : "")
        ),
        node("div", "users-email", u.email)
      );

      const r = node("td");
      r.append(
        badge(roleNames[u.role] || u.role, "role")
      );

      const team = node(
        "td",
        "",
        u.team_name || "—"
      );

      const status = node("td");

      status.append(
        badge(
          u.is_active ? "Активен" : "Заблокирован",
          u.is_active ? "ok" : "off"
        )
      );

      const registered = node(
        "td",
        "",
        date(u.created_at)
      );

      const actions = node("td");
      actions.append(makeActions(u));

      tr.append(
        person,
        r,
        team,
        status,
        registered,
        actions
      );

      tbody.append(tr);

      // Mobile card
      const card = node("article", "users-card");
      const header = node(
        "div",
        "users-card-top"
      );

      const info = node("div");

      info.append(
        node(
          "div",
          "users-name",
          `${u.last_name} ${u.first_name}`
        ),
        node("div", "users-email", u.email)
      );

      header.append(
        info,
        badge(
          u.is_active ? "Активен" : "Заблокирован",
          u.is_active ? "ok" : "off"
        )
      );

      const meta = node("div", "users-card-meta");

      meta.append(
        badge(roleNames[u.role] || u.role, "role"),
        node(
          "span",
          "users-muted",
          u.team_name || "Без команды"
        )
      );

      card.append(
        header,
        meta,
        makeActions(u, true)
      );

      mobile.append(card);
    }

    $("usersPage").textContent = `${page} / ${pages}`;
    $("usersPrev").disabled = page <= 1 || busy;
    $("usersNext").disabled = page >= pages || busy;
  }

  async function load() {
    const sequence = ++requestId;

    const params = new URLSearchParams({
      page: String(page),
      page_size: "15"
    });

    const q = $("usersSearch").value.trim();
    const role = $("usersRole").value;

    if (q) params.set("q", q);
    if (role) params.set("role", role);

    params.set(
      "active",
      $("usersActiveFilter").value
    );

    try {
      const data = await http(`${API}?${params}`);

      if (sequence !== requestId) return;

      rows = data.items;
      pages = data.pages;
      currentAdmin = data.current_admin_id;

      $("usersTotal").textContent = data.stats.total;
      $("usersActive").textContent = data.stats.active;
      $("usersBlocked").textContent = data.stats.blocked;
      $("usersCount").textContent = `Найдено: ${data.total}`;

      render();

    } catch (error) {
      if (sequence === requestId) {
        notice(error.message, true);
      }
    }
  }

  async function loadTeams() {
    if (teamsLoaded) return;

    const teams = await http(`${API}/teams`);
    const select = $("ufTeam");

    select.replaceChildren(
      new Option("Без команды", "")
    );

    for (const team of teams) {
      select.add(
        new Option(
          team.name,
          String(team.id)
        )
      );
    }

    teamsLoaded = true;
  }

  async function openForm(u = null) {
    if (busy) return;

    try {
      await loadTeams();
    } catch (e) {
      return notice(
        `Не удалось загрузить команды: ${e.message}`,
        true
      );
    }

    selected = u;
    $("usersForm").reset();

    $("usersDialogTitle").textContent = u
      ? `Редактирование #${u.id}`
      : "Новый пользователь";

    $("ufLast").value = u?.last_name || "";
    $("ufFirst").value = u?.first_name || "";
    $("ufEmail").value = u?.email || "";
    $("ufPhone").value = u?.phone || "";
    $("ufRole").value = u?.role || "user";

    $("ufTeam").value = u?.team_id
      ? String(u.team_id)
      : "";

    $("ufActive").checked = u
      ? u.is_active
      : true;

    $("ufActive").disabled =
      !u || u.id === currentAdmin;

    $("ufVerified").checked = u
      ? u.is_verified
      : true;

    $("usersDialog").showModal();
  }

  function edit(u) {
    return openForm(u);
  }

  function revealPassword(email, temporaryPassword) {
    $("passwordMessage").textContent =
      `Аккаунт: ${email}. Скопируй временный пароль ` +
      "и передай пользователю безопасно.";

    $("passwordValue").value = temporaryPassword;
    $("passwordDialog").showModal();
    $("passwordValue").select();
  }

  async function save(e) {
    e.preventDefault();

    if (busy) return;

    const data = {
      last_name: $("ufLast").value.trim(),
      first_name: $("ufFirst").value.trim(),
      email: $("ufEmail").value.trim(),
      phone: $("ufPhone").value.trim() || null,
      role: $("ufRole").value,
      team_id: $("ufTeam").value
        ? Number($("ufTeam").value)
        : null,
      is_verified: $("ufVerified").checked
    };

    if (selected) {
      data.is_active = $("ufActive").checked;
    }

    if (
      data.role === "admin" &&
      selected?.role !== "admin" &&
      !confirm(
        "Предоставить этому пользователю " +
        "полный административный доступ?"
      )
    ) {
      return;
    }

    busy = true;
    $("usersSave").disabled = true;

    try {
      const result = selected
        ? await send(
            `${API}/${selected.id}`,
            "PATCH",
            data
          )
        : await send(
            API,
            "POST",
            data
          );

      $("usersDialog").close();

      notice(
        selected
          ? "Изменения сохранены"
          : "Пользователь создан"
      );

      await load();

      if (result.temporary_password) {
        revealPassword(
          result.user.email,
          result.temporary_password
        );
      }

    } catch (e) {
      notice(e.message, true);

    } finally {
      busy = false;
      $("usersSave").disabled = false;
      render();
    }
  }

  async function toggle(u) {
    const action = u.is_active
      ? "заблокировать"
      : "разблокировать";

    if (!confirm(
      `Действительно ${action} ${u.email}?`
    )) {
      return;
    }

    busy = true;

    try {
      await send(
        `${API}/${u.id}`,
        "PATCH",
        {is_active: !u.is_active}
      );

      notice(
        u.is_active
          ? "Аккаунт заблокирован"
          : "Аккаунт разблокирован"
      );

      await load();

    } catch (e) {
      notice(e.message, true);

    } finally {
      busy = false;
      render();
    }
  }

  async function resetPassword(u) {
    if (!confirm(
      `Создать новый временный пароль для ${u.email}? ` +
      "Старый пароль перестанет работать."
    )) {
      return;
    }

    busy = true;

    try {
      const result = await send(
        `${API}/${u.id}/reset-password`,
        "POST"
      );

      revealPassword(
        result.email,
        result.temporary_password
      );

      notice("Пароль обновлён");

    } catch (e) {
      notice(e.message, true);

    } finally {
      busy = false;
      render();
    }
  }

  $("usersAdd").addEventListener(
    "click",
    () => openForm()
  );

  $("usersForm").addEventListener("submit", save);

  $("usersClose").addEventListener(
    "click",
    () => $("usersDialog").close()
  );

  $("usersCancel").addEventListener(
    "click",
    () => $("usersDialog").close()
  );

  const closePassword = () =>
    $("passwordDialog").close();

  $("passwordClose").addEventListener(
    "click",
    closePassword
  );

  $("passwordDone").addEventListener(
    "click",
    closePassword
  );

  $("passwordDialog").addEventListener(
    "close",
    () => {
      $("passwordValue").value = "";
    }
  );

  $("passwordCopy").addEventListener(
    "click",
    async () => {
      try {
        await navigator.clipboard.writeText(
          $("passwordValue").value
        );

        $("passwordCopy").textContent = "Скопировано";

        setTimeout(
          () => {
            $("passwordCopy").textContent = "Копировать";
          },
          1500
        );

      } catch {
        $("passwordValue").select();
        document.execCommand("copy");
      }
    }
  );

  $("usersPrev").addEventListener(
    "click",
    () => {
      if (page > 1) {
        page--;
        load();
      }
    }
  );

  $("usersNext").addEventListener(
    "click",
    () => {
      if (page < pages) {
        page++;
        load();
      }
    }
  );

  const filter = () => {
    page = 1;
    load();
  };

  $("usersSearch").addEventListener(
    "input",
    () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(filter, 350);
    }
  );

  $("usersRole").addEventListener(
    "change",
    filter
  );

  $("usersActiveFilter").addEventListener(
    "change",
    filter
  );

  load();
})();
