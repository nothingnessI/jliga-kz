
/* J-LIGA mobile helpers.
   Preserves Bootstrap, custom sidebar toggles,
   and all API handlers. */

(() => {
    "use strict";

    const mobile = window.matchMedia(
        "(max-width: 767.98px)"
    );

    function enhanceTables() {
        if (!mobile.matches) return;

        document.querySelectorAll("table").forEach(table => {
            if (
                table.closest(
                    ".table-responsive, " +
                    ".jliga-table-scroll, " +
                    "[data-jliga-no-scroll], " +
                    "#scene"
                )
            ) {
                return;
            }

            // Nested tables are not modified.
            if (table.querySelector("table")) {
                return;
            }

            const columns =
                table.querySelectorAll(
                    "thead tr:first-child > th"
                ).length ||
                table.querySelectorAll(
                    "tr:first-child > :is(td, th)"
                ).length;

            if (
                columns < 5 &&
                table.scrollWidth <= window.innerWidth - 24
            ) {
                return;
            }

            const parent = table.parentElement;

            if (!parent) return;

            const wrapper = document.createElement("div");

            wrapper.className = "jliga-table-scroll";
            wrapper.tabIndex = 0;

            wrapper.setAttribute(
                "role",
                "region"
            );

            wrapper.setAttribute(
                "aria-label",
                "Таблица с горизонтальной прокруткой"
            );

            parent.insertBefore(wrapper, table);
            wrapper.appendChild(table);
        });
    }

    function prepareImages() {
        document
            .querySelectorAll("img:not([loading])")
            .forEach(img => {
                if (
                    !img.closest(
                        ".header-top, .navbar, .topbar, #scene"
                    ) &&
                    !img.hasAttribute("data-no-lazy")
                ) {
                    img.loading = "lazy";
                }
            });
    }

    function init() {
        prepareImages();
        enhanceTables();
    }

    if (document.readyState === "loading") {
        document.addEventListener(
            "DOMContentLoaded",
            init,
            {once: true}
        );
    } else {
        init();
    }

    if (typeof mobile.addEventListener === "function") {
        mobile.addEventListener(
            "change",
            enhanceTables
        );
    }

    // Can be called after rendering a dynamic table.
    window.JLigaResponsive = Object.freeze({
        enhanceTables
    });
})();
