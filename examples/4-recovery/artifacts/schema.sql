-- dialect: sqlite
CREATE TABLE urls (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    short_code   VARCHAR(16) NOT NULL UNIQUE,
    long_url     TEXT NOT NULL,
    created_at   DATETIME NOT NULL,
    expires_at   DATETIME NULL
);
CREATE TABLE clicks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    url_id       INTEGER NOT NULL REFERENCES urls(id),
    clicked_at   DATETIME NOT NULL,
    referrer     VARCHAR(512) NULL,
    user_agent   VARCHAR(512) NULL
);
CREATE INDEX ix_clicks_url_id_clicked_at ON clicks (url_id, clicked_at);

-- dialect: postgresql
CREATE TABLE urls (
    id           SERIAL PRIMARY KEY,
    short_code   VARCHAR(16) NOT NULL UNIQUE,
    long_url     TEXT NOT NULL,
    created_at   TIMESTAMP NOT NULL,
    expires_at   TIMESTAMP NULL
);
CREATE TABLE clicks (
    id           SERIAL PRIMARY KEY,
    url_id       INTEGER NOT NULL REFERENCES urls(id),
    clicked_at   TIMESTAMP NOT NULL,
    referrer     VARCHAR(512) NULL,
    user_agent   VARCHAR(512) NULL
);
CREATE INDEX ix_clicks_url_id_clicked_at ON clicks (url_id, clicked_at);
