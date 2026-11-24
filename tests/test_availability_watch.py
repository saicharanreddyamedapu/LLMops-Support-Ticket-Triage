import sqlite3

import httpx

from scripts import availability_watch as watch


def test_resume_keeps_original_window_and_unobserved_hours(monkeypatch):
    db = sqlite3.connect(":memory:")
    start = watch.initialize(db, 1800000000)
    calls = []
    def success(*args):
        calls.append(1)
        return "ok","v1","revision","trace"
    monkeypatch.setattr(watch,"probe",success)
    monkeypatch.setattr(watch,"availability_report",lambda *args: {"status":"met"})
    watch.sample(db,start,start,None,"key")
    assert watch.initialize(db,start + watch.PERIOD) == start
    watch.sample(db,start,start+60,None,"key")
    watch.sample(db,start,start+2*watch.PERIOD,None,"key")
    assert len(calls) == 2
    report = watch.snapshot(db,start,start+2*watch.PERIOD,None)
    assert (report["due"],report["missing"],report["status"]) == (3,1,"collecting")
    report = watch.snapshot(db,start,start+watch.PERIOD*watch.SLOTS,None)
    assert report["status"] == "incomplete" and report["missing"] == watch.SLOTS-2


def test_probe_declines_nonlocal_service():
    def handler(request):
        assert request.url.path == "/readyz"
        return httpx.Response(200,json={"status":"ready","mode":"live"})
    with httpx.Client(base_url="http://test",transport=httpx.MockTransport(handler)) as client:
        assert watch.probe(client,"ticket","key")[0] == "unexpected_mode"
