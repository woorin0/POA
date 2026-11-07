"""Real SQLite IO failure after sidecar commit; no validator/admission mocks."""
from fill_confirmation_contracts import setup,feed,tick,confirmation_posts,DP,IntakeStore,E


def test_capture_commit_replays_after_main_database_unavailable(tmp_path,monkeypatch):
    r=setup(tmp_path,monkeypatch)
    store=r.store()
    with store._connect():
        accepted=feed(r)
    assert accepted['accepted']
    with IntakeStore(r.st.VERIFIED_NOTIFICATIONS_DB_PATH).connect() as c:
        raw,digest=c.execute('SELECT request,request_digest FROM notification_intake').fetchone()
        import json
        assert E.digest(json.loads(raw))==digest
    tick(r);DP.flush_outbox(r.st);tick(r);DP.flush_outbox(r.st)
    assert len(confirmation_posts(r))==1
