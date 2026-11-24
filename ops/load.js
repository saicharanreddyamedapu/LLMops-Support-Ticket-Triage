import http from 'k6/http';
import { Counter, Trend } from 'k6/metrics';

const rate = Number(__ENV.RATE || 5);
const interval = Number(__ENV.INTERVAL || 1);
const mode = __ENV.MODE || 'fixture';
const seconds = Number(__ENV.SECONDS || 15);
const counts = new Counter('triage_status');
const success = new Trend('triage_success_ms', true);
export const options = {
  scenarios: { triage: { executor: 'constant-arrival-rate', rate, timeUnit: interval + 's',
    duration: seconds + 's', preAllocatedVUs: 12, maxVUs: 48, gracefulStop: mode === 'local' ? '100s' : '5s' } },
  summaryTrendStats: ['med', 'p(95)', 'p(99)', 'max'],
};
export default function () {
  const res = http.post(__ENV.URL || 'http://api:8000/triage', JSON.stringify({text:'How can I track my order?'}),
    {headers:{'Content-Type':'application/json',Authorization:'Bearer ' + (__ENV.TRIAGE_KEY || 'fixture-demo')}});
  counts.add(1, {status:String(res.status)});
  if (res.status === 200) success.add(res.timings.duration);
  if (res.status === 429) limited.add(1);
  else if (res.status !== 200) other.add(1);
}
const limited = new Counter('triage_429');
const other = new Counter('triage_other_error');
export function handleSummary(data) {
  const m = data.metrics;
  const n = m.http_reqs.values.count;
  const target = rate/interval;
  const row = {mode, target_rps:target, duration_s:seconds, n, achieved_rps:n/seconds,
    rejected_429:m.triage_429?.values.count || 0, other_errors:m.triage_other_error?.values.count || 0,
    dropped:m.dropped_iterations?.values.count || 0, all_ms:m.http_req_duration.values, success_ms:m.triage_success_ms?.values || null};
  row.elapsed_s_including_drain = data.state.testRunDurationMs/1000;
  row.completed_rps_including_drain = n/row.elapsed_s_including_drain;
  return {stdout: JSON.stringify(row)+'\n', ['/results/load-'+target+'.json']:JSON.stringify(row,null,2)+'\n'};
}
