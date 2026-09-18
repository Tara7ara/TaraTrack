"""app.repo.comments - debate por episodio (2026-09-18, Fase 4 multiusuario).

Unico modulo de repo pensado para datos VISIBLES ENTRE usuarios: un hilo
cronologico de comentarios por episodio. El resto de "comentario de episodio"
(episode_user_state, ver repo/titles.py) sigue siendo privado, un mini-diario
por usuario - esto es distinto, es la funcion que pidio el amigo de Tara para
poder comentar y debatir episodio a episodio."""


def list_episode_comments(conn, episode_id: int):
    return conn.execute(
        """SELECT episode_comments.id, episode_comments.body, episode_comments.created_at,
                  episode_comments.user_id, users.username, users.avatar_path
           FROM episode_comments
           JOIN users ON users.id = episode_comments.user_id
           WHERE episode_comments.episode_id = ?
           ORDER BY episode_comments.created_at ASC, episode_comments.id ASC""",
        (episode_id,),
    ).fetchall()


def count_episode_comments(conn, episode_id: int) -> int:
    return conn.execute(
        "SELECT count(*) FROM episode_comments WHERE episode_id = ?", (episode_id,)
    ).fetchone()[0]


def add_episode_comment(conn, episode_id: int, user_id: int, body: str):
    body = body.strip()
    if not body:
        return
    conn.execute(
        "INSERT INTO episode_comments (episode_id, user_id, body) VALUES (?, ?, ?)",
        (episode_id, user_id, body),
    )


def delete_episode_comment(conn, comment_id: int, user_id: int, is_admin: bool):
    """Fase 5 (Tara, 2026-09-18): cada uno borra el suyo, admin borra el de cualquiera."""
    if is_admin:
        conn.execute("DELETE FROM episode_comments WHERE id = ?", (comment_id,))
    else:
        conn.execute("DELETE FROM episode_comments WHERE id = ? AND user_id = ?", (comment_id, user_id))


def _comments_seen_at(conn, user_id: int) -> str | None:
    row = conn.execute("SELECT comments_seen_at FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["comments_seen_at"] if row else None


def count_unseen_comments(conn, user_id: int) -> int:
    """Aviso en el nav (Tara, 2026-09-18: "estilo tvtime") - comentarios de OTROS
    usuarios desde la ultima vez que este usuario "se puso al dia" (ver
    mark_comments_seen). Los propios no cuentan, no hace falta avisarte de lo que
    tu mismo has escrito."""
    seen_at = _comments_seen_at(conn, user_id)
    if not seen_at:
        return 0
    return conn.execute(
        "SELECT count(*) FROM episode_comments WHERE created_at > ? AND user_id != ?",
        (seen_at, user_id),
    ).fetchone()[0]


def get_oldest_unseen_comment(conn, user_id: int):
    """El comentario ajeno mas antiguo que este usuario aun no ha visto - a donde
    lleva el aviso del nav al pulsarlo (ver routers/titulo.py:comentarios_siguiente)."""
    seen_at = _comments_seen_at(conn, user_id)
    if not seen_at:
        return None
    return conn.execute(
        """SELECT episode_comments.episode_id, titles.tmdb_id, titles.type
           FROM episode_comments
           JOIN episodes ON episodes.id = episode_comments.episode_id
           JOIN titles ON titles.id = episodes.title_id
           WHERE episode_comments.created_at > ? AND episode_comments.user_id != ?
           ORDER BY episode_comments.created_at ASC LIMIT 1""",
        (seen_at, user_id),
    ).fetchone()


def mark_comments_seen(conn, user_id: int):
    conn.execute("UPDATE users SET comments_seen_at = datetime('now') WHERE id = ?", (user_id,))
