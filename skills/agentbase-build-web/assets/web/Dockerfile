# Next.js web chat (the BFF + pages) as a container: `make web-docker-build`, from the project root.
# It runs ANYWHERE a container runs — AgentBase Runtime hosts agents (the /invocations contract), not web apps. Point
# BFF_AGENT_URL at the agent runtime's endpoint; set the BFF_* values in the platform's environment, secrets
# (BFF_SESSION_SECRET, BFF_OIDC_CLIENT_SECRET) from its secret store — see references/bff.md of agentbase-build-web.
# Needs package-lock.json (the first `npm install` creates it — commit it): `npm ci` installs exactly that.
# Base images are pinned by digest (the tag stays for readability); Dependabot's docker ecosystem moves them.
FROM node:24-slim@sha256:c86ecead9e55a5210ac1727de38a4ca840bf03dcaf34dc9749addbf383b1920c AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-fund --no-audit

FROM node:24-slim@sha256:c86ecead9e55a5210ac1727de38a4ca840bf03dcaf34dc9749addbf383b1920c AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1 NEXT_OUTPUT=standalone
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN mkdir -p public && npm run build

FROM node:24-slim@sha256:c86ecead9e55a5210ac1727de38a4ca840bf03dcaf34dc9749addbf383b1920c
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0
WORKDIR /app
# Code stays owned by root (read-only for the non-root `node` user)
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
COPY --from=build /app/public ./public
USER node
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD node -e "fetch('http://127.0.0.1:3000/api/health').then(r => process.exit(r.ok ? 0 : 1), () => process.exit(1))"
CMD ["node", "server.js"]
