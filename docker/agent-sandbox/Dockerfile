# Sandbox image for the agent's own bash/write/edit/read tools -- not for
# the deterministic checks/git operations (agent/tools/checks.py,
# agent/tools/git.py), which stay on the host since those are fixed commands
# our code runs, never LLM-chosen. This image bounds what an LLM-issued
# `bash` command can reach: only the bind-mounted repo checkout is visible
# inside the container, not the rest of the host filesystem.
#
# node:22-bookworm covers this system's Node-based projects and is
# Debian-based so apt/git/ripgrep are straightforward. pnpm via corepack
# (bundled with Node 22+) rather than a separate install step.
FROM node:22-bookworm

RUN corepack enable && corepack prepare pnpm@10 --activate \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        ripgrep \
        jq \
        ca-certificates \
        python3 \
        python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Playwright + headless Chromium, so the agent can SEE the frontends it
# builds (webapp-testing skill): start the dev server and run a Playwright
# script in ONE bash call -- each bash call is its own ephemeral container,
# so a server started in one call is gone by the next -- then screenshot to
# /workspace and read it with describe_image (the bind mount makes the file
# visible to the host-side vision tool). --with-deps pulls Chromium's shared
# libraries; the version pin matches the browsers baked below so a pip
# upgrade can't silently orphan them.
RUN pip3 install --no-cache-dir --break-system-packages playwright==1.49.1 \
    && playwright install --with-deps chromium

# Git refuses to operate on /workspace ("detected dubious ownership") when
# the bind-mounted repo's owning UID doesn't match the container's own --
# and since `--rm` spins up a brand new container per bash call, any runtime
# fix (`git config --global`, editing ~/.gitconfig) would be discarded the
# instant that call's container exits. `--system` (not `--global`) writes to
# /etc/gitconfig, which is part of this image layer -- baked in once here,
# it's present in every container instantiated from this image with no
# runtime action needed.
RUN git config --system --add safe.directory '*'

WORKDIR /workspace
