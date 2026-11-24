FROM prom/prometheus:v3.6.0 AS monitoring
FROM ghcr.io/actions/actions-runner@sha256:6d0b2d80d80f143f77dcb676e50920100bbb547e39e883454d57f45ab8c31300
USER root
COPY --from=monitoring /bin/promtool /usr/local/bin/promtool
RUN mkdir -p /opt/hostedtoolcache && chown runner:runner /opt/hostedtoolcache +    && ln -s /usr/bin/python3 /usr/local/bin/python
USER runner
ENV RUNNER_TOOL_CACHE=/opt/hostedtoolcache
