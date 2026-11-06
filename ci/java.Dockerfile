# Fixture runtime only: no Maven build, model client, or experiment data.
# Both official images are pinned; no mutable apt packages are installed.
FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c AS python
FROM eclipse-temurin:25.0.4.1_1-jdk-noble@sha256:a9c5ddd386f629518bd500ecfabf7d1e5d32d7ffed735d5c3b1604ae92c830f4
COPY --from=python /usr/local /usr/local
RUN ldconfig && python3 -c 'import csv, hashlib, json, ssl, subprocess, xml.etree.ElementTree, zipfile' \
    && javac -version
WORKDIR /experiment
