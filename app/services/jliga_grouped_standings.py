
"""Standings for tournament page: never combine distinct source sections.

Uses source table snapshots from jliga_structure_2026.py when unambiguous.
Falls back to calculating per group from existing matches (not per team alone).
Does not write to DB. Compatible with the current tournament_detail.html context.
"""
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
import json

from app.models.enums import MatchStage, MatchStatus
from app.models.tournament_team import TournamentTeam
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[2]


def _row(team, group):
    return dict(
        team=team,
        group_name=group,
        games_played=0,
        wins=0,
        draws=0,
        losses=0,
        goals_for=0,
        goals_against=0,
        points=0,
        progress=None,
    )


def _sort(rows):
    return sorted(
        rows,
        key=lambda x: (
            -x.points,
            -(x.goals_for - x.goals_against),
            -x.goals_for,
            x.team.name,
        ),
    )


def _snapshot(tournament, teams):
    if not tournament.jliga_id:
        return None

    path = (
        ROOT
        / "jliga_sync_2026"
        / "structure"
        / f"{tournament.jliga_id}.json"
    )

    if not path.is_file():
        return None

    source = json.loads(
        path.read_text(encoding="utf-8")
    )

    by_source = {
        t.jliga_id: t
        for t in teams
        if t.jliga_id is not None
    }

    names = defaultdict(set)

    for block in source.get("tables", []):
        if block.get("confirmed_group"):
            signature = tuple(
                sorted(
                    (
                        r.get("source_team_id"),
                        r.get("played"),
                        r.get("points"),
                        r.get("goals_for"),
                        r.get("goals_against"),
                    )
                    for r in block.get("rows", [])
                )
            )

            names[block["source_section"]].add(
                signature
            )

    conflicts = {
        key
        for key, values in names.items()
        if len(values) > 1
    }

    groups = {}

    for block in source.get("tables", []):
        group = block["source_section"]

        if (
            not block.get("confirmed_group")
            or group in conflicts
            or group in groups
        ):
            continue

        rows = []

        for value in block.get("rows", []):
            team = by_source.get(
                value.get("source_team_id")
            )

            if team is None:
                rows = []
                break

            fields = (
                "played",
                "wins",
                "draws",
                "losses",
                "goals_for",
                "goals_against",
                "points",
            )

            if any(
                value.get(key) is None
                for key in fields
            ):
                rows = []
                break

            rows.append(
                SimpleNamespace(
                    team=team,
                    group_name=group,
                    games_played=value["played"],
                    wins=value["wins"],
                    draws=value["draws"],
                    losses=value["losses"],
                    goals_for=value["goals_for"],
                    goals_against=value["goals_against"],
                    points=value["points"],
                    progress=value.get("progress"),
                    rank=value["rank"],
                )
            )

        if rows:
            groups[group] = sorted(
                rows,
                key=lambda row: row.rank,
            )

    return groups if groups else None


async def grouped_standings(
    db,
    tournament,
    matches,
    teams,
):
    """
    Returns:
        grouped_standings,
        standings,
        teams_list,
        is_cup
    """
    source = _snapshot(tournament, teams)

    if source:
        flat = [
            row
            for rows in source.values()
            for row in rows
        ]

        unique = list(
            dict.fromkeys(t.id for t in teams)
        )

        unique_teams = [
            next(
                t for t in teams
                if t.id == team_id
            )
            for team_id in unique
        ]

        return (
            source,
            flat,
            unique_teams,
            len(source) > 1,
        )

    memberships = (
        await db.scalars(
            select(TournamentTeam).where(
                TournamentTeam.tournament_id
                == tournament.id
            )
        )
    ).all()

    by_team = defaultdict(set)

    for item in memberships:
        if item.group_name:
            by_team[item.team_id].add(
                item.group_name.strip()
            )

    buckets = defaultdict(dict)

    for team in teams:
        groups = (
            by_team.get(team.id)
            or {"Общая таблица"}
        )

        for group in groups:
            buckets[group][team.id] = _row(
                team,
                group,
            )

    for match in matches:
        if (
            match.home_score is None
            or match.away_score is None
        ):
            continue

        if match.status != MatchStatus.FINISHED:
            continue

        if match.stage in (
            MatchStage.PLAYOFF,
            MatchStage.FINAL,
        ):
            continue

        group = (
            match.group_name or ""
        ).strip()

        if not group:
            home_groups = by_team.get(
                match.home_team_id,
                set(),
            )

            away_groups = by_team.get(
                match.away_team_id,
                set(),
            )

            common = home_groups & away_groups

            group = (
                next(iter(common))
                if len(common) == 1
                else "Общая таблица"
            )

        for team_id in (
            match.home_team_id,
            match.away_team_id,
        ):
            if team_id not in buckets[group]:
                obj = next(
                    (
                        team for team in teams
                        if team.id == team_id
                    ),
                    None,
                )

                if obj:
                    buckets[group][team_id] = _row(
                        obj,
                        group,
                    )

        home = buckets[group].get(
            match.home_team_id
        )

        away = buckets[group].get(
            match.away_team_id
        )

        if not home or not away:
            continue

        home["games_played"] += 1
        away["games_played"] += 1

        home["goals_for"] += match.home_score
        home["goals_against"] += match.away_score

        away["goals_for"] += match.away_score
        away["goals_against"] += match.home_score

        if match.home_score > match.away_score:
            home["wins"] += 1
            away["losses"] += 1
            home["points"] += tournament.points_win
            away["points"] += tournament.points_loss

        elif match.home_score < match.away_score:
            away["wins"] += 1
            home["losses"] += 1
            away["points"] += tournament.points_win
            home["points"] += tournament.points_loss

        else:
            home["draws"] += 1
            away["draws"] += 1
            home["points"] += tournament.points_draw
            away["points"] += tournament.points_draw

    groups = {}

    for group, values in sorted(
        buckets.items()
    ):
        objects = [
            SimpleNamespace(**row)
            for row in values.values()
        ]

        groups[group] = _sort(objects)

    flat = [
        row
        for values in groups.values()
        for row in values
    ]

    return (
        groups,
        flat,
        teams,
        len(groups) > 1,
    )
