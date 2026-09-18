# Notas de ingeniería

Cuatro problemas reales resueltos durante el desarrollo, elegidos por ser los que mejor explican decisiones no evidentes del código.

## Migraciones SQLite idempotentes que se comían columnas en BBDD nuevas

Una migración antigua (`_migrate_rating_scale_0_10`) reconstruía las tablas `entries`/`season_ratings` con un `CREATE TABLE ... AS SELECT` sobre una lista de columnas **hardcodeada**, separada de la lista real de migraciones (`MIGRATIONS`, que va añadiendo `ALTER TABLE` con el tiempo). El problema: cualquier columna añadida después de esa lista fija (`elo`, `is_habit`, `auto_watch`, `rd`, `rewatch_started_at`...) desaparecía en cuanto esa reconstrucción se disparaba.

En producción era invisible — la BBDD real ya tenía el flag que salta esa reconstrucción por completo. En una instalación nueva (o en los tests contra una BBDD en memoria) el bug era 100% reproducible: `init_db()` sobre una base de datos vacía perdía silenciosamente las columnas más recientes.

**Fix**: añadir las columnas que faltaban a la lista hardcodeada, y sobre todo, un test de regresión (`tests/test_migrations.py`) que crea una BBDD desde cero y comprueba explícitamente que todas las columnas esperadas existen tras `init_db()` — para que la próxima columna nueva que alguien añada y se olvide de replicar aquí falle alto y claro en CI, no semanas después en silencio.

## Emparejar títulos entre TMDB, AniList y Jikan sin aceptar coincidencias falsas

Tres fuentes de metadatos, tres formas distintas de nombrar lo mismo: TMDB indexa una serie de anime como un único show con todas sus temporadas; AniList mete el número de temporada dentro del propio título (`"... Season 2"`) y solo indexa bien por romaji/inglés, nunca por el título en español que cachea TMDB. Buscar el título literal fallaba constantemente para temporadas ≥2 y para cualquier anime sin licencia occidental fuerte.

Dos piezas resuelven esto:

- **Reintento sin sufijo de temporada** (`_strip_season_suffix` + búsqueda flexible): si el título completo no da resultados, se reintenta sin el `"Season N"`/`"Nth Season"` del final.
- **Freno de seguridad antes de aceptar un match** (`_plausible_match`, luego `_best_plausible_match`): usa `difflib.SequenceMatcher` + comprobación de substring sobre los primeros resultados de TMDB (no solo el primero — ampliado a los 3 primeros tras un caso real donde el resultado correcto quedaba 2º) antes de auto-enlazar o auto-añadir una ficha. Sin este freno, un título de AniList sin match real en TMDB podía enlazar una ficha completamente distinta con confianza total.

Aparte, TMDB tiene entradas "basura" de agregador (ej. `"Serie Seasons 2/3/4"` sin año, sin nota, sin popularidad real) que ensuciaban resultados de búsqueda — filtradas (`_looks_like_junk`) exigiendo que un resultado tenga al menos uno de los tres (año, nota, popularidad ≥1) para considerarse real.

## Backtesting sin fuga de datos para el índice de afinidad

El índice de afinidad (ver README) predice cuánto te va a gustar algo que no has visto — pero para confiar en esa predicción hace falta medir qué tan bien acierta contra tu historial real, **sin que el modelo se "acuerde" de su propia respuesta**.

`_backtest_leave_one_out` recalcula el perfil de gustos completo **una vez por cada título ya puntuado**, sacando ESE título de TODOS los conteos que alimentan apetito y calidad (no solo de la nota final) antes de predecir su score — así la predicción nunca ve, ni de refilón, el dato que está intentando adivinar. El resultado (score predicho vs. nota real, por título) es lo que usa tanto la calibración a percentil como `scripts/tune_affinity_weights.py` (grid search sobre los pesos del modelo, minimizando error absoluto medio contra este mismo backtesting).

Con ~450 títulos puntuados, recalcular el perfil completo 450 veces sería demasiado lento contra la base de datos — por eso toda la materia prima (notas, episodios, géneros, franquicias...) se carga una única vez en memoria (`_load_affinity_raw`) y el backtesting reagrega sobre esa copia en Python, sin volver a tocar SQLite en cada iteración.

## `PRAGMA foreign_keys = OFF` como no-op silencioso durante una migración real

Pasar de una única cuenta a varias exige reconstruir tablas con restricciones `UNIQUE` distintas (SQLite no permite tocar un `UNIQUE` con `ALTER TABLE`) — mismo patrón que ya usaba el proyecto para otro cambio de esquema: `CREATE TABLE nueva AS SELECT...` + `DROP` de la vieja + `RENAME`, con `PRAGMA foreign_keys = OFF` mientras dura para que el hueco intermedio sin la tabla no rompiera las referencias de otras tablas.

Contra una base de datos de pruebas (siempre vacía) funcionaba perfecto. Contra una **copia real** de la base de datos de producción (probada antes de desplegar, nunca en caliente), el `DROP TABLE` reventaba con `FOREIGN KEY constraint failed` en cuanto la tabla tenía filas de verdad referenciadas desde otras tablas.

La causa, documentada en la propia SQLite pero fácil de pasar por alto: `PRAGMA foreign_keys` es un no-op si ya hay una transacción abierta — y la había, porque el resto del esquema/migraciones ya habían escrito antes en esa misma conexión. El `OFF` se aceptaba sin error y sin efecto real. **Fix**: un `conn.commit()` explícito justo antes de cada `PRAGMA foreign_keys = OFF`, para forzar el cierre de la transacción en curso antes de tocar el flag.

Es exactamente el tipo de bug que un test con base de datos vacía nunca va a encontrar — solo aparece con datos reales referenciados de verdad. De ahí la regla que quedó para cualquier migración futura: probarla siempre contra una copia real antes de tocar producción, no solo contra los tests.

## Sesiones y cookies: cuatro fixes tras una revisión de seguridad

Una revisión de seguridad externa sobre el código encontró cuatro problemas reales en la capa de autenticación, verificados uno a uno contra el código antes de tocar nada y con tests (`TestClient`) antes de desplegar:

- **Clave de sesión sin valor por defecto conocido**: la cookie firmada usaba `itsdangerous` con una clave que, si faltaba la variable de entorno, caía a un string fijo hardcodeado en el código — cualquiera con acceso al repositorio podía fabricar una cookie de sesión válida. Ahora el arranque falla explícitamente (`RuntimeError`) si la clave no está puesta, en vez de arrancar con un valor inseguro.
- **Open redirect en el login**: el parámetro `?next=` se usaba tal cual para redirigir tras autenticarse — un enlace manipulado (`?next=https://sitio-malicioso.com`) podía redirigir fuera del sitio tras un login legítimo. Ahora solo se aceptan rutas internas (nunca una URL completa, nunca `//dominio` — ese prefijo también es una forma válida de indicar un host externo).
- **Sin límite de intentos**: el formulario de login no frenaba fuerza bruta. Añadido un límite (N intentos fallidos por ventana de tiempo) que bloquea nuevos intentos, incluida la contraseña correcta, hasta que pasa la ventana.
- **Subida de archivo sin validar**: un endpoint de subida de imagen leía el archivo entero a memoria sin comprobar tipo ni tamaño. Ahora valida `content-type` contra una lista blanca y corta la lectura a un límite razonable en streaming, no tras cargarlo entero.
