FROM postgres:17.11-bookworm@sha256:639ab7ceb90e13123085b741fb31ef493fba25463002f6da665352e7b534b652

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3-venv ca-certificates \
    && python3 -m venv /opt/patroni \
    && /opt/patroni/bin/pip install --no-cache-dir 'patroni[etcd3]==4.1.5' 'psycopg2-binary==2.9.11' \
    && install -d -o postgres -g postgres -m 0700 /var/lib/postgresql/patroni \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/opt/patroni/bin:${PATH}"
COPY patroni.yaml /etc/patroni.yaml
USER postgres
# Patroni initializes and starts PostgreSQL itself.
ENTRYPOINT ["patroni", "/etc/patroni.yaml"]
CMD []
