import time
from scripts.common import quantile,write
from app.gateway import PII

if __name__=="__main__":
    positives=["test.person@example.org","(202) 555-0143","202-555-0152","+1 202 555 0152","a+b@sample.co.uk"]
    negatives=["order ID 12345","account pending","shipping by Friday","ticket ABC123","refund 12.50"]
    values=positives+negatives
    def identity(text):
        return text
    def redact(text):
        return PII.sub("[REDACTED]",text)
    def batch_us(transform):
        start=time.perf_counter_ns()
        for value in values:
            transform(value)
        return (time.perf_counter_ns()-start)/1000/len(values)
    before, after, added=[], [], []
    for i in range(1000):
        if i % 2:
            enabled, disabled=batch_us(redact), batch_us(identity)
        else:
            disabled, enabled=batch_us(identity), batch_us(redact)
        before.append(disabled)
        after.append(enabled)
        added.append(enabled-disabled)
    write("results/fixture/redaction.json",dict(mode="local synthetic",positives=len(positives),negatives=len(negatives),
          fully_redacted=sum(PII.sub("[REDACTED]",v)=="[REDACTED]" for v in positives),
          false_positives=sum(bool(PII.search(v)) for v in negatives), calls=len(added)*len(values),
          method="1000 paired ten-ticket batches; alternating order; microseconds per ticket",
          disabled_p50_us=quantile(before,.5), enabled_p50_us=quantile(after,.5),
          added_p50_us=quantile(added,.5), added_p95_us=quantile(added,.95)))
