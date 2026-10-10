"""Static stop-name search: one best matching stop per route direction."""
from __future__ import annotations

import sqlite3


def search_stop_routes(
    connection: sqlite3.Connection,
    *,
    query: str,
    city_prefixes: tuple[str, ...] = (),
    limit: int = 40,
) -> list[dict]:
    normalized = query.strip().lower().replace("臺", "台")
    if not normalized:
        return []

    args: dict[str, object] = {"query": normalized, "limit": limit}
    scope = "1 = 1"
    if city_prefixes:
        # GLOB's fixed, authority-validated prefix can use the routeid index.
        scope = " OR ".join(f"s.routeid GLOB :city_{index}" for index in range(len(city_prefixes)))
        args.update({f"city_{index}": f"{prefix}*" for index, prefix in enumerate(city_prefixes)})

    rows = connection.execute(
        f"""
        WITH normalized AS (
            SELECT s.routeid, s.pathid, s.stopid, s.name AS stop_name,
                   s.name_en AS stop_name_en, s.seq, s.lat, s.lon,
                   REPLACE(LOWER(TRIM(s.name)), '臺', '台') AS name_zh,
                   REPLACE(LOWER(TRIM(COALESCE(s.name_en, ''))), '臺', '台') AS name_en
            FROM stops s
            WHERE ({scope})
        ), matches AS (
            SELECT *,
                   CASE WHEN name_zh = :query OR name_en = :query THEN 0
                        WHEN INSTR(name_zh, :query) = 1 OR INSTR(name_en, :query) = 1 THEN 1
                        ELSE 2 END AS match_rank,
                   MIN(CASE WHEN INSTR(name_zh, :query) > 0 THEN LENGTH(name_zh) ELSE 2147483647 END,
                       CASE WHEN INSTR(name_en, :query) > 0 THEN LENGTH(name_en) ELSE 2147483647 END)
                       - LENGTH(:query) AS length_gap
            FROM normalized
            WHERE INSTR(name_zh, :query) > 0 OR INSTR(name_en, :query) > 0
        ), ranked AS (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY routeid, pathid
                ORDER BY match_rank, length_gap, stop_name, seq, stopid
            ) AS position
            FROM matches
        )
        SELECT s.routeid, r.name AS route_name, r.name_en AS route_name_en,
               s.pathid, COALESCE(p.name, '') AS path_name,
               COALESCE(p.name_en, '') AS path_name_en,
               s.stopid, s.stop_name, s.stop_name_en, s.seq, s.lat, s.lon,
               SUBSTR(s.routeid, 1, 3) AS city_code
        FROM ranked s
        JOIN routes r ON r.routeid = s.routeid
        JOIN paths p ON p.routeid = s.routeid AND p.pathid = s.pathid
        WHERE s.position = 1
        ORDER BY s.match_rank, s.length_gap, r.name COLLATE NOCASE, s.routeid, s.pathid, s.seq
        LIMIT :limit
        """,
        args,
    ).fetchall()
    return [dict(row) for row in rows]
