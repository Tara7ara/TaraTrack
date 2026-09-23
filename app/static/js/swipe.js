// Desliza una fila de pendientes hacia la derecha para marcarla como vista (estilo TVTime).
document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll(".list-row.swipeable").forEach(setupSwipe);
});

// El buscador/filtro de /pendientes reemplaza la lista entera via htmx (hx-select) -
// las filas nuevas no llevan el listener de swipe puesto en DOMContentLoaded, hay que
// volver a engancharlo en lo que haya entrado con el swap.
document.addEventListener("htmx:afterSwap", (e) => {
    e.target.querySelectorAll(".list-row.swipeable").forEach(setupSwipe);
});

function setupSwipe(row) {
    const content = row.querySelector(".swipe-content");
    // Bug real (AGY, 2026-09-18): sin esta guarda, una fila .swipeable sin
    // .swipe-content dentro (markup distinto en algun render parcial) tiraba un
    // TypeError aqui - forEach no tiene try/catch propio, asi que abortaba el
    // resto de filas de la pagina sin dejarles el gesto de swipe montado.
    if (!content) return;
    const btn = row.querySelector('button[hx-post^="/vista/"]');
    const threshold = 90;
    let startX = 0;
    let dx = 0;
    let dragging = false;

    function collapseAndRemove() {
        row.style.transition = "max-height 250ms ease, opacity 250ms ease, margin-top 250ms ease";
        row.style.maxHeight = row.offsetHeight + "px";
        requestAnimationFrame(() => {
            row.style.maxHeight = "0px";
            row.style.opacity = "0";
            row.style.marginTop = "0px";
        });
        setTimeout(() => row.remove(), 260);
    }

    // El listener va en la fila entera y no en un boton concreto: "quitar de
    // pendientes" pasa por una confirmacion en dos pasos (ver
    // partials/entry_actions.html, estado "pending_confirm") y el boton final
    // ("¿Quitar? Sí") todavia no existe en el DOM en este momento, lo inserta un
    // swap de htmx despues. Delegando en la fila tambien se captan esos botones.
    //
    // Se decide por la peticion (ruta + POST) y no por e.detail.elt: el swap
    // (hx-target="closest .actions", outerHTML) saca el boton del DOM antes de que
    // htmx dispare afterRequest, asi que htmx lo vuelve a disparar sobre el primer
    // ancestro que sigue conectado y sobrescribe detail.elt con ese ancestro - nunca
    // es el boton. El POST importa porque "¿Quitar?" y "No" son GET a /pendiente/...
    // y no deben quitar la fila.
    row.addEventListener("htmx:afterRequest", (e) => {
        if (!e.detail.successful) return;
        const path = e.detail.pathInfo && e.detail.pathInfo.requestPath;
        const verb = e.detail.requestConfig && e.detail.requestConfig.verb;
        if (verb === "post" && path && (path.startsWith("/vista/") || path.startsWith("/pendiente/"))) {
            collapseAndRemove();
        }
    });

    content.addEventListener("touchstart", (e) => {
        startX = e.touches[0].clientX;
        dragging = true;
        content.style.transition = "none";
    }, { passive: true });

    content.addEventListener("touchmove", (e) => {
        if (!dragging) return;
        dx = Math.max(0, Math.min(140, e.touches[0].clientX - startX));
        // El fondo verde solo se ensena mientras hay gesto de verdad.
        row.classList.toggle("swiping", dx > 0);
        content.style.transform = `translateX(${dx}px)`;
    }, { passive: true });

    content.addEventListener("touchend", () => {
        if (!dragging) return;
        dragging = false;
        content.style.transition = "transform 200ms ease";
        if (dx > threshold && btn) {
            content.style.transform = "translateX(100%)";
            btn.click();
        } else {
            content.style.transform = "translateX(0)";
            setTimeout(() => row.classList.remove("swiping"), 200);
        }
        dx = 0;
    });
}
