import pytest

from scripts import slo_report


@pytest.mark.parametrize(("observed","down","eligible","success","expected"), [
    (16, 0, 47, 47, "incomplete_scrape_history"),
    (slo_report.MINUTES, 1, 100, 100, "incomplete_scrape_history"),
    (0, 0, None, None, "incomplete_scrape_history"),
    (slo_report.MINUTES, 0, 0, 0, "no_measurable_requests"),
    (slo_report.MINUTES, 0, 100, 99, "met"),
    (slo_report.MINUTES, 0, 100, 98, "missed"),
])
def test_seven_day_status(monkeypatch, observed, down, eligible, success, expected):
    end = 1_800_000_000
    def query(client, endpoint, expression, **params):
        assert 'instance="host.docker.internal:8766"' in expression
        if endpoint == "query_range":
            assert params == dict(start=end-60*(slo_report.MINUTES-1), end=end, step=60)
            return [{"values":[[params["start"]+60*i, str(int(i >= down))] for i in range(observed)]}] if observed else []
        assert params == {"time":end}
        value = success if 'outcome="ok"' in expression else eligible
        return [{"value":[end,str(value)]}] if value is not None else []
    monkeypatch.setattr(slo_report, "query", query)
    report = slo_report.availability_report(None, "local", end+55)
    assert report["status"] == expected
    assert report["missing_minutes"] == slo_report.MINUTES-observed
    assert report["minutes_with_failed_scrape"] == down
    assert report["observed_availability"] == (success/eligible if eligible else None)
