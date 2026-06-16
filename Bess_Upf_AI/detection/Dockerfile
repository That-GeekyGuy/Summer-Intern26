# -- Build stage ---------------------------------------------------------------
FROM golang:1.22-alpine@sha256:1699c10032ca2582ec89a24a1312d986a3f094aed3d5c1147b19880afe40e052 AS builder

WORKDIR /build
COPY go.mod go.sum ./
RUN go mod download

COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o detection .

# -- Runtime stage -------------------------------------------------------------
FROM alpine:3.19@sha256:6baf43584bcb78f2e5847d1de515f23499913ac9f12bdf834811a3145eb11ca1

RUN apk add --no-cache ca-certificates && \
    addgroup -S det && adduser -S det -G det

WORKDIR /app
COPY --from=builder /build/detection .

RUN mkdir -p /data /etc/detection && chown -R det:det /data

USER det

EXPOSE 8081
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -qO- http://localhost:8081/health || exit 1

ENTRYPOINT ["./detection"]
