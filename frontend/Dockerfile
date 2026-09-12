FROM node:24-alpine AS dependencies
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM dependencies AS build
COPY . .
ARG NEXT_PUBLIC_LEXTRACE_API_URL=http://localhost:8000
ENV NEXT_PUBLIC_LEXTRACE_API_URL=$NEXT_PUBLIC_LEXTRACE_API_URL
RUN npm run build

FROM node:24-alpine AS runtime
ENV NODE_ENV=production
WORKDIR /app
RUN addgroup -S lextrace && adduser -S lextrace -G lextrace
COPY --from=build --chown=lextrace:lextrace /app/public ./public
COPY --from=build --chown=lextrace:lextrace /app/.next/standalone ./
COPY --from=build --chown=lextrace:lextrace /app/.next/static ./.next/static
USER lextrace
EXPOSE 3000
CMD ["node", "server.js"]
