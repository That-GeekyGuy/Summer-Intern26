# Stage 1: build
FROM node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293 AS builder
WORKDIR /app
COPY package.json ./
RUN npm install
COPY . .
ARG VITE_ENABLE_SCENARIO_CONTROLS
ENV VITE_ENABLE_SCENARIO_CONTROLS=$VITE_ENABLE_SCENARIO_CONTROLS
RUN npm run build

# Stage 2: serve
FROM nginx:alpine@sha256:8b1e78743a03dbb2c95171cc58639fef29abc8816598e27fb910ed2e621e589a
COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
