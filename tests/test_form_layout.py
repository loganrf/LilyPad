"""Every form leads with its submit button; the options to adjust follow it."""

import re

import pytest

from app import repo

# path -> (submit label, a field that must come after it)
OPEN_FORMS = {
    "/feed/new": ("Save feed", 'name="method"'),
    "/diaper/new": ("Save", 'name="kind"'),
    "/vitals/new": ("Save vitals", 'name="p_'),
    "/note/new": ("Save note", 'name="body"'),
}
ADMIN_FORMS = {
    "/admin/settings": ("Save settings", 'name="baby_name"'),
    "/admin/reminders/new": ("Save reminder", 'name="name"'),
    "/admin/vitals/new": ("Save", 'name="name"'),
}


def _form_with(html: str, label: str) -> str:
    for form in re.findall(r"<form\b.*?</form>", html, re.S):
        if f">{label}</button>" in form:
            return form
    raise AssertionError(f"no form with a {label!r} button")


def _assert_submit_first(html: str, label: str, field: str) -> None:
    form = _form_with(html, label)
    submit = form.index(f">{label}</button>")
    assert form.count('type="submit"') == 1
    assert 'class="form-actions form-actions-top"' in form
    assert submit < form.index(field), f"{label!r} should come before {field}"
    # Nothing the user fills in sits above the button (hidden inputs aside).
    above = form[:submit]
    assert not re.search(r'<(textarea|select)\b|<input(?![^>]*type="hidden")', above)


@pytest.mark.parametrize("path", OPEN_FORMS)
def test_logging_forms_lead_with_submit(client, path):
    label, field = OPEN_FORMS[path]
    _assert_submit_first(client.get(path).text, label, field)


@pytest.mark.parametrize("path", ADMIN_FORMS)
def test_admin_forms_lead_with_submit(admin, path):
    label, field = ADMIN_FORMS[path]
    _assert_submit_first(admin.get(path).text, label, field)


def test_edit_forms_lead_with_submit_and_keep_delete_last(client, conn):
    client.post("/feed/start", data={"method": "breast", "side": "left"})
    fid = repo.active_feeding(conn)["id"]
    client.post(f"/feed/{fid}/stop")
    page = client.get(f"/feed/{fid}/edit").text
    _assert_submit_first(page, "Save feed", 'name="quality"')
    assert page.index(">Save feed</button>") < page.index("Delete this entry")

    conn.execute("UPDATE reminders SET enabled = 1 WHERE kind = 'custom'")
    rid = conn.execute("SELECT id FROM reminders WHERE kind = 'custom'").fetchone()[0]
    client.post(f"/task/{rid}/done")
    log_id = repo.last_task_done(conn, rid)["id"]
    _assert_submit_first(client.get(f"/task/{log_id}/edit").text, "Save", 'name="notes"')
