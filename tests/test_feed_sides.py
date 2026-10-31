"""Switching breasts during a feed: logged as "both", suggestion follows the last side."""

import csv
import io

from app import db, repo


def _start(client, side):
    r = client.post("/feed/start", data={"method": "breast", "side": side}, follow_redirects=False)
    assert r.status_code == 303
    return r


def _switch(client, feed_id, side):
    return client.post(f"/feed/{feed_id}/side", data={"side": side}, follow_redirects=False)


def test_switching_sides_logs_both_and_suggests_from_last_side(client, conn):
    _start(client, "left")
    feed = repo.active_feeding(conn)
    assert (feed["side"], feed["last_side"]) == ("left", "left")
    assert repo.suggested_side(conn) == "right"

    r = _switch(client, feed["id"], "right")
    assert "toast=feed-switched" in r.headers["location"]
    feed = repo.get(conn, "feedings", feed["id"])
    assert (feed["side"], feed["last_side"]) == ("both", "right")

    home = client.get("/").text
    assert "Both, now on right" in home
    assert 'value="right" class="btn small selected"' in home
    assert 'value="both"' not in home  # only the two breasts to switch between

    client.post(f"/feed/{feed['id']}/stop")
    data = client.get("/api/dash").json()["feeding"]
    assert data["last"]["side"] == "both" and data["last"]["side_label"] == "Both"
    assert data["last"]["last_side"] == "right"
    assert data["suggested_side"] == "left"

    home = client.get("/").text
    assert "Breast both (ended right)" in home
    assert "&#9654; Left<small>suggested</small>" in home
    assert "Breast · Both" in client.get("/timeline").text
    assert "ended right" in client.get("/timeline").text


def test_switching_back_and_forth_tracks_the_latest_side(client, conn):
    _start(client, "right")
    fid = repo.active_feeding(conn)["id"]
    _switch(client, fid, "left")
    _switch(client, fid, "right")
    feed = repo.get(conn, "feedings", fid)
    assert (feed["side"], feed["last_side"]) == ("both", "right")
    assert repo.suggested_side(conn) == "left"


def test_tapping_the_current_side_changes_nothing(client, conn):
    _start(client, "left")
    fid = repo.active_feeding(conn)["id"]
    r = _switch(client, fid, "left")
    assert "toast" not in r.headers["location"]
    feed = repo.get(conn, "feedings", fid)
    assert (feed["side"], feed["last_side"]) == ("left", "left")


def test_choosing_a_side_for_a_sideless_feed_is_not_a_switch(client, conn):
    client.post("/feed/start", data={"method": "breast"})
    fid = repo.active_feeding(conn)["id"]
    _switch(client, fid, "right")
    feed = repo.get(conn, "feedings", fid)
    assert (feed["side"], feed["last_side"]) == ("right", "right")


def test_side_cannot_change_after_the_feed_ends(client, conn):
    _start(client, "left")
    fid = repo.active_feeding(conn)["id"]
    client.post(f"/feed/{fid}/stop")
    _switch(client, fid, "right")
    feed = repo.get(conn, "feedings", fid)
    assert (feed["side"], feed["last_side"]) == ("left", "left")


def test_feed_form_records_the_side_a_both_feed_finished_on(client, conn):
    r = client.post("/feed/new", data={
        "method": "breast", "side": "both", "last_side": "left", "start_ago": "30", "duration": "20",
    }, follow_redirects=False)
    assert r.status_code == 303, r.text
    feed = repo.last_feeding(conn)
    assert (feed["side"], feed["last_side"]) == ("both", "left")
    assert repo.suggested_side(conn) == "right"

    # Editing it to a single side makes that the last side, whatever else was posted.
    page = client.get(f"/feed/{feed['id']}/edit").text
    assert "Finished on" in page
    start_at = page.split('name="start_at" value="')[1].split('"')[0]
    client.post(f"/feed/{feed['id']}/edit", data={
        "method": "breast", "side": "right", "last_side": "left", "start_at": start_at, "duration": "20",
    })
    feed = repo.get(conn, "feedings", feed["id"])
    assert (feed["side"], feed["last_side"]) == ("right", "right")
    assert repo.suggested_side(conn) == "left"


def test_both_without_a_known_last_side_makes_no_suggestion(client, conn):
    client.post("/feed/new", data={"method": "breast", "side": "both", "start_ago": "30", "duration": "20"})
    feed = repo.last_feeding(conn)
    assert (feed["side"], feed["last_side"]) == ("both", None)
    assert repo.suggested_side(conn) is None


def test_bottle_feed_has_no_sides(client, conn):
    client.post("/feed/new", data={"method": "bottle", "side": "left", "last_side": "left",
                                   "amount": "60", "start_ago": "0"})
    feed = repo.last_feeding(conn)
    assert (feed["side"], feed["last_side"]) == (None, None)


def test_bad_last_side_is_rejected(client):
    r = client.post("/feed/new", data={"method": "breast", "side": "both", "last_side": "middle", "start_ago": "0"})
    assert r.status_code == 400
    assert "Unknown which side it ended on" in r.text


def test_csv_export_includes_last_side(admin):
    admin.post("/feed/new", data={"method": "breast", "side": "both", "last_side": "right",
                                  "start_ago": "30", "duration": "10"})
    rows = list(csv.reader(io.StringIO(admin.get("/admin/export/feedings.csv").text.lstrip("﻿"))))
    header = rows[0]
    assert rows[1][header.index("side")] == "both"
    assert rows[1][header.index("last_side")] == "right"


def test_migration_backfills_last_side(tmp_path):
    path = tmp_path / "old.sqlite3"
    conn = db.connect(path)
    conn.executescript(f"BEGIN;\n{db.MIGRATIONS[0]}\nPRAGMA user_version = 1;\nCOMMIT;")
    rows = [("left", "breast"), ("both", "breast"), (None, "bottle")]
    for side, method in rows:
        conn.execute(
            "INSERT INTO feedings (started_at, ended_at, method, side, created_at, updated_at) "
            "VALUES ('2026-10-01T10:00:00Z', '2026-10-01T10:20:00Z', ?, ?, '', '')",
            (method, side),
        )
    assert db.migrate(conn) == len(db.MIGRATIONS)
    got = [tuple(r) for r in conn.execute("SELECT side, last_side FROM feedings ORDER BY id")]
    assert got == [("left", "left"), ("both", None), (None, None)]
    conn.close()
