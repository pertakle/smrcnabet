DROP TABLE IF EXISTS paid_out_bets;
DROP TABLE IF EXISTS results;
DROP TABLE IF EXISTS bets;
DROP TABLE IF EXISTS positions;
DROP TABLE IF EXISTS playing_teams;
DROP TABLE IF EXISTS matches;
DROP TABLE IF EXISTS members;
DROP TABLE IF EXISTS teams;
DROP TABLE IF EXISTS players;
DROP TABLE IF EXISTS users;

CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL
);


CREATE TABLE players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL
);

CREATE TABLE teams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL
);

CREATE TABLE members (
    player_id INTEGER,
    team_id INTEGER,
    
    PRIMARY KEY (player_id, team_id),

    FOREIGN KEY (player_id) REFERENCES players(id),
    FOREIGN KEY (team_id) REFERENCES teams(id)
);

CREATE TABLE matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    time DATETIME NOT NULL
);

CREATE TABLE playing_teams (
    match_id INTEGER,
    team_id INTEGER,
    
    PRIMARY KEY (match_id, team_id),

    FOREIGN KEY (match_id) REFERENCES matches(id),
    FOREIGN KEY (team_id) REFERENCES teams(id)
);

CREATE TABLE positions (
    match_id INTEGER,
    position_name TEXT,

    PRIMARY KEY (match_id, position_name),
    FOREIGN KEY (match_id) REFERENCES matches(id)
);

CREATE TABLE bets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    match_id INTEGER NOT NULL,
    team_id INTEGER NOT NULL,
    position_name TEXT NOT NULL,
    amount INTEGER NOT NULL,
    time DATETIME NOT NULL DEFAULT (datetime('now')),

    FOREIGN KEY (user_id) REFERENCES users(id),

    FOREIGN KEY (match_id, team_id)
        REFERENCES playing_teams(match_id, team_id),

    FOREIGN KEY (match_id, position_name)
        REFERENCES positions(match_id, position_name)

    -- we allow multiple bets, why not
    -- UNIQUE (user_id, match_id, team_id, position)
);

CREATE TABLE results (
    match_id INTEGER,
    team_id INTEGER,
    position_name TEXT,

    PRIMARY KEY (match_id, team_id, position_name),
    
    FOREIGN KEY (match_id, team_id)
        REFERENCES playing_teams(match_id, team_id),

    FOREIGN KEY (match_id, position_name)
        REFERENCES positions(match_id, position_name)
);
