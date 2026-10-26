# Image for the per-task sandbox (src/sandbox/docker_sandbox.py). Build from the repo root:
#   docker build -t sectriage-sandbox:1 src/sandbox
# The base is pinned by its multi-arch index digest (python:3.12-slim-bookworm). The apt layer is
# not pinned, so rebuilds are not bit-for-bit reproducible. No semgrep: that tool is unsupported.
FROM python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70

# coreutils, grep, findutils and sed come with the base; git is the only addition.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system safe.directory /work \
    && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin sandbox

USER 10001:10001
WORKDIR /work
