import time
from scripts.common import quantile,write
from app.gateway import PII

if __name__=="__main__":
    positives=["test.person@example.org","(202) 555-0143","202-555-0152","+1 202 555 0152","a+b@sample.co.uk"]
    negatives=["order ID 12345","account pending","shipping by Friday","ticket ABC123","refund 12.50"]
    times=[]
    for _ in range(1000):
        for value in positives+negatives:
            start=time.perf_counter_ns()
            PII.sub("[REDACTED]",value)
            times.append((time.perf_counter_ns()-start)/1000)
    write("results/fixture/redaction.json",dict(mode="local synthetic",positives=len(positives),negatives=len(negatives),
          fully_redacted=sum(PII.sub("[REDACTED]",v)=="[REDACTED]" for v in positives),
          false_positives=sum(bool(PII.search(v)) for v in negatives), calls=len(times),p50_us=quantile(times,.5),p95_us=quantile(times,.95)))
