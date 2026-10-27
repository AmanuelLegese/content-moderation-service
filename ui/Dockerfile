# Stage 1: build the React app with Node. Only the static output is kept.
FROM node:22-alpine AS build
WORKDIR /ui
# Dependencies first for layer caching; npm ci installs exactly package-lock.json.
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
# HANDLER from docker-compose; Vite inlines VITE_* variables into the bundle at build time.
ARG VITE_HANDLER=fixed
ENV VITE_HANDLER=$VITE_HANDLER
RUN npm run build

# Stage 2: a small nginx image serving the built files and proxying /api (see nginx.conf).
FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /ui/dist /usr/share/nginx/html
