FROM node:22-alpine AS build
WORKDIR /ui
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
ARG VITE_HANDLER=fixed
ENV VITE_HANDLER=$VITE_HANDLER
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /ui/dist /usr/share/nginx/html
