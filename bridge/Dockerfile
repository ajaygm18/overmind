# The OMA bridge. One service, one dependency, no build step -- tsx runs the
# TypeScript directly, which is also how `npm start` runs it outside Docker, so
# there is no second execution path to keep in sync.
FROM node:22-alpine

# package.json declares "node": ">=22"; the tag is pinned to the same major so a
# node 24 release cannot silently change what this image runs.
WORKDIR /bridge

# Dependencies first: this layer is cached until package.json changes, so
# editing planner.ts does not reinstall @open-multi-agent/core.
#
# NOTE: there is no package-lock.json in the repo, so this resolves within the
# declared semver ranges at build time. That makes the image repeatable, not
# reproducible. See docs/LIMITATIONS.md.
COPY package.json ./
RUN npm install --no-audit --no-fund

COPY tsconfig.json planner.ts schema.ts ./

# node:alpine ships a non-root `node` user. The bridge reads no files at runtime
# and needs no write access anywhere, so there is no reason for it to be root.
USER node

ENV OVERMIND_BRIDGE_PORT=7801
EXPOSE 7801

# Bound to 0.0.0.0 inside the container only. The compose file publishes it on
# 127.0.0.1, so it is not reachable from outside the host.
CMD ["npm", "start"]
