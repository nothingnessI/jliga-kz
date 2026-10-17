import enum


class TournamentStatus(str, enum.Enum):
    DRAFT = "draft"
    ACCEPTING_APPLICATIONS = "accepting_applications"
    ACTIVE = "active"
    FINISHED = "finished"


class TournamentFormat(str, enum.Enum):
    ROUND_ROBIN = "round_robin"
    GROUPS_PLAYOFF = "groups_playoff"


class MatchStatus(str, enum.Enum):
    SCHEDULED = "scheduled"
    LIVE = "live"
    FINISHED = "finished"
    CANCELLED = "cancelled"


class MatchStage(str, enum.Enum):
    GROUP = "group"
    PLAYOFF = "playoff"
    FINAL = "final"


class PlayerPosition(str, enum.Enum):
    GOALKEEPER = "goalkeeper"
    DEFENDER = "defender"
    MIDFIELDER = "midfielder"
    FORWARD = "forward"


class MatchEventType(str, enum.Enum):
    GOAL = "goal"
    OWN_GOAL = "own_goal"
    PENALTY_GOAL = "penalty_goal"
    YELLOW_CARD = "yellow_card"
    RED_CARD = "red_card"
    SUBSTITUTION_IN = "substitution_in"
    SUBSTITUTION_OUT = "substitution_out"


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    EDITOR = "editor"
    CAPTAIN = "captain"
    USER = "user"