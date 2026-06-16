# -- Build stage ---------------------------------------------------------------
FROM golang:1.22-alpine@sha256:1699c10032ca2582ec89a24a1312d986a3f094aed3d5c1147b19880afe40e052 AS builder

# Dependencies are vendored so no internet access or proxy/sumdb needed.
# This avoids the github.com/prometheus/prometheus +incompatible path-validation
# error that proxy.golang.org enforces for v2.x modules without a /v2 suffix.
ENV CGO_ENABLED=0

RUN apk add --no-cache ca-certificates

WORKDIR /build
COPY go.mod go.sum vendor/ ./
COPY . .
RUN go build -mod=vendor -trimpath -ldflags="-s -w" -o analysis .

# -- Runtime stage -------------------------------------------------------------
# Use alpine:3.21 (same Alpine version as the golang builder base) so apk
# package indexes are consistent. ca-certificates is copied from the builder
# layer to avoid any CDN fetch in the final stage.
FROM alpine:3.21

RUN addgroup -S analysis && adduser -S analysis -G analysis

COPY --from=builder /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/
WORKDIR /app
COPY --from=builder /build/analysis .

RUN mkdir -p /data /docs && chown -R analysis:analysis /data

USER analysis

EXPOSE 8082
HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=3 \
    CMD wget -qO- http://localhost:8082/health || exit 1

ENTRYPOINT ["./analysis"]