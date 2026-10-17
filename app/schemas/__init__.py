from app.schemas.disqualification import (
    DisqualificationBase,
    DisqualificationCreate,
    DisqualificationRead,
    DisqualificationUpdate,
)
from app.schemas.document import (
    DocumentBase,
    DocumentCreate,
    DocumentRead,
    DocumentReadShort,
    DocumentUpdate,
)
from app.schemas.match import (
    MatchBase,
    MatchCreate,
    MatchRead,
    MatchReadShort,
    MatchReadWithTeams,
    MatchUpdate,
)
from app.schemas.match_event import (
    MatchEventBase,
    MatchEventCreate,
    MatchEventRead,
    MatchEventReadWithPlayer,
    MatchEventUpdate,
)
from app.schemas.news import (
    NewsBase,
    NewsCreate,
    NewsRead,
    NewsReadShort,
    NewsUpdate,
)
from app.schemas.player import (
    PlayerBase,
    PlayerCreate,
    PlayerRead,
    PlayerReadShort,
    PlayerUpdate,
)
from app.schemas.stadium import (
    StadiumBase,
    StadiumCreate,
    StadiumRead,
    StadiumReadShort,
    StadiumUpdate,
)
from app.schemas.team import (
    TeamBase,
    TeamCreate,
    TeamRead,
    TeamReadShort,
    TeamUpdate,
)
from app.schemas.team_player import (
    TeamPlayerBase,
    TeamPlayerCreate,
    TeamPlayerRead,
    TeamPlayerReadWithPlayer,
    TeamPlayerUpdate,
)
from app.schemas.tournament import (
    TournamentBase,
    TournamentCreate,
    TournamentRead,
    TournamentReadShort,
    TournamentUpdate,
)
from app.schemas.tournament_standing import (
    TournamentStandingBase,
    TournamentStandingCreate,
    TournamentStandingRead,
    TournamentStandingReadWithTeam,
)
from app.schemas.tournament_team import (
    TournamentTeamBase,
    TournamentTeamCreate,
    TournamentTeamRead,
    TournamentTeamReadWithTeam,
    TournamentTeamUpdate,
)

__all__ = [
    # Disqualification
    "DisqualificationBase", "DisqualificationCreate", "DisqualificationRead",
    "DisqualificationUpdate",
    # Document
    "DocumentBase", "DocumentCreate", "DocumentRead", "DocumentReadShort",
    "DocumentUpdate",
    # Match
    "MatchBase", "MatchCreate", "MatchRead", "MatchReadShort",
    "MatchReadWithTeams", "MatchUpdate",
    # MatchEvent
    "MatchEventBase", "MatchEventCreate", "MatchEventRead",
    "MatchEventReadWithPlayer", "MatchEventUpdate",
    # News
    "NewsBase", "NewsCreate", "NewsRead", "NewsReadShort", "NewsUpdate",
    # Player
    "PlayerBase", "PlayerCreate", "PlayerRead", "PlayerReadShort", "PlayerUpdate",
    # Stadium
    "StadiumBase", "StadiumCreate", "StadiumRead", "StadiumReadShort",
    "StadiumUpdate",
    # Team
    "TeamBase", "TeamCreate", "TeamRead", "TeamReadShort", "TeamUpdate",
    # TeamPlayer
    "TeamPlayerBase", "TeamPlayerCreate", "TeamPlayerRead",
    "TeamPlayerReadWithPlayer", "TeamPlayerUpdate",
    # Tournament
    "TournamentBase", "TournamentCreate", "TournamentRead",
    "TournamentReadShort", "TournamentUpdate",
    # TournamentStanding
    "TournamentStandingBase", "TournamentStandingCreate",
    "TournamentStandingRead", "TournamentStandingReadWithTeam",
    # TournamentTeam
    "TournamentTeamBase", "TournamentTeamCreate", "TournamentTeamRead",
    "TournamentTeamReadWithTeam", "TournamentTeamUpdate",
]