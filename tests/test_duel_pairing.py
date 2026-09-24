"""Emparejamiento del duelo (random_duel_pair): sin pares repetidos mientras haya
alternativas, descanso para los que acaban de jugar, y los primeros del ranking
siguen jugando aunque queden elementos sin ningún duelo."""
import random
from collections import Counter

from app import repo


def _entries(conn, user_id, n, elo=1500.0, rd=350.0):
    ids = []
    for i in range(n):
        title = repo.ensure_manual_title(conn, "movie", f"Peli {len(ids)}-{elo}-{i}", 2000 + i)
        cur = conn.execute(
            "INSERT INTO entries (title_id, user_id, status, elo, rd) VALUES (?, ?, 'watched', ?, ?)",
            (title["id"], user_id, elo, rd),
        )
        ids.append(cur.lastrowid)
    return ids


def test_does_not_repeat_pairs_while_there_are_unplayed_ones(conn, user_id):
    random.seed(1)
    ids = _entries(conn, user_id, 6)
    seen = Counter()
    for _ in range(15):  # 6 items = 15 pares posibles
        a, b = repo.random_duel_pair(conn, "entries", ids)
        seen[frozenset((a, b))] += 1
        repo.record_duel(conn, "entries", a, b, user_id, 0.5)
    assert max(seen.values()) <= 2
    assert len(seen) >= 12


def test_last_duel_players_rest(conn, user_id):
    random.seed(2)
    ids = _entries(conn, user_id, 12)
    for _ in range(30):
        a, b = repo.random_duel_pair(conn, "entries", ids)
        last = conn.execute(
            "SELECT winner_id, loser_id FROM duels WHERE table_name = 'entries' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if last:
            assert {a, b}.isdisjoint({last["winner_id"], last["loser_id"]})
        repo.record_duel(conn, "entries", a, b, user_id, 1.0)


def test_top_keeps_playing_with_undueled_items_left(conn, user_id):
    """Antes, mientras quedara un item con RD 350, los ya jugados no volvian a salir."""
    random.seed(3)
    top = _entries(conn, user_id, 4, elo=2400.0, rd=290.0)
    fresh = _entries(conn, user_id, 80)
    ids = top + fresh
    hits = 0
    for _ in range(100):
        pair = repo.random_duel_pair(conn, "entries", ids)
        hits += bool(set(pair) & set(top))
    assert hits > 0


def test_tiny_pool_still_returns_a_pair(conn, user_id):
    ids = _entries(conn, user_id, 2)
    for _ in range(3):
        a, b = repo.random_duel_pair(conn, "entries", ids)
        assert {a, b} == set(ids)
        repo.record_duel(conn, "entries", a, b, user_id, 1.0)
    assert repo.random_duel_pair(conn, "entries", ids[:1]) is None
