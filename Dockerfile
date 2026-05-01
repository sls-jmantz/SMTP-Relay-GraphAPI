# Base image pinned by immutable digest (not just tag) to prevent a poisoned
# or tag-swapped base image from sliding into a rebuild. To upgrade, pick a
# new digest from https://hub.docker.com/_/python/tags and update both the
# tag and the @sha256: part. The tag is cosmetic; the digest is authoritative.
FROM python:3.12-slim@sha256:804ddf3251a60bbf9c92e73b7566c40428d54d0e79d3428194edf40da6521286

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /opt/smtp-relay

# Install dependencies with full hash verification. --require-hashes forces
# pip to refuse any package whose sha256 doesn't match one listed in the
# lockfile, AND refuses any transitive package that doesn't carry a hash at
# all. Combined with the digest-pinned base image (which transitively pins
# the pip + certifi bundle used to reach PyPI) this is end-to-end verified
# from the OS layer up to each wheel.
#
# --only-binary=:all: prevents pip from silently falling back to an sdist
# build (which could execute attacker-controlled setup.py) if a wheel is
# unavailable. All pinned packages publish wheels.
#
# --no-deps is safe here because requirements.txt is a fully resolved
# lockfile from pip-compile; it already contains every transitive dep.
COPY requirements.txt ./
RUN pip install \
      --require-hashes \
      --no-deps \
      --only-binary=:all: \
      -r requirements.txt

# App code.
COPY app/ ./app/

# Non-root user. Bind to :25 via CAP_NET_BIND_SERVICE (set in compose).
RUN useradd --system --uid 1000 --home /opt/smtp-relay relay \
 && chown -R relay:relay /opt/smtp-relay
USER relay

ENV RELAY_CONFIG=/etc/smtp-relay/config.yaml

EXPOSE 25

ENTRYPOINT ["python", "-m", "app.main"]
