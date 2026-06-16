# ── Build stage ──────────────────────────────────────────────────────────────
FROM golang:1.22-alpine AS builder

RUN apk add --no-cache git

WORKDIR /build
COPY go.mod go.sum ./
RUN go mod download

COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o upf-sim .

# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM alpine:3.19

RUN apk add --no-cache ca-certificates && \
    addgroup -S sim && adduser -S sim -G sim

WORKDIR /app
COPY --from=builder /build/upf-sim .

USER sim

EXPOSE 8090
HEALTHCHECK --interval=15s --timeout=5s --start-period=5s --retries=3 \
    CMD wget -qO- http://localhost:8090/healthz || exit 1

ENTRYPOINT ["./upf-sim"]
