#!/usr/bin/env python3
"""Fase 6 del indice de afinidad (encargo 2026-08-14): prueba combinaciones de los
pesos de combinacion (exponente de apetito/calidad y castigo por desfase) por fuerza
bruta contra el historial real, y propone la que menos error de prediccion da. Sin
modelos ni librerias de optimizacion - un grid pequeño y una medida de error honesta
(leave-one-out, cada titulo puntuado predicho con un perfil que lo excluye a si
mismo, igual que hace recompute_taste_profile).

Cada combinacion evaluada relanza el pipeline completo de afinidad (~1 min con la
biblioteca actual del usuario) - el grid por defecto (9 combinaciones) tarda del orden de
10-15 minutos. NO toca la base de datos ni los pesos guardados en /ajustes; solo
imprime el resultado. Si el resultado convence, aplicar los valores a mano desde
/ajustes.

Uso: python3 scripts/tune_affinity_weights.py [usuario]
El indice de afinidad es por-usuario (multiusuario Fase 3, 2026-09-18) - sin
argumento, prueba contra el primer usuario admin de la instancia.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import repo  # noqa: E402
from app.db import get_connection  # noqa: E402

# Grid deliberadamente pequeño (ver docstring del modulo): variar todo el espacio de
# pesos (16 numeros) por fuerza bruta es computacionalmente inviable con este pipeline
# (cada evaluacion recalcula franquicias/ritmo/elo 449 veces). Los dos parametros de
# aqui son los que mas cambian el comportamiento cualitativo del resultado final.
APPETITE_EXP_CANDIDATES = [0.5, 0.6, 0.7]
GAP_FACTOR_CANDIDATES = [0.3, 0.5, 0.7]


def _mae(raw, cfg) -> tuple[float, int]:
    pairs = repo._backtest_leave_one_out(raw, cfg)
    if not pairs:
        return float("inf"), 0
    errors = [abs(score - rating * 10) for score, rating in pairs]
    return sum(errors) / len(errors), len(pairs)


def main():
    username = sys.argv[1] if len(sys.argv) > 1 else None
    with get_connection() as conn:
        if username:
            user = repo.get_user_by_username(conn, username)
            if not user:
                print(f"No existe el usuario '{username}'")
                return
        else:
            users = repo.list_users(conn)
            user = next((u for u in users if u["is_admin"]), users[0] if users else None)
            if not user:
                print("No hay ningun usuario todavia - crea una cuenta primero.")
                return
        user_id = user["id"]
        base_cfg = repo.get_affinity_config(conn, user_id)
        raw = repo._load_affinity_raw(conn, user_id)

    print(f"Usuario: {user['username']} - biblioteca: {len(raw['titles'])} titulos con anilist_id\n")
    print(f"{'apetito_exp':>12} {'calidad_exp':>12} {'gap_factor':>11} {'MAE':>8} {'n':>5}")

    results = []
    for appetite_exp in APPETITE_EXP_CANDIDATES:
        for gap_factor in GAP_FACTOR_CANDIDATES:
            cfg = dict(base_cfg)
            cfg["appetite_exp"] = appetite_exp
            cfg["quality_exp"] = round(1 - appetite_exp, 2)
            cfg["gap_factor"] = gap_factor
            mae, n = _mae(raw, cfg)
            results.append((mae, appetite_exp, cfg["quality_exp"], gap_factor, n))
            print(f"{appetite_exp:>12.2f} {cfg['quality_exp']:>12.2f} {gap_factor:>11.2f} {mae:>8.2f} {n:>5}")

    results.sort(key=lambda r: r[0])
    best_mae, best_ap, best_ca, best_gap, best_n = results[0]
    current_mae, current_n = _mae(raw, base_cfg)
    print(f"\nMejor combinacion: apetito_exp={best_ap}, calidad_exp={best_ca}, gap_factor={best_gap} (MAE={best_mae:.2f}, n={best_n})")
    print(f"Configuracion actual en /ajustes: apetito_exp={base_cfg['appetite_exp']}, calidad_exp={base_cfg['quality_exp']}, gap_factor={base_cfg['gap_factor']} (MAE={current_mae:.2f}, n={current_n})")
    if best_mae < current_mae:
        print("-> La combinacion actual NO es la de menor error en este grid. Considera aplicarla a mano en /ajustes.")
    else:
        print("-> La configuracion actual ya es la de menor error dentro de este grid.")


if __name__ == "__main__":
    main()
